"""Cover Art Archive frontend: exact release first, release-group fallback."""

from dataclasses import dataclass
from uuid import UUID

from urllib3.exceptions import HTTPError
from urllib3.util.retry import Retry

from tool.music.http import RateLimiter, Session

_BASE = "https://coverartarchive.org"
_MAX_BYTES = 10 * 1024 * 1024
_SIZES = (1200, 500, 250)
_RETRIES = Retry(
    total=2,
    status_forcelist=(429, 500, 502, 503, 504),
    backoff_factor=0.5,
    raise_on_status=False,
)
_session = Session(
    user_agent="toolbox-music/0.1 (https://github.com/joshuarli/toolbox)",
    rate_limiter=RateLimiter(rate=2.0),
)


class CoverArtError(RuntimeError):
    """A cover art lookup failed for reasons other than missing artwork."""


@dataclass(frozen=True)
class Cover:
    data: bytes
    mime: str
    extension: str
    source: str


def _uuid(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise CoverArtError(f"invalid MusicBrainz ID: {value}") from exc


def _get(url: str) -> bytes | None:
    try:
        _session._wait()
        response = _session.pool.request(
            "GET", url, headers=_session.headers, retries=_RETRIES, preload_content=False
        )
        try:
            if response.status == 404:
                return None
            if response.status != 200:
                raise CoverArtError(f"Cover Art Archive returned HTTP {response.status}: {url}")
            data = response.read(_MAX_BYTES + 1)
            if len(data) > _MAX_BYTES:
                raise CoverArtError(f"image exceeds {_MAX_BYTES // (1024 * 1024)} MiB: {url}")
            return data
        finally:
            response.close()
    except (HTTPError, OSError) as exc:
        raise CoverArtError(f"Cover Art Archive request failed: {url}") from exc


def fetch_cover(
    release_id: str | None,
    group_id: str | None,
    *,
    size: int = 1200,
    group_fallback: bool = True,
) -> Cover | None:
    """Fetch a JPEG/PNG front cover or None for a genuine 404 from every candidate."""
    if size not in _SIZES:
        raise ValueError(f"unsupported cover size: {size}")
    release = _uuid(release_id)
    group = _uuid(group_id) if group_fallback else None
    attempts = [("release", release)] if release else []
    if group:
        attempts.append(("release-group", group))

    for kind, mbid in attempts:
        for dimension in _SIZES[_SIZES.index(size) :]:
            url = f"{_BASE}/{kind}/{mbid}/front-{dimension}"
            data = _get(url)
            if data is None:
                continue
            if data.startswith(b"\xff\xd8\xff"):
                return Cover(data, "image/jpeg", "jpg", kind)
            if data.startswith(b"\x89PNG\r\n\x1a\n"):
                return Cover(data, "image/png", "png", kind)
            raise CoverArtError(f"Cover Art Archive returned non-image data: {url}")
    return None
