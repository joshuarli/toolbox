"""Unauthenticated Spotify playlist fetching.

Extracted from spotapi/SpotipyFree internals. No credentials, login, or TLS
fingerprinting needed — uses Spotify's private web-player API via urllib3.

API docs: https://developer.spotify.com/documentation/web-api
"""

import base64
import hashlib
import hmac
import json
import os
import re
import struct
import time
from typing import Any

from tool.music.http import Session

_SECRETS_URL = "https://code.thetadev.de/ThetaDev/spotify-secrets/raw/branch/main/secrets/secretDict.json"
_FALLBACK_SECRET = (
    61,
    bytearray(
        [
            44,
            55,
            47,
            42,
            70,
            40,
            34,
            114,
            76,
            74,
            50,
            111,
            120,
            97,
            75,
            76,
            94,
            102,
            43,
            69,
            49,
            120,
            118,
            80,
            64,
            78,
        ]
    ),
)

_CACHE_DIR = os.path.expanduser("~/.cache/spotify-playlist")
_CACHE_PATH = os.path.join(_CACHE_DIR, "secretDict.json")

_PAGE_SIZE = 343  # chunk size for playlist GraphQL pagination


def _read_cached_secret() -> tuple[int, bytearray] | None:
    """Read the cached secretDict from disk. Returns None if missing or corrupt."""
    try:
        with open(_CACHE_PATH) as f:
            data = json.load(f)
        version = max((int(k) for k in data), default=0)
        if version == 0:
            return None
        return version, bytearray(data[str(version)])
    except Exception:
        return None


def _write_cached_secret(data: dict[str, Any]) -> None:
    """Persist the secretDict to disk."""
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(_CACHE_PATH, "w") as f:
            json.dump(data, f)
    except Exception:
        pass  # cache write failures are non-fatal


def _fetch_totp_secret(session: Session) -> tuple[int, bytearray]:
    """Fetch the latest TOTP secret from ThetaDev, or fall back to hardcoded.

    Cached to disk — network fetch only happens when no cache exists.
    Callers that get an auth failure should call _refetch_totp_secret() instead.
    """
    cached = _read_cached_secret()
    if cached is not None:
        return cached

    try:
        data = session.get_json(_SECRETS_URL)
        _write_cached_secret(data)
        version = max((int(k) for k in data), default=18)
        return version, bytearray(data[str(version)])
    except Exception:
        return _FALLBACK_SECRET


def _refetch_totp_secret(session: Session) -> tuple[int, bytearray]:
    """Force-refetch the TOTP secret from network, bypassing cache. Called on auth failure."""
    try:
        data = session.get_json(_SECRETS_URL)
        _write_cached_secret(data)
        version = max((int(k) for k in data), default=18)
        return version, bytearray(data[str(version)])
    except Exception:
        return _FALLBACK_SECRET


