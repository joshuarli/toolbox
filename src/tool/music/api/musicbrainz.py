"""MusicBrainz API client — search recordings, and enrich an exact recording/release
selection into full `Metadata` with a single lookup.

Rate-limited to 1 req/sec as required by the MusicBrainz API terms.
API docs: https://musicbrainz.org/doc/MusicBrainz_API
"""

import sys
from collections import Counter
from dataclasses import replace
from typing import Any

from tool.music.http import RateLimiter, Session
from tool.music.metadata import Candidate, Metadata

MB_BASE = "https://musicbrainz.org/ws/2"
USER_AGENT = "music-tag/0.1"

# MusicBrainz requires 1 req/sec
_session = Session(user_agent=USER_AGENT, rate_limiter=RateLimiter(rate=1.0))

# One release lookup returns every track with its recording, ISRCs, genres, artist credits and
# (recording- and work-level) relationships, so enriching a selection costs a single request.
_RELEASE_INC = (
    "artist-credits labels recordings release-groups isrcs genres artist-rels work-rels "
    "recording-level-rels work-level-rels"
)
_RECORDING_INC = "artist-credits isrcs genres artist-rels work-rels work-level-rels"

_MAX_GENRES = 5
# MusicBrainz relationship type -> Metadata field, for artist relations on recordings and works.
_CREDIT_FIELDS = {
    "composer": "composer",
    "lyricist": "lyricist",
    "writer": "writer",
    "arranger": "arranger",
    "producer": "producer",
    "conductor": "conductor",
    "remixer": "remixer",
}
_PERFORMER_TYPES = {"instrument", "vocal", "performer"}


# ---------------------------------------------------------------------------
# Search → candidates
# ---------------------------------------------------------------------------


def search(query: str) -> list[Candidate]:
    """Search MusicBrainz recordings; one candidate per (recording, release)."""
    try:
        data = _session.get_json(
            f"{MB_BASE}/recording/",
            params={"query": query, "fmt": "json", "limit": "10"},
        )
    except Exception:
        return []

    out: list[Candidate] = []
    for rec in data.get("recordings", []):
        if not rec.get("title"):
            continue
        base = Metadata(
            title=rec["title"],
            artist=_join_credit(rec.get("artist-credit", []), "name"),
            musicbrainz_recordingid=rec.get("id"),
            musicbrainz_artistid=_artist_ids(rec.get("artist-credit", [])),
        )
        score = rec.get("score", 100) / 100.0
        for rel in rec.get("releases", []):
            out.extend(Candidate(score, m) for m in _search_release(base, rel))
        if not rec.get("releases"):
            out.append(Candidate(score, base))
    return out


def _search_release(base: Metadata, rel: dict[str, Any]) -> list[Metadata]:
    rg = rel.get("release-group", {})
    # Search results list only the matching medium, so the disc total is unknown until enrichment.
    meta = replace(
        base,
        album=rel.get("title"),
        date=rel.get("date"),
        releasecountry=rel.get("country"),
        releasestatus=rel.get("status"),
        musicbrainz_albumid=rel.get("id"),
        musicbrainz_releasegroupid=rg.get("id"),
        releasetype=_release_types(rg),
    )
    out = []
    for medium in rel.get("media", []):
        for track in medium.get("track", []):
            # Search results give the 0-based offset of the matched track within the medium.
            pos = medium.get("track-offset")
            out.append(
                replace(
                    meta,
                    media=medium.get("format"),
                    discnumber=str(medium["position"]) if medium.get("position") else None,
                    totaltracks=str(medium["track-count"]) if medium.get("track-count") else None,
                    tracknumber=str(pos + 1) if pos is not None else None,
                    musicbrainz_trackid=track.get("id"),
                )
            )
    return out or [meta]


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------


def enrich(meta: Metadata) -> Metadata:
    """Fill in the canonical MusicBrainz metadata for the selected *meta*.

    Looks up the exact release when one was selected (one request), otherwise the bare
    recording.  On failure the selection is returned as-is, with a warning.
    """
    try:
        if meta.musicbrainz_albumid:
            return _enrich_release(meta, _release_lookup(meta.musicbrainz_albumid))
        if meta.musicbrainz_recordingid:
            rec = _session.get_json(
                f"{MB_BASE}/recording/{meta.musicbrainz_recordingid}",
                params={"inc": _RECORDING_INC, "fmt": "json"},
            )
            return _apply_recording(meta, rec, [])
    except Exception as e:
        print(f"warning: MusicBrainz lookup failed ({e}); keeping AcoustID data only", file=sys.stderr)
    return meta


def _release_lookup(release_id: str) -> dict[str, Any]:
    return _session.get_json(f"{MB_BASE}/release/{release_id}", params={"inc": _RELEASE_INC, "fmt": "json"})


