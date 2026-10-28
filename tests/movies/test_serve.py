import json
import sqlite3
import threading
import urllib.request
import zlib
from http.server import HTTPServer

from tool.movies import serve
from tool.movies.serve import Handler


def _blob(obj) -> bytes:
    c = zlib.compressobj(wbits=-15)
    return c.compress(json.dumps(obj).encode()) + c.flush()


def _make_db(path) -> None:
    db = sqlite3.connect(path)
    db.execute(
        """CREATE TABLE movie (slug TEXT PRIMARY KEY, title TEXT, alt_title TEXT, year TEXT,
        directors BLOB, cast BLOB, synopsis BLOB, rating REAL, rating_count INTEGER,
        rating_dist BLOB, fans INTEGER, watched_by INTEGER, liked_by INTEGER,
        imdb_id TEXT, tmdb_id TEXT, studios BLOB, crew BLOB, backdrop_url TEXT,
        poster_url TEXT, enriched_at INTEGER, backdrop_local TEXT, poster_local TEXT)"""
    )
    db.execute(
        "INSERT INTO movie VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "disorder-2015",
            "Disorder",
            "Maryland",
            "2015",
            _blob(["alice-winocour-1"]),
            _blob(["matthias-schoenaerts-1"]),
            _blob(["A bodyguard."]),
            3.2,
            4214,
            _blob([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]),
            100,
            5000,
            200,
            "tt1234567",
            "12345",
            _blob([]),
            _blob([{"slug": "alice-winocour-1", "role": "Writer"}]),
            "https://example.com/backdrop.jpg",
            "https://example.com/poster.jpg",
            1,
            None,
            None,
        ),
    )
    db.execute(
        "INSERT INTO movie (slug, title, enriched_at, liked_by) VALUES (?,?,?,?)",
        ("unscraped-2020", "Unscraped", None, 5),
    )
    db.commit()
    db.close()


def _live_server(tmp_path):
    db_path = tmp_path / "letterboxd.db"
    img_dir = tmp_path / "static" / "img"
    img_dir.mkdir(parents=True)
    _make_db(db_path)
    serve.configure(db_path, img_dir)
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _get(base, path):
    with urllib.request.urlopen(base + path) as resp:
        return resp.status, resp.read()


def test_index_counts_only_enriched(tmp_path):
    server, base = _live_server(tmp_path)
    try:
        status, body = _get(base, "/")
        assert status == 200
        assert "1 films" in body.decode()
    finally:
        server.shutdown()
        server.server_close()


def test_api_movies_and_film_page(tmp_path):
    server, base = _live_server(tmp_path)
    try:
        status, body = _get(base, "/api/movies?offset=0")
        assert status == 200
        cards = json.loads(body)
        assert [c["id"] for c in cards] == ["disorder-2015"]

        status, body = _get(base, "/disorder-2015/")
        assert status == 200
        html = body.decode()
        assert "Disorder" in html
        assert "Alice Winocour" in html
    finally:
        server.shutdown()
        server.server_close()


def test_api_search_and_person(tmp_path):
    server, base = _live_server(tmp_path)
    try:
        status, body = _get(base, "/api/search?q=disorder")
        assert status == 200
        assert [c["id"] for c in json.loads(body)] == ["disorder-2015"]

        status, body = _get(base, "/api/person?q=alice-winocour-1")
        assert status == 200
        cards = json.loads(body)
        assert len(cards) == 1
        assert cards[0]["roles"] == ["Director", "Writer"]
    finally:
        server.shutdown()
        server.server_close()
