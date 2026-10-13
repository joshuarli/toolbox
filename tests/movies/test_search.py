import sqlite3

import pytest

from tool.movies import search as movie_search
from tool.movies.search import extract_title_year, parse_filename


@pytest.mark.parametrize(
    ("stem", "expected_title", "expected_year"),
    [
        # Year in parens, resolution/source junk after
        ("Bumming in Beijing The Last Dreamers (1990) Bluray-576p", "Bumming in Beijing The Last Dreamers", 1990),
        # Year in parens with parenthetical director credit
        ("Disorder (2009, Huang Weikai) [ICdBGTy5g40]-encoded-hq", "Disorder", 2009),
        # Year in parens with director + YouTube ID
        ("Floating (2004, Huang Weikai) [QXprc7TSBkU]", "Floating", 2004),
        # Dot-separated scene name: dots become spaces, year is anchor
        ("Linda.Linda.Linda.2005.1080p.WEBRip.x264.AAC-[YTS.MX]", "Linda Linda Linda", 2005),
        # Year in parens with director name, encoder suffix
        ("The Silent Holy Stones (Pema Tseden, 2005)-encoded-hq", "The Silent Holy Stones", 2005),
        # Dot-separated with encoder group and suffix
        ("The.Traveler.1974.1080p.BluRay.x264-iBEX-encoded-hq", "The Traveler", 1974),
    ],
)
def test_extract_title_year(stem, expected_title, expected_year):
    title, year = extract_title_year(stem)
    assert title == expected_title
    assert year == expected_year


@pytest.mark.parametrize(
    ("filename", "expected_title", "expected_year", "expected_ext"),
    [
        (
            "Bumming in Beijing The Last Dreamers (1990) Bluray-576p.mkv",
            "Bumming in Beijing The Last Dreamers",
            1990,
            "mkv",
        ),
        ("Disorder (2009, Huang Weikai) [ICdBGTy5g40]-encoded-hq.mp4", "Disorder", 2009, "mp4"),
        ("Floating (2004, Huang Weikai) [QXprc7TSBkU].webm", "Floating", 2004, "webm"),
        ("Linda.Linda.Linda.2005.1080p.WEBRip.x264.AAC-[YTS.MX].mp4", "Linda Linda Linda", 2005, "mp4"),
        ("The Silent Holy Stones (Pema Tseden, 2005)-encoded-hq.mp4", "The Silent Holy Stones", 2005, "mp4"),
        ("The.Traveler.1974.1080p.BluRay.x264-iBEX-encoded-hq.mp4", "The Traveler", 1974, "mp4"),
    ],
)
def test_parse_filename(filename, expected_title, expected_year, expected_ext):
    title, year, ext = parse_filename(filename)
    assert title == expected_title
    assert year == expected_year
    assert ext == expected_ext


def test_no_year():
    title, year = extract_title_year("Some Movie Without A Year")
    assert title == "Some Movie Without A Year"
    assert year is None


def test_no_year_parse_filename():
    title, year, ext = parse_filename("Some Movie Without A Year.mkv")
    assert title == "Some Movie Without A Year"
    assert year is None
    assert ext == "mkv"


def test_ensure_fts_refreshes_changed_fields_when_movie_count_is_constant(monkeypatch, tmp_path):
    db = tmp_path / "movies.db"
    con = sqlite3.connect(db)
    con.execute(
        """
        CREATE TABLE movie (
            slug TEXT PRIMARY KEY,
            title TEXT,
            alt_title TEXT,
            year TEXT,
            directors BLOB,
            rating REAL,
            rating_count INTEGER,
            enriched_at INTEGER
        )
        """
    )
    con.execute(
        "INSERT INTO movie VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("quiet-volcano", "Ancient Winter", None, "2001", None, 7.0, 12, 1),
    )
    con.execute(
        "CREATE VIRTUAL TABLE movie_fts USING fts5(slug UNINDEXED, title, alt_title, directors_text, tokenize='trigram')"
    )
    con.execute(
        "INSERT INTO movie_fts VALUES (?, ?, ?, ?)",
        ("quiet-volcano", "Stale indexed title", "", ""),
    )
    con.commit()
    con.close()
    monkeypatch.setattr(movie_search, "db_path", lambda: db)

    movie_search.ensure_fts()
    con = sqlite3.connect(db)
    indexed_title = con.execute("SELECT title FROM movie_fts WHERE slug = ?", ("quiet-volcano",)).fetchone()[0]
    con.close()
    assert indexed_title == "Ancient Winter"

    con = sqlite3.connect(db)
    con.execute("UPDATE movie SET title = ? WHERE slug = ?", ("Quiet Volcano", "quiet-volcano"))
    con.commit()
    con.close()

    movie_search.ensure_fts()

    con = sqlite3.connect(db)
    indexed_title = con.execute("SELECT title FROM movie_fts WHERE slug = ?", ("quiet-volcano",)).fetchone()[0]
    con.close()
    assert indexed_title == "Quiet Volcano"
    assert [row["slug"] for row in movie_search.search("Quiet Volcano")] == ["quiet-volcano"]


def test_search_rolls_back_and_closes_connection_after_query_failure(monkeypatch, tmp_path):
    db = tmp_path / "movies.db"
    connections = []
    state = {"rolled_back": False, "closed": False}
    connect = sqlite3.connect

    class TrackedConnection(sqlite3.Connection):
        def rollback(self):
            state["rolled_back"] = True
            super().rollback()

        def close(self):
            state["closed"] = True
            super().close()

    def tracked_connect(path):
        con = connect(path, factory=TrackedConnection)
        connections.append(con)
        return con

    def fail_after_transaction(con):
        con.execute("BEGIN IMMEDIATE")
        raise RuntimeError("search setup failed")

    monkeypatch.setattr(movie_search, "db_path", lambda: db)
    monkeypatch.setattr(movie_search.sqlite3, "connect", tracked_connect)
    monkeypatch.setattr(movie_search, "_ensure_fts", fail_after_transaction)

    with pytest.raises(RuntimeError, match="search setup failed"):
        movie_search.search("Quiet Volcano")

    assert state == {"rolled_back": True, "closed": True}
    assert len(connections) == 1
