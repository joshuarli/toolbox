import json
import math
import re
import sqlite3
import zlib
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, TypedDict, cast

from tool.movies.paths import db_path

VIDEO_EXTS = frozenset({".mkv", ".mp4", ".avi", ".webm", ".mov", ".m4v", ".mpg", ".mpeg", ".wmv"})
SUBTITLE_EXTS = frozenset({".srt", ".sub", ".ass", ".ssa", ".vtt", ".smi", ".idx"})
ALL_MEDIA_EXTS = VIDEO_EXTS | SUBTITLE_EXTS


def extract_title_year(stem: str) -> tuple[str, int | None]:
    """Parse a title and year out of a raw filename stem.

    Normalises dot-separated scene names, then uses the year as an anchor:
    everything before it is the title; everything after (codecs, resolution,
    encoder tags, YouTube IDs) is discarded.  Parenthetical director credits
    like "(2009, Huang Weikai)" are stripped because the open paren sits
    immediately before the year anchor.
    """
    # Scene releases use dots as word separators (no spaces present)
    if "." in stem and " " not in stem:
        stem = stem.replace(".", " ")
    m = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", stem)
    if m:
        year = int(m.group(1))
        # Strip any open bracket/paren the year was sitting inside
        title = re.sub(r"\s*[\(\[{][^)\]]*$", "", stem[: m.start()]).strip(" -_.,")
        return title, year
    return stem.strip(), None


def parse_filename(filename: str) -> tuple[str, int | None, str]:
    """Wrap extract_title_year with Path handling to also return the extension."""
    p = Path(filename)
    title, year = extract_title_year(p.stem)
    return title, year, p.suffix.lstrip(".").lower()


def _blob(val) -> list[Any]:
    return json.loads(zlib.decompress(val, wbits=-15).decode()) if val else []


def _name(slug: str) -> str:
    return " ".join(w.capitalize() for w in re.sub(r"-\d+$", "", slug).split("-"))


def _normalize(s: str) -> str:
    # Collapse all Unicode whitespace variants (e.g. \xa0 from DB) to plain spaces
    return re.sub(r"\s+", " ", s).strip().lower()


def _title_sim(query: str, row) -> float:
    """Best title similarity between the query and the row's title + alt_title."""
    q = _normalize(query)
    sim = SequenceMatcher(None, q, _normalize(row["title"] or "")).ratio()
    if row["alt_title"]:
        sim = max(sim, SequenceMatcher(None, q, _normalize(row["alt_title"])).ratio())
    return sim


def _popularity(row) -> float:
    # Bayesian-style: penalise low vote counts, reward both rating and popularity
    r = float(row["rating"] or 0.0)
    n = row["rating_count"] or 0
    return r * math.log10(n + 1)


def _row_year(row) -> int | None:
    # DB stores year as TEXT; coerce to int for comparisons
    try:
        return int(row["year"]) if row["year"] is not None else None
    except ValueError, TypeError:
        return None


def _rank_score(row, query_title: str, query_year: int | None) -> float:
    """Composite score: title similarity is primary, year match is strong secondary,
    popularity is a weak tiebreaker.  Weights chosen so that an exact year match
    can't be overridden by a popular title with a mediocre name similarity."""
    title_sim = _title_sim(query_title, row)
    year_bonus = 1.0 if (query_year and _row_year(row) == query_year) else 0.0
    # Normalise popularity to [0, 1] (log-scale max ~25 for a 5★, 1M-vote film)
    pop = min(_popularity(row), 25.0) / 25.0

    # Word overlap bonus: catches films stored under different English titles
    # that still share several words (e.g. "And Life Goes On" ↔ "Life, and
    # Nothing More…").  Strip punctuation so "Life," and "life" match.
    def _words(s: str) -> set[str]:
        return set(re.sub(r"[^\w\s]", " ", s).lower().split())

    q_words = _words(query_title)
    t_words = _words(row["title"] or "") | _words(row["alt_title"] or "")
    overlap = len(q_words & t_words)
    word_bonus = (overlap / max(len(q_words), 1)) * 0.8
    return title_sim * 3.0 + year_bonus * 5.0 + pop * 0.5 + word_bonus


def match_confidence(query_title: str, query_year: int | None, row) -> int:
    """Return 0-100 confidence that row is the correct match for (query_title, query_year).

    Without a year the ceiling is 100 (exact title).  With a year, title accounts
    for 60% and an exact year match accounts for 40%.
    """
    sim = _title_sim(query_title, row)
    if query_year is None:
        return round(sim * 100)
    year_match = _row_year(row) == query_year
    return round(sim * 60 + (40 if year_match else 0))


class ConfidenceBreakdown(TypedDict):
    title: int
    year: bool | None
    pop: int


