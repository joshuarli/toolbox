"""AcoustID API client — fingerprint audio with fpcalc, look up MusicBrainz
recordings via the AcoustID web service.

Rate-limited to 3 req/sec as recommended by the AcoustID API docs.
API docs: https://acoustid.org/webservice
"""

import json
import os
import subprocess
import sys
from typing import Any

from tool.music.http import RateLimiter, Session

# AcoustID recommends no more than 3 req/sec
_session = Session(user_agent="music-tag/0.1", rate_limiter=RateLimiter(rate=3.0))


def get_audio_fingerprint(file_path):
    try:
        result = subprocess.run(
            ["fpcalc", "-json", file_path],
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(result.stdout)
        return data.get("duration"), data.get("fingerprint")
    except subprocess.CalledProcessError as e:
        print(f"Error executing fpcalc: {e.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print("Error: 'fpcalc' binary not found. Please install chromaprint.", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError:
        print("Error: Failed to parse JSON from fpcalc output.", file=sys.stderr)
        sys.exit(1)


def fetch_acoustid_metadata(duration, fingerprint):
    """Queries AcoustID API for MusicBrainz recordings and releases."""
    try:
        api_key = os.environ["ACOUSTID_API_KEY"]
    except KeyError:
        print("error: ACOUSTID_API_KEY not set in environment", file=sys.stderr)
        sys.exit(1)

    params = {
        "client": api_key,
        "duration": int(duration),
        "fingerprint": fingerprint,
        "meta": "recordings releases tracks",
    }

    try:
        res_data = _session.get_json("https://api.acoustid.org/v2/lookup", params=params)
    except Exception as e:
        print(f"Network or parsing error: {e}", file=sys.stderr)
        sys.exit(1)

    if res_data.get("status") != "ok":
        error_msg = res_data.get("error", {}).get("message", "Unknown API error")
        print(f"AcoustID API Error: {error_msg}", file=sys.stderr)
        return None

    return res_data.get("results", [])


def extract_metadata(result: dict[str, Any]) -> dict[str, str]:
    """Pull title, artist, album, date, track number from one AcoustID result.

    Returns a dict with only the keys that could be extracted.
    """
    meta: dict[str, str] = {}
    recordings = result.get("recordings")
    if not recordings:
        return meta

    rec = recordings[0]
    rec_id = rec.get("id")

    if "title" in rec:
        meta["title"] = rec["title"]

    artists = rec.get("artists")
    if artists:
        meta["artist"] = ", ".join(a["name"] for a in artists if a.get("name"))

    releases = rec.get("releases")
    if releases:
        rel = releases[0]
        if "title" in rel:
            meta["album"] = rel["title"]
        date = rel.get("date", {})
        year = date.get("year") if isinstance(date, dict) else None
        if year:
            meta["date"] = str(year)

        # Track number — search media within releases for a track matching this recording
        if rec_id:
            for rel in releases:
                for medium in rel.get("media", []):
                    for track in medium.get("tracks", []):
                        if track.get("id") == rec_id:
                            tn = track.get("position")
                            if tn:
                                meta["tracknumber"] = str(tn)
                            break
                    if "tracknumber" in meta:
                        break
                if "tracknumber" in meta:
                    break

    return meta
