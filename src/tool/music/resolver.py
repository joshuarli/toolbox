"""Resolve Spotify tracks against the MusicBrainz recording search."""

import re
from difflib import SequenceMatcher
from typing import Any

from tool.music.api.musicbrainz import MB_BASE
from tool.music.http import Session


def _normalize(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    s = s.lower()
    s = re.sub(r"[^\w\s]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _artist_from_credit(credit: list[dict[str, Any]]) -> str:
    names = [e.get("name", "") for e in credit if isinstance(e, dict) and e.get("name")]
    return ", ".join(names)


def _extract_distinctive_words(title: str, artist: str) -> str:
    """Build a plain-text MusicBrainz query from title + artist info.

    For classical works, picks out the movement/section name and performer names.
    For non-classical, uses the full title.
    """
    # Separate main work from movement/section
    main = title
    movement = ""

    # Try splitting on colon (classical convention: Work: Movement)
    if ":" in title:
        parts = title.split(":")
        main = parts[0].strip()
        movement = " ".join(p.strip() for p in parts[1:])

    # Strip catalog numbers from main title for cleaner queries
    main_clean = re.sub(
        r",\s*(Op\.|BWV|K\.|M\.|S\.|D\.|Hob\.|MWV|TH|WAB|FP|CD)\s*\d+[^,]*(,\s*No\.?\s*\d+)?",
        "",
        main,
        flags=re.IGNORECASE,
    )

    # Take distinctive words from movement (skip roman numerals, common words)
    movement_words = []
    if movement:
        for w in movement.split():
            w_clean = w.strip(" -,")
            if (
                len(w_clean) > 2
                and not re.match(r"^(I|II|III|IV|V|VI|VII|VIII|IX|X)+\.?$", w_clean)
                and w_clean.lower() not in {"and", "the", "for", "with", "from", "live"}
            ):
                movement_words.append(w_clean)

    # Use performer names (last 2-3 artists, skipping the composer)
    artist_names = [a.strip() for a in artist.split(",")]
    # Keep only names that look like performers (2+ words or distinctive single word)
    performers = [a for a in artist_names if len(a.split()) >= 2 or len(a) > 5]

    # Build query: main work + movement keywords + key performers
    parts = [main_clean, *movement_words[:4], *performers[:3]]
    # Quote multi-word terms for phrase matching
    query_parts = []
    for p in parts:
        p = p.strip().strip('"')
        if not p:
            continue
        query_parts.append(f'"{p}"' if " " in p else p)

    return " ".join(query_parts)


def _score(result: dict[str, Any], title: str, artist: str, duration_ms: int) -> float:
    """Score a MusicBrainz recording against a Spotify track. Higher = better."""
    score = 0.0
    mb_title = result.get("title", "")
    mb_artist = _artist_from_credit(result.get("artist-credit", []))
    mb_length = result.get("length")

    n_title = _normalize(title)
    n_mb_title = _normalize(mb_title)

    # Title match — use sequence similarity for fuzzy matching
    title_ratio = SequenceMatcher(None, n_title, n_mb_title).ratio()
    if title_ratio >= 0.95:
        score += 60
    elif title_ratio >= 0.75:
        score += 40
    elif title_ratio >= 0.5:
        score += 20
    elif n_title in n_mb_title or n_mb_title in n_title:
        score += 15

    # Artist match — check individual names, not just full-string containment
    n_artist = _normalize(artist)
    n_mb_artist = _normalize(mb_artist)
    spotify_names = set(n_artist.split(", "))
    mb_names = set(n_mb_artist.split(", "))
    common = spotify_names & mb_names
    if common:
        score += min(30, len(common) * 15)
    elif n_artist in n_mb_artist or n_mb_artist in n_artist:
        score += 10

    # Duration proximity
    if mb_length and duration_ms:
        diff = abs(duration_ms - mb_length)
        if diff <= 2000:
            score += 10
        elif diff <= 5000:
            score += 7
        elif diff <= 10000:
            score += 4
        elif diff <= 30000:
            score += 1

    return score


def resolve_track(session: Session, title: str, artist: str, duration_ms: int) -> dict[str, Any] | None:
    """Look up one track. Returns best matching MB recording or None."""
    query = _extract_distinctive_words(title, artist)

    try:
        data = session.get_json(
            f"{MB_BASE}/recording/",
            params={"query": query, "fmt": "json", "limit": "10"},
        )
    except Exception:
        return None

    recordings = data.get("recordings", [])
    if not recordings:
        return None

    best = None
    best_score = 0.0
    for rec in recordings:
        s = _score(rec, title, artist, duration_ms)
        if s > best_score:
            best_score = s
            best = rec

    if best is None or best_score < 30:
        return None

    return {
        "id": best.get("id"),
        "title": best.get("title"),
        "artist-credit": best.get("artist-credit"),
        "length": best.get("length"),
        "score": best_score,
        "disambiguation": best.get("disambiguation", ""),
    }


def resolve_tracks(
    session: Session,
    tracks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve all tracks. Returns (resolved, unresolved)."""
    resolved = []
    unresolved = []

    for i, t in enumerate(tracks):
        artist = ", ".join(a.get("name", "?") for a in t.get("artists", []))
        title = t.get("name", "")
        duration_ms = t.get("duration_ms", 0)

        mb = resolve_track(session, title, artist, duration_ms)

        entry = {
            "spotify_id": t.get("id"),
            "title": title,
            "artist": artist,
            "duration_ms": duration_ms,
        }

        if mb:
            resolved.append({**entry, "mb_recording": mb})
        else:
            unresolved.append(entry)

        label = f"{mb['id']}" if mb else "unresolved"
        print(f"  [{i + 1}/{len(tracks)}] {title[:60]} — {label}")

    return resolved, unresolved