def confidence_breakdown(query_title: str, query_year: int | None, row) -> ConfidenceBreakdown:
    """Return the individual score components: title %, year bool|None, pop %."""
    sim = _title_sim(query_title, row)
    pop_pct = round(min(_popularity(row), 25.0) / 25.0 * 100)
    if query_year is None:
        return {"title": round(sim * 100), "year": None, "pop": pop_pct}
    return {"title": round(sim * 100), "year": _row_year(row) == query_year, "pop": pop_pct}


def _clean_title(row) -> str:
    """DB title with Unicode whitespace normalised to plain spaces."""
    return re.sub(r"\s+", " ", row["title"] or "").strip()


def _director_full_name(directors_blob) -> str | None:
    """Full display name of the first director, or None if no directors."""
    directors = _blob(directors_blob)
    if not directors:
        return None
    return _name(directors[0])


def _fts5_safe(query: str) -> str:
    """Normalise a query for FTS5 trigram MATCH.

    Strips punctuation, splits apostrophe-contracted words ("L'eternite"
    → "eternite"), and drops single-char tokens because the trigram
    tokeniser cannot match them.
    """
    # Strip punctuation but temporarily keep apostrophe so we can split
    # on it below.  FTS5 uses ' as a string delimiter, so we must remove
    # all apostrophes from the final query string.
    cleaned = re.sub(r"[^\w'\s]", " ", query)
    words: list[str] = []
    for w in cleaned.split():
        # Split on apostrophe: "L'eternite" → ["L", "eternite"]
        # Keep only sub-tokens with >1 char (drops "L", "s", etc.)
        words.extend(part for part in w.split("'") if len(part) > 1)
    return " ".join(words)


def _strip_quotes(s: str) -> str:
    """Strip ASCII and curly apostrophes / single quotes from *s*."""
    return s.replace("'", "").replace("\u2018", "").replace("\u2019", "").replace("\u201b", "")


def _ensure_fts(con: sqlite3.Connection) -> None:
    """Ensure the index and keep the transaction open for a consistent caller query."""
    con.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS movie_fts USING fts5(
            slug UNINDEXED, title, alt_title, directors_text,
            tokenize='trigram'
        )
    """)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "movie" not in tables:
        return

    con.execute("BEGIN IMMEDIATE")
    trigger_names = {
        "movie_fts_dirty_insert",
        "movie_fts_dirty_update",
        "movie_fts_dirty_delete",
    }
    installed_triggers = {
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'movie_fts_dirty_%'"
        )
    }
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS movie_fts_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            dirty INTEGER NOT NULL CHECK (dirty IN (0, 1))
        )
        """
    )
    con.execute("INSERT OR IGNORE INTO movie_fts_state(singleton, dirty) VALUES (1, 1)")
    con.execute(
        """
        CREATE TRIGGER IF NOT EXISTS movie_fts_dirty_insert
        AFTER INSERT ON movie
        BEGIN
            UPDATE movie_fts_state SET dirty = 1 WHERE singleton = 1;
        END
        """
    )
    con.execute(
        """
        CREATE TRIGGER IF NOT EXISTS movie_fts_dirty_update
        AFTER UPDATE OF slug, title, alt_title, directors, enriched_at ON movie
        BEGIN
            UPDATE movie_fts_state SET dirty = 1 WHERE singleton = 1;
        END
        """
    )
    con.execute(
        """
        CREATE TRIGGER IF NOT EXISTS movie_fts_dirty_delete
        AFTER DELETE ON movie
        BEGIN
            UPDATE movie_fts_state SET dirty = 1 WHERE singleton = 1;
        END
        """
    )

    movie_count = con.execute(
        "SELECT COUNT(*) FROM movie WHERE enriched_at IS NOT NULL AND title IS NOT NULL"
    ).fetchone()[0]
    fts_count = con.execute("SELECT COUNT(*) FROM movie_fts").fetchone()[0]
    dirty = con.execute("SELECT dirty FROM movie_fts_state WHERE singleton = 1").fetchone()[0]
    if fts_count != movie_count or dirty or not trigger_names.issubset(installed_triggers):
        print(f"Building FTS index ({movie_count} movies)...", flush=True)
        con.execute("DELETE FROM movie_fts")
        rows = con.execute(
            "SELECT slug, title, alt_title, directors"
            " FROM movie WHERE enriched_at IS NOT NULL AND title IS NOT NULL"
        ).fetchall()
        con.executemany(
            "INSERT INTO movie_fts(slug, title, alt_title, directors_text) VALUES (?,?,?,?)",
            [
                (
                    slug,
                    _strip_quotes(title or ""),
                    _strip_quotes(alt or ""),
                    _strip_quotes(" ".join(_name(s) for s in _blob(directors))),
                )
                for slug, title, alt, directors in rows
            ],
        )
        con.execute("UPDATE movie_fts_state SET dirty = 0 WHERE singleton = 1")
        print("FTS index ready.", flush=True)


def ensure_fts() -> None:
    """Create the movie_fts trigram index if missing, rebuilding it when stale."""
    con = sqlite3.connect(db_path())
    try:
        _ensure_fts(con)
        con.commit()
    finally:
        con.close()