def _generate_totp(version: int, secret_bytes: bytearray) -> str:
    """Derive a TOTP code from the obfuscated secret. Pure stdlib — no pyotp."""
    # Deobfuscate: XOR each byte with (index % 33) + 9
    transformed = [b ^ ((i % 33) + 9) for i, b in enumerate(secret_bytes)]
    joined = "".join(str(n) for n in transformed)
    hex_str = joined.encode().hex()
    key = base64.b32encode(bytes.fromhex(hex_str)).decode().rstrip("=")
    # Standard TOTP (RFC 6238): HMAC-SHA1, 30-second window, 6 digits
    counter = int(time.time() // 30)
    msg = struct.pack(">Q", counter)
    h = hmac.new(base64.b32decode(key), msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code = struct.unpack(">I", h[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % 10**6).zfill(6)


def _get_js_pack_url(session: Session) -> str:
    """Extract the web-player JS bundle URL from Spotify's homepage."""
    html = session.get_text("https://open.spotify.com")
    links = [str(link) for link in re.findall(r'src="(https://open\.spotifycdn\.com/cdn/build/[^"]+\.js)"', html)]
    # Prefer desktop web-player, fall back to mobile-web-player
    for link in links:
        if "/web-player/web-player" in link:
            return link
    for link in links:
        if "/mobile-web-player/mobile-web-player" in link:
            return link
    raise RuntimeError("Could not find web-player JS pack URL on Spotify homepage")


def _get_client_version(session: Session) -> str:
    """Extract clientVersion from Spotify's homepage appServerConfig."""
    html = session.get_text("https://open.spotify.com")
    match = re.search(r'<script id="appServerConfig" type="text/plain">([^<]+)</script>', html)
    if not match:
        raise RuntimeError("Could not find appServerConfig on Spotify homepage")
    cfg = json.loads(base64.b64decode(match.group(1)).decode("utf-8"))
    return str(cfg["clientVersion"])


def _get_sha256_hash(session: Session, js_pack_url: str, operation_name: str) -> str:
    """Download Spotify's web-player JS pack and extract the persisted query hash."""
    js = session.get_text(js_pack_url)
    pattern = rf'"{operation_name}","query","([a-f0-9]{{64}})"'
    match = re.search(pattern, js)
    if match:
        h = match.group(1)
        assert isinstance(h, str)
        return h
    # Fallback: try mutation
    pattern = rf'"{operation_name}","mutation","([a-f0-9]{{64}})"'
    match = re.search(pattern, js)
    if match:
        h = match.group(1)
        assert isinstance(h, str)
        return h
    raise RuntimeError(f"Could not find sha256Hash for {operation_name} in JS pack")


_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class SpotifyPublicPlaylist:
    """Fetch metadata + tracks for any public Spotify playlist. No auth required."""

    def __init__(self, session: Session | None = None):
        self._session = session or Session(user_agent=_BROWSER_UA)
        self._access_token: str | None = None
        self._client_token: str | None = None
        self._client_version: str | None = None
        self._sha_hash: str | None = None

    def _authenticate(self) -> None:
        """Run the full auth flow, caching results.

        Uses the cached TOTP secret on first attempt. If the token endpoint
        rejects it, refetches the secret from network and retries once.
        """
        if self._access_token and self._client_token and self._sha_hash:
            return

        # 1. TOTP from obfuscated secret (cached first, refetch on failure)
        version, secret_bytes = _fetch_totp_secret(self._session)

        client_id: str | None = None
        for attempt in (1, 2):
            totp = _generate_totp(version, secret_bytes)

            # 2. Access token
            try:
                data = self._session.get_json(
                    "https://open.spotify.com/api/token",
                    params={
                        "reason": "init",
                        "productType": "web-player",
                        "totp": totp,
                        "totpVer": version,
                        "totpServer": totp,
                    },
                )
                self._access_token = data["accessToken"]
                client_id = str(data["clientId"])
                break
            except Exception:
                if attempt == 1:
                    version, secret_bytes = _refetch_totp_secret(self._session)
                else:
                    raise

        # 3. Client version from homepage
        self._client_version = _get_client_version(self._session)

        # 4. Client token
        data = self._session.post_json(
            "https://clienttoken.spotify.com/v1/clienttoken",
            json_body={
                "client_data": {
                    "client_version": self._client_version,
                    "client_id": client_id,
                    "js_sdk_data": {
                        "device_brand": "unknown",
                        "device_model": "unknown",
                        "os": "windows",
                        "os_version": "NT 10.0",
                        "device_id": "",
                        "device_type": "computer",
                    },
                }
            },
            headers={"Accept": "application/json"},
        )
        self._client_token = data["granted_token"]["token"]

        # 5. Persisted query hash for fetchPlaylist
        js_pack_url = _get_js_pack_url(self._session)
        self._sha_hash = _get_sha256_hash(self._session, js_pack_url, "fetchPlaylist")

    @property
    def _api_headers(self) -> dict[str, str]:
        self._authenticate()
        assert self._client_token is not None
        assert self._client_version is not None
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Client-Token": self._client_token,
            "Spotify-App-Version": self._client_version,
            "Content-Type": "application/json;charset=UTF-8",
            "Accept-Language": "en",
        }

    def _graphql(self, operation: str, variables: dict[str, Any]) -> dict[str, Any]:
        """POST to Spotify's persisted-query GraphQL endpoint."""
        return self._session.post_json(
            "https://api-partner.spotify.com/pathfinder/v1/query",
            params={
                "operationName": operation,
                "variables": json.dumps(variables),
                "extensions": json.dumps(
                    {
                        "persistedQuery": {
                            "version": 1,
                            "sha256Hash": self._sha_hash,
                        }
                    }
                ),
            },
            headers=self._api_headers,
        )

    def get_playlist(self, playlist_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Fetch playlist metadata and full track list.

        Returns (metadata, tracks). Each track is a dict with keys:
        name, id, duration_ms, artists, disc_number, track_number, explicit.
        """
        self._authenticate()

        playlist_id = playlist_id.split("playlist/")[-1].split("?")[0]
        uri = f"spotify:playlist:{playlist_id}"

        # First page
        data = self._graphql(
            "fetchPlaylist",
            {
                "uri": uri,
                "offset": 0,
                "limit": _PAGE_SIZE,
                "enableWatchFeedEntrypoint": False,
            },
        )
        pl = data["data"]["playlistV2"]
        content = pl["content"]
        items = list(content["items"])
        total = content["totalCount"]

        # Paginate remaining
        offset = _PAGE_SIZE
        while offset < total:
            data = self._graphql(
                "fetchPlaylist",
                {
                    "uri": uri,
                    "offset": offset,
                    "limit": _PAGE_SIZE,
                    "enableWatchFeedEntrypoint": False,
                },
            )
            items.extend(data["data"]["playlistV2"]["content"]["items"])
            offset += _PAGE_SIZE

        # Normalise metadata
        owner = pl.get("ownerV2", {}).get("data", {})
        images = pl.get("images", {}).get("items", [])
        if images:
            largest = max(
                images,
                key=lambda i: (
                    (i.get("sources", [{}])[0].get("width", 0) or 0) * (i.get("sources", [{}])[0].get("height", 0) or 0)
                ),
            )
            cover = largest["sources"][0]["url"] if largest.get("sources") else ""
        else:
            cover = ""

        metadata = {
            "playlist_id": playlist_id,
            "name": pl.get("name", ""),
            "description": pl.get("description", ""),
            "owner_name": owner.get("name", ""),
            "owner_uri": owner.get("uri", ""),
            "cover_url": cover,
        }

        # Normalise tracks
        tracks = []
        for item in items:
            v2 = item.get("itemV2", {}).get("data", {})
            v3 = item.get("itemV3", {}).get("data", {})
            if not v2 or not v3:
                continue
            if v2.get("mediaType") != "AUDIO":
                continue
            song_id = v3.get("uri", "").removeprefix("spotify:track:")
            if not song_id:
                continue
            identity = v3.get("identityTrait", {})
            tracks.append(
                {
                    "name": identity.get("name", v2.get("name", "")),
                    "id": song_id,
                    "duration_ms": v2.get("trackDuration", {}).get("totalMilliseconds", 0),
                    "artists": [
                        {"name": a.get("name", ""), "uri": a.get("uri", "")}
                        for a in identity.get("contributors", {}).get("items", [])
                    ],
                    "disc_number": v2.get("discNumber", 1),
                    "track_number": v2.get("trackNumber", 1),
                    "explicit": v2.get("contentRating", {}).get("label") == "EXPLICIT",
                }
            )

        return metadata, tracks
