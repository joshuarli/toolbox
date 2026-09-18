import sqlite3

from tool.movies import scraper


def test_slug_from_uri():
    assert scraper.slug_from_uri("https://letterboxd.com/film/parasite-2019/") == "parasite-2019"
    assert scraper.slug_from_uri("https://letterboxd.com/film/parasite-2019") == "parasite-2019"
    assert scraper.slug_from_uri("http://letterboxd.com/film/the-traveler-1974-1/?foo=bar") == "the-traveler-1974-1"
    assert scraper.slug_from_uri("https://boxd.it/28E2") is None
    assert scraper.slug_from_uri("not a url") is None


def test_seed_inserts_unenriched_slugs(monkeypatch, tmp_path):
    monkeypatch.setenv("MOVIES_DATA_DIR", str(tmp_path / "data"))
    csv_path = tmp_path / "diary.csv"
    csv_path.write_text(
        "Date,Name,Year,Letterboxd URI\n"
        "2021-12-25,Spider-Man 2,2004,https://letterboxd.com/film/spider-man-2-2004/\n"
        "2022-04-19,Fresh,2022,https://letterboxd.com/film/fresh-2022/\n"
    )

    scraper.seed(csv_path)
    db = sqlite3.connect(tmp_path / "data" / "letterboxd.db")
    rows = db.execute("SELECT slug, enriched_at FROM movie ORDER BY slug").fetchall()
    db.close()
    assert [r[0] for r in rows] == ["fresh-2022", "spider-man-2-2004"]
    assert all(r[1] is None for r in rows)

    # Re-seeding is idempotent
    scraper.seed(csv_path)
    db = sqlite3.connect(tmp_path / "data" / "letterboxd.db")
    count = db.execute("SELECT COUNT(*) FROM movie").fetchone()[0]
    db.close()
    assert count == 2


def test_seed_rejects_csv_without_uri_column(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MOVIES_DATA_DIR", str(tmp_path / "data"))
    csv_path = tmp_path / "other.csv"
    csv_path.write_text("Name,Year\nFresh,2022\n")

    scraper.seed(csv_path)
    assert "No URI column" in capsys.readouterr().out


def test_init_db_creates_person_frontier(monkeypatch, tmp_path):
    monkeypatch.setenv("MOVIES_DATA_DIR", str(tmp_path / "data"))
    db = scraper.init_db()
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"movie", "person"} <= tables
    db.close()


def test_bootstrap_seeds_empty_db_once(monkeypatch, tmp_path):
    monkeypatch.setenv("MOVIES_DATA_DIR", str(tmp_path / "data"))
    db = scraper.init_db()
    assert scraper.bootstrap_seeds(db) == len(scraper.BOOTSTRAP_SLUGS)
    assert scraper.bootstrap_seeds(db) == 0
    db.close()