def _search_with_connection(
    con: sqlite3.Connection, query: str, year: int | None, limit: int
) -> list[sqlite3.Row]:
    fts_query = _fts5_safe(query)

    def _execute(fts_expr: str, year_filter: int | None, row_limit: int) -> list[sqlite3.Row]:
        """Run an FTS query; if year_filter is given, pre-filter in SQL
        so year-matched films can never be pushed out of the candidate set
        by BM25 rank alone (fixes e.g. 'Summer' where 634 results bury
        the year-1986 candidates beyond the default limit)."""
        # con.row_factory = sqlite3.Row (set above); fetchall() is typed
        # list[Any] by typeshed since the row type is row_factory-dependent.
        if year_filter is not None:
            return cast(
                "list[sqlite3.Row]",
                con.execute(
                    """
                    SELECT m.slug, m.title, m.alt_title, m.year, m.directors,
                           m.rating, m.rating_count
                    FROM movie_fts fts
                    JOIN movie m ON m.slug = fts.slug
                    WHERE movie_fts MATCH ? AND m.year = ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (fts_expr, year_filter, row_limit),
                ).fetchall(),
            )
        else:
            return cast(
                "list[sqlite3.Row]",
                con.execute(
                    """
                    SELECT m.slug, m.title, m.alt_title, m.year, m.directors,
                           m.rating, m.rating_count
                    FROM movie_fts fts
                    JOIN movie m ON m.slug = fts.slug
                    WHERE movie_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (fts_expr, row_limit),
                ).fetchall(),
            )

    # When year is known, try year-filtered search first so year-matched
    # candidates are guaranteed to appear in the result set.
    rows: list[sqlite3.Row] = []
    if year is not None:
        rows = _execute(fts_query, year, limit * 10)
    if not rows:
        # Wide search without year filter (higher limit for common-word queries)
        rows = _execute(fts_query, None, max(limit * 10, 200))
    # If the strict AND query returns nothing, retry with words OR'd
    if not rows:
        words = fts_query.strip().split()
        if len(words) > 1:
            fts_or = " OR ".join(f'"{w}"' for w in words)
            if year is not None:
                rows = _execute(fts_or, year, limit * 10)
            if not rows:
                rows = _execute(fts_or, None, max(limit * 10, 200))
    # Always supplement FTS results with a LIKE-based substring search.
    # FTS trigram requires exact substring matches, so spelling variants
    # ("Nostalghia" vs "Nostalgia", "eternite" vs "eternity") get no
    # FTS hits.  LIKE catches these with a looser substring test.
    # Also catches films stored under completely different English titles
    # (e.g. "And Life Goes On" → "Life, and Nothing More…").
    # Strip apostrophes and punctuation from query words for LIKE matching
    # (they won't match DB titles like "Eternity" from query "l'eternite")
    raw_words = re.sub(r"[^\w\s]", " ", _normalize(query)).split()
    words = [w for w in raw_words if len(w) >= 4]
    if words:
        longest = max(words, key=len)
        patterns = [f"%{longest}%"]
        # Also try the first 5 chars in case of spelling differences
        # (e.g. "eternite" → "etern" matches "Eternity")
        if len(longest) >= 6:
            patterns.append(f"%{longest[:5]}%")
        for pattern in patterns:
            if year is not None:
                like_rows = con.execute(
                    """
                    SELECT m.slug, m.title, m.alt_title, m.year, m.directors,
                           m.rating, m.rating_count
                    FROM movie m
                    WHERE (m.title LIKE ? OR m.alt_title LIKE ?)
                      AND m.year = ?
                    LIMIT ?
                    """,
                    (pattern, pattern, year, limit * 5),
                ).fetchall()
            else:
                like_rows = con.execute(
                    """
                    SELECT m.slug, m.title, m.alt_title, m.year, m.directors,
                           m.rating, m.rating_count
                    FROM movie m
                    WHERE (m.title LIKE ? OR m.alt_title LIKE ?)
                    LIMIT ?
                    """,
                    (pattern, pattern, limit * 5),
                ).fetchall()
            if like_rows:
                # Merge, deduplicating by slug
                seen = {r["slug"] for r in rows}
                for r in like_rows:
                    if r["slug"] not in seen:
                        rows.append(r)
                        seen.add(r["slug"])
    ranked = sorted(rows, key=lambda r: _rank_score(r, query, year), reverse=True)
    return ranked[:limit]


def search(query: str, year: int | None = None, limit: int = 10) -> list[sqlite3.Row]:
    con = sqlite3.connect(db_path())
    try:
        con.row_factory = sqlite3.Row
        _ensure_fts(con)
        results = _search_with_connection(con, query, year, limit)
        con.commit()
        return results
    finally:
        try:
            if con.in_transaction:
                con.rollback()
        finally:
            con.close()
