"""MusicBrainz API client — query recordings, fetch genre/artist/track metadata.

Rate-limited to 1 req/sec as required by the MusicBrainz API terms.
API docs: https://musicbrainz.org/doc/MusicBrainz_API
"""

from typing import Any

from tool.music.http import RateLimiter, Session

MB_BASE = "https://musicbrainz.org/ws/2"
USER_AGENT = "music-tag/0.1"

# MusicBrainz requires 1 req/sec
_session = Session(user_agent=USER_AGENT, rate_limiter=RateLimiter(rate=1.0))


def _recording_to_result(recording: dict[str, Any]) -> dict[str, Any]:
    """Convert a MusicBrainz search recording to AcoustID-compatible result format."""
    artist_credit = recording.get("artist-credit", [])
    artists = [{"name": a["name"]} for a in artist_credit if a.get("name")]

    releases = recording.get("releases", [])
    mb_releases = []
    for rel in releases:
        r: dict[str, Any] = {}
        if rel.get("title"):
            r["title"] = rel["title"]
        date = rel.get("date")
        if date:
            # MB dates are strings like "2024" or "2024-05"
            year = date.split("-")[0] if isinstance(date, str) else None
            if year:
                r["date"] = {"year": int(year)}
        # Pass through media/tracks so extract_metadata can find track numbers.
        # Normalize MB track format (track.id=track MBID, track.recording.id=recording MBID)
        # to AcoustID format (track.id=recording MBID).
        media = rel.get("media")
        if media:
            r["media"] = [
                {
                    **m,
                    "tracks": [{**t, "id": t.get("recording", {}).get("id", t.get("id"))} for t in m.get("tracks", [])],
                }
                for m in media
            ]
        if r:
            mb_releases.append(r)

    score = recording.get("score", 100) / 100.0

    return {
        "score": score,
        "recordings": [
            {
                "id": recording.get("id"),
                "title": recording.get("title"),
                "artists": artists,
                "releases": mb_releases,
            }
        ],
    }


def search(query: str) -> list[dict[str, Any]]:
    """Search MusicBrainz recordings and return results in AcoustID-compatible format."""
    try:
        data = _session.get_json(
            f"{MB_BASE}/recording/",
            params={"query": query, "fmt": "json", "limit": "10"},
        )
    except Exception:
        return []

    recordings = data.get("recordings", [])
    return [_recording_to_result(r) for r in recordings if r.get("title")]


def fetch_recording(recording_mbid: str) -> dict[str, str]:
    """Query MusicBrainz API for track number, genre, and album artist.

    Returns a dict with only the keys that could be extracted.
    """
    meta: dict[str, str] = {}
    try:
        data = _session.get_json(
            f"{MB_BASE}/recording/{recording_mbid}",
            params={"inc": "genres artists releases media", "fmt": "json"},
        )
    except Exception:
        return meta

    # Genre — take top 3 by count
    genres = data.get("genres", [])
    if genres:
        top = sorted(genres, key=lambda g: g.get("count", 0), reverse=True)
        meta["genre"] = ", ".join(g["name"] for g in top[:3] if g.get("name"))

    # Track number — search releases for a track matching this recording.
    # Recording lookups omit the track.recording sub-object (it's the looked-up
    # recording), so a missing recording key counts as a match.
    releases = data.get("releases", [])
    for rel in releases:
        for medium in rel.get("media", []):
            for track in medium.get("tracks", []):
                track_rec = track.get("recording")
                if track_rec is None or track_rec.get("id") == recording_mbid:
                    tn = track.get("number")
                    if tn:
                        meta["tracknumber"] = str(tn)
                    break
            if "tracknumber" in meta:
                break
        if "tracknumber" in meta:
            break

    # Album artist — from the first release's artist-credit
    if releases:
        ac = releases[0].get("artist-credit", [])
        if ac:
            meta["albumartist"] = "".join(a.get("name", "") + a.get("joinphrase", "") for a in ac)

    return meta
