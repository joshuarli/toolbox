"""AcoustID API client — fingerprint audio with fpcalc, look up MusicBrainz
recordings via the AcoustID web service.

Rate-limited to 3 req/sec as recommended by the AcoustID API docs.
API docs: https://acoustid.org/webservice
"""

import json
import os
import subprocess
import sys
from dataclasses import replace
from typing import Any

from tool.music.http import RateLimiter, Session
from tool.music.metadata import Candidate, Metadata

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
    """Queries AcoustID API for MusicBrainz recordings, release groups, releases and tracks."""
    try:
        api_key = os.environ["ACOUSTID_API_KEY"]
    except KeyError:
        print("error: ACOUSTID_API_KEY not set in environment", file=sys.stderr)
        sys.exit(1)

    params = {
        "client": api_key,
        "duration": int(duration),
        "fingerprint": fingerprint,
        "meta": "recordings releasegroups releases tracks",
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


def _date(d: Any) -> str | None:
    """AcoustID dates are {"year": 2020, "month": 10, "day": 23}; keep as much as is present."""
    if not isinstance(d, dict) or not d.get("year"):
        return None
    return "-".join([str(d["year"])] + [f"{d[k]:02d}" for k in ("month", "day") if d.get(k)])


def _credit(artists: list[dict[str, Any]]) -> str | None:
    credit = "".join(str(a["name"]) + (a.get("joinphrase") or ", ") for a in artists if a.get("name"))
    return credit.removesuffix(", ") or None


def candidates(results: list[dict[str, Any]]) -> list[Candidate]:
    """Flatten AcoustID results into one candidate per (recording, release, medium, track).

    The same match can appear under several results; the highest-scoring one is kept.
    """
    best: dict[tuple[str | None, ...], Candidate] = {}
    for result in results:
        score = result.get("score", 0.0)
        for rec in result.get("recordings", []):
            base = Metadata(
                title=rec.get("title"),
                artist=_credit(rec.get("artists", [])),
                musicbrainz_recordingid=rec.get("id"),
                musicbrainz_artistid=[a["id"] for a in rec.get("artists", []) if a.get("id")],
                acoustid_id=result.get("id"),
            )
            # Releases nest under their release group when releasegroups are requested.
            releases = [(rg, rel) for rg in rec.get("releasegroups", []) for rel in rg.get("releases", [])]
            releases += [(None, rel) for rel in rec.get("releases", [])]
            for rg, rel in releases or [(None, None)]:
                for meta in _release_candidates(base, rg, rel):
                    key = (meta.musicbrainz_recordingid, meta.musicbrainz_albumid, meta.musicbrainz_trackid)
                    if key not in best or best[key].score < score:
                        best[key] = Candidate(score, meta)
    return list(best.values())


def _release_candidates(base: Metadata, rg: dict[str, Any] | None, rel: dict[str, Any] | None) -> list[Metadata]:
    if rel is None:
        return [base]
    meta = replace(
        base,
        album=rel.get("title"),
        date=_date(rel.get("date")),
        releasecountry=rel.get("country"),
        totaldiscs=str(rel["medium_count"]) if rel.get("medium_count") else None,
        musicbrainz_albumid=rel.get("id"),
        musicbrainz_releasegroupid=rg.get("id") if rg is not None and rg else None,
        releasetype=[rg["type"].lower()] if rg is not None and rg and rg.get("type") else [],
    )
    out = [
        replace(
            meta,
            media=medium.get("format"),
            discnumber=str(medium["position"]) if medium.get("position") else None,
            totaltracks=str(medium["track_count"]) if medium.get("track_count") else None,
            tracknumber=str(track["position"]) if track.get("position") else None,
            musicbrainz_trackid=track.get("id"),
        )
        for medium in rel.get("mediums", [])
        for track in medium.get("tracks", [])
    ]
    return out or [meta]