def _enrich_release(meta: Metadata, rel: dict[str, Any]) -> Metadata:
    rg = rel.get("release-group", {})
    labels = rel.get("label-info", [])
    out = replace(
        meta,
        album=rel.get("title") or meta.album,
        albumartist=_join_credit(rel.get("artist-credit", []), "name") or meta.albumartist,
        albumartistsort=_join_credit(rel.get("artist-credit", []), "sort-name"),
        musicbrainz_albumartistid=_artist_ids(rel.get("artist-credit", [])),
        date=rel.get("date") or meta.date,
        originaldate=rg.get("first-release-date") or None,
        releasecountry=_country(rel) or meta.releasecountry,
        releasestatus=rel.get("status") or meta.releasestatus,
        releasetype=_release_types(rg) or meta.releasetype,
        barcode=rel.get("barcode") or None,
        label=_unique(li["label"]["name"] for li in labels if li.get("label")),
        catalognumber=_unique(li["catalog-number"] for li in labels if li.get("catalog-number")),
        totaldiscs=str(len(rel["media"])) if rel.get("media") else meta.totaldiscs,
        musicbrainz_albumid=rel["id"],
        musicbrainz_releasegroupid=rg.get("id") or meta.musicbrainz_releasegroupid,
    )

    found = _find_track(rel, meta)
    if not found:
        print("warning: recording not found on the selected release; track details skipped", file=sys.stderr)
        return out
    medium, track = found
    out = replace(
        out,
        media=medium.get("format") or meta.media,
        discnumber=str(medium["position"]),
        totaltracks=str(medium["track-count"]) if medium.get("track-count") else meta.totaltracks,
        tracknumber=str(track["position"]),
        musicbrainz_trackid=track["id"],
        artist=_join_credit(track.get("artist-credit", []), "name") or out.artist,
        artistsort=_join_credit(track.get("artist-credit", []), "sort-name") or out.artistsort,
        artists=[c["name"] for c in track.get("artist-credit", []) if c.get("name")] or out.artists,
        musicbrainz_artistid=_artist_ids(track.get("artist-credit", [])) or out.musicbrainz_artistid,
    )
    # Release and release-group genres are folksonomy-level, recordings add their own.
    return _apply_recording(out, track["recording"], rel.get("genres", []) + rg.get("genres", []))


def _country(rel: dict[str, Any]) -> str | None:
    """Release country, falling back to the first release event with a country area."""
    if rel.get("country"):
        return str(rel["country"])
    for event in rel.get("release-events", []):
        if codes := (event.get("area") or {}).get("iso-3166-1-codes"):
            return str(codes[0])
    return None


def _find_track(rel: dict[str, Any], meta: Metadata) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The selected track on *rel*: by release-track MBID, else by recording (and position if ambiguous)."""
    tracks = [(m, t) for m in rel.get("media", []) for t in m.get("tracks", [])]
    for matches in (
        [mt for mt in tracks if meta.musicbrainz_trackid and mt[1]["id"] == meta.musicbrainz_trackid],
        [mt for mt in tracks if mt[1]["recording"]["id"] == meta.musicbrainz_recordingid],
    ):
        if len(matches) > 1:
            wanted = (meta.discnumber, meta.tracknumber)
            matches = [mt for mt in matches if (str(mt[0]["position"]), str(mt[1]["position"])) == wanted] or matches
        if matches:
            return matches[0]
    return None


def _apply_recording(meta: Metadata, rec: dict[str, Any], extra_genres: list[dict[str, Any]]) -> Metadata:
    """Apply recording-level data: ISRCs, genres, credits, works.  Also canonicalises the
    recording MBID and, for a bare-recording lookup, the title and artist credit."""
    genres: Counter[str] = Counter()
    for g in rec.get("genres", []) + extra_genres:
        genres[g["name"]] += g.get("count", 0)

    by_field: dict[str, list[str]] = {}
    performers: list[str] = []
    works: list[str] = []
    for rel in rec.get("relations", []):
        if rel["target-type"] == "artist":
            _apply_artist_relation(rel, by_field, performers)
        elif rel["target-type"] == "work" and rel["type"] == "performance":
            works.append(rel["work"]["id"])
            for wrel in rel["work"].get("relations", []):
                if wrel["target-type"] == "artist":
                    _apply_artist_relation(wrel, by_field, performers)

    out = replace(
        meta,
        title=rec.get("title") or meta.title,
        musicbrainz_recordingid=rec["id"],
        isrc=_unique(rec.get("isrcs", [])),
        genre=[name for name, _ in sorted(genres.items(), key=lambda kv: (-kv[1], kv[0]))[:_MAX_GENRES]],
        musicbrainz_workid=_unique(works),
        performer=_unique(performers),
        **{field: _unique(names) for field, names in by_field.items()},
    )
    if not meta.musicbrainz_trackid and rec.get("artist-credit"):  # bare recording: no track credit available
        out = replace(
            out,
            artist=_join_credit(rec["artist-credit"], "name"),
            artistsort=_join_credit(rec["artist-credit"], "sort-name"),
            artists=[c["name"] for c in rec["artist-credit"] if c.get("name")],
            musicbrainz_artistid=_artist_ids(rec["artist-credit"]),
        )
    return out


def _apply_artist_relation(rel: dict[str, Any], by_field: dict[str, list[str]], performers: list[str]) -> None:
    name = rel["artist"]["name"]
    if rel["type"] in _CREDIT_FIELDS:
        by_field.setdefault(_CREDIT_FIELDS[rel["type"]], []).append(name)
    elif rel["type"] in _PERFORMER_TYPES:
        what = ", ".join(rel.get("attributes", []))
        performers.append(f"{name} ({what})" if what else name)


def _join_credit(credit: list[dict[str, Any]], key: str) -> str | None:
    """Join an artist-credit list into its printed form ("A feat. B").  *key* is `name`
    (the credited name) or `sort-name` (the artist's sort name)."""
    parts: list[str] = []
    for c in credit:
        name = c.get("name") if key == "name" else c.get("artist", {}).get(key)
        if name:
            parts.append(name + c.get("joinphrase", ""))
    return "".join(parts) or None


def _artist_ids(credit: list[dict[str, Any]]) -> list[str]:
    return _unique(c["artist"]["id"] for c in credit if c.get("artist", {}).get("id"))


def _release_types(rg: dict[str, Any]) -> list[str]:
    return [t.lower() for t in [rg.get("primary-type"), *rg.get("secondary-types", [])] if t]


def _unique(values) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))
