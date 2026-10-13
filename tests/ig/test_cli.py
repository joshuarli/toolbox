"""Tests for CLI routing and command helpers."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import pytest

import tool.ig.cli as cli
from tool.ig.browser import BrowserInUseError
from tool.ig.cli import (
    _fingerprint,
    _fingerprint_warnings,
    _get,
    _get_following,
)
from tool.ig.cli.following import _dump_following
from tool.ig.cli.get_post import _get_post, shortid_arg
from tool.ig.cli.import_db import ImportDbConflictError, ImportDbError, import_db
from tool.ig.cli.info import _info
from tool.ig.cli.remove import _remove
from tool.ig.cli.scan import _scan
from tool.ig.cli.update import _update
from tool.ig.db import init_db
from tool.ig.following import Followee, save_following
from tool.ig.post import RateLimitError
from tool.ig.profile_scan import DEFAULT_SAVED_PROFILE, init_profile_table


@pytest.mark.asyncio
async def test_main_routes_get(monkeypatch):
    seen = {}

    async def fake_get(urls, **_kwargs):
        seen["urls"] = urls

    monkeypatch.setattr(cli, "_get", fake_get)
    monkeypatch.setattr("sys.argv", ["ig", "get", "@alice"])

    await cli.main()
    assert seen == {"urls": ["@alice"]}


@pytest.mark.asyncio
async def test_main_routes_update_rescans_then_gets(monkeypatch):
    calls = []

    async def fake_scan(urls, **kwargs):
        calls.append(("scan", urls, kwargs))

    async def fake_get(urls, **kwargs):
        calls.append(("get", urls, kwargs))

    monkeypatch.setattr("tool.ig.cli.update._scan", fake_scan)
    monkeypatch.setattr("tool.ig.cli.update._get", fake_get)
    monkeypatch.setattr("sys.argv", ["ig", "update", "--live", "-j", "3"])

    await cli.main()
    assert [name for name, _urls, _kwargs in calls] == ["scan", "scan", "get"]
    normal_scan_urls, saved_scan_urls, get_urls = (call[1] for call in calls)
    assert normal_scan_urls == []
    assert saved_scan_urls == []
    assert get_urls == []
    normal_scan_kwargs, saved_scan_kwargs, get_kwargs = (call[2] for call in calls)
    assert normal_scan_kwargs["saved"] is False
    assert saved_scan_kwargs["saved"] is True
    assert normal_scan_kwargs["live"] is True
    assert saved_scan_kwargs["live"] is True
    assert get_kwargs["jobs"] == 3
    assert get_kwargs["saved"] is False


@pytest.mark.asyncio
async def test_update_rescans_only_saved_profiles_still_in_the_database(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    db = init_db("instagram.db")
    init_profile_table(db)
    db.executescript(
        """
        INSERT INTO post VALUES ('SAVED1', NULL, NULL, NULL, NULL);
        INSERT INTO profile_post VALUES ('teaandvessel', 1, 'SAVED1', 1, 0);
        """
    )
    db.commit()
    db.close()

    scans = []

    async def fake_scan_profiles(identifiers, *_args, **kwargs):
        scans.append((identifiers, kwargs["saved"]))

    async def fake_get(*_args, **_kwargs):
        return

    monkeypatch.setattr("tool.ig.cli.scan.scan_profiles", fake_scan_profiles)
    monkeypatch.setattr("tool.ig.cli.update._get", fake_get)

    await _update()

    assert scans == [(["teaandvessel"], True)]
    assert "no profiles scanned yet" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_update_starts_downloads_when_a_scan_phase_fails(monkeypatch, capsys):
    calls = []

    async def fake_scan(_urls, **kwargs):
        calls.append(("scan", kwargs["saved"]))
        if not kwargs["saved"]:
            raise RuntimeError("profile grid did not render")

    async def fake_get(_urls, **_kwargs):
        calls.append(("get", None))

    monkeypatch.setattr("tool.ig.cli.update._scan", fake_scan)
    monkeypatch.setattr("tool.ig.cli.update._get", fake_get)

    await _update()

    assert calls == [("scan", False), ("scan", True), ("get", None)]
    assert "update: profile scan phase failed; continuing to downloads" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_main_routes_get_post(monkeypatch):
    seen = {}

    async def fake_get_post(target, **_kwargs):
        seen["target"] = target

    monkeypatch.setattr(cli, "_get_post", fake_get_post)
    monkeypatch.setattr("sys.argv", ["ig", "get-post", "Db706nYIUZk"])

    await cli.main()
    assert seen == {"target": "Db706nYIUZk"}


def test_get_post_shortid_arg_accepts_shortid_and_urls():
    assert shortid_arg("Db706nYIUZk") == "Db706nYIUZk"
    assert shortid_arg("https://www.instagram.com/p/Db706nYIUZk/") == "Db706nYIUZk"
    assert shortid_arg("https://www.instagram.com/reel/Db706nYIUZk/") == "Db706nYIUZk"


def test_get_post_shortid_arg_rejects_profiles_and_bare_words():
    with pytest.raises(ValueError, match="get-post expects a post shortid or /p/<shortid>/ URL"):
        shortid_arg("@alice")
    with pytest.raises(ValueError, match="get-post expects a post shortid or /p/<shortid>/ URL"):
        shortid_arg("https://www.instagram.com/alice/")


@pytest.mark.asyncio
async def test_get_post_invokes_scrape_post(monkeypatch):
    db = MagicMock()
    calls = []

    async def fake_scrape_post(shortid, db_conn, **_kwargs):
        calls.append((shortid, db_conn, _kwargs["live"], str(_kwargs["media_dir"])))

    monkeypatch.setattr("tool.ig.cli.get_post.init_db", lambda: db)
    monkeypatch.setattr("tool.ig.cli.get_post.scrape_post", fake_scrape_post)

    await _get_post("https://www.instagram.com/p/Db706nYIUZk/", live=True, media_dir="site/images")

    assert calls == [("Db706nYIUZk", db, True, "site/images")]
    db.close.assert_called_once()


@pytest.mark.asyncio
async def test_get_post_reports_rate_limit(monkeypatch, capsys):
    db = MagicMock()

    def fake_scrape_post(shortid, _db_conn, **_kwargs):
        raise RateLimitError(shortid, retry_after=120)

    monkeypatch.setattr("tool.ig.cli.get_post.init_db", lambda: db)
    monkeypatch.setattr("tool.ig.cli.get_post.scrape_post", fake_scrape_post)

    with pytest.raises(SystemExit) as exc:
        await _get_post("Db706nYIUZk")

    assert exc.value.code == 1
    assert "rate-limited fetching Db706nYIUZk (retry after 2.0 minutes)" in capsys.readouterr().err
    db.close.assert_called_once()


@pytest.mark.asyncio
async def test_get_following_rejects_post_urls(monkeypatch):
    db = MagicMock()
    monkeypatch.setattr("tool.ig.cli.following.init_db", lambda: db)

    with pytest.raises(ValueError, match="get-following expects @profile"):
        await _get_following(["https://www.instagram.com/p/DXzJCEmu5bI/"])

    db.close.assert_called_once()


@pytest.mark.asyncio
async def test_get_following_rejects_profile_urls(monkeypatch):
    db = MagicMock()
    monkeypatch.setattr("tool.ig.cli.following.init_db", lambda: db)

    with pytest.raises(ValueError, match="get-following expects @profile"):
        await _get_following(["https://www.instagram.com/alice/"])

    db.close.assert_called_once()


@pytest.mark.asyncio
async def test_get_following_rejects_bare_usernames(monkeypatch):
    db = MagicMock()
    monkeypatch.setattr("tool.ig.cli.following.init_db", lambda: db)

    with pytest.raises(ValueError, match="get-following expects @profile"):
        await _get_following(["alice"])

    db.close.assert_called_once()


def test_dump_following_prints_current_usernames(monkeypatch, capsys):
    db = init_db(":memory:")
    save_following(
        db,
        "source",
        [
            Followee("alice", "1", "", False, ""),
            Followee("bob", "2", "", False, ""),
        ],
    )
    monkeypatch.setattr("tool.ig.cli.following.init_db", lambda: db)

    _dump_following(["@source"])

    assert capsys.readouterr().out.splitlines() == ["alice", "bob"]


def test_info_done_counts_downloaded_files(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    db = init_db("instagram.db")
    db.executescript(
        """
        INSERT INTO post VALUES ('DONE1', 'alice', 'cap', 'image', 1);
        INSERT INTO media VALUES ('DONE1', 0, 'https://cdn/done.jpg');
        INSERT INTO profile_post VALUES ('alice', 0, 'DONE1', 1, 0);
        INSERT INTO post VALUES ('PEND1', 'alice', 'cap', 'image', 1);
        INSERT INTO media VALUES ('PEND1', 0, 'https://cdn/pending.jpg');
        INSERT INTO profile_post VALUES ('alice', 0, 'PEND1', 1, 1);
        """
    )
    db.commit()
    db.close()

    media_dir = tmp_path / "images"
    (media_dir / "E1").mkdir(parents=True)
    (media_dir / "E1" / "DONE1.jpg").write_bytes(b"image")

    _info(media_dir=media_dir, images=True)

    lines = capsys.readouterr().out.splitlines()
    assert "@alice                        2      1       0      2     0     0" in lines
    assert "\033[32m@alice" not in "\n".join(lines)


def test_info_colors_completed_rows(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    db = init_db("instagram.db")
    db.executescript(
        """
        INSERT INTO post VALUES ('DONE1', 'alice', 'cap', 'image', 1);
        INSERT INTO media VALUES ('DONE1', 0, 'https://cdn/done.jpg');
        INSERT INTO profile_post VALUES ('alice', 0, 'DONE1', 1, 0);
        """
    )
    db.commit()
    db.close()

    media_dir = tmp_path / "images"
    (media_dir / "E1").mkdir(parents=True)
    (media_dir / "E1" / "DONE1.jpg").write_bytes(b"image")

    _info(media_dir=media_dir, images=True)

    output = capsys.readouterr().out
    assert "\033[32m@alice                        1      1       0      1     0     0\033[0m" in output


def test_import_db_copies_profiles_failures_and_following(tmp_path):
    target_path = tmp_path / "target.db"
    source_path = tmp_path / "source.db"

    target = init_db(str(target_path))
    target.execute("INSERT INTO post VALUES ('SHARED1', NULL, NULL, NULL, NULL)")
    target.commit()
    target.close()

    source = init_db(str(source_path))
    init_profile_table(source)
    source.executescript(
        """
        INSERT INTO post VALUES ('A1', 'owner', 'cap', 'image', 10);
        INSERT INTO post VALUES ('SHARED1', 'owner', 'shared cap', 'image', 11);
        INSERT INTO media VALUES ('A1', 0, 'https://cdn/a1.jpg');
        INSERT INTO media VALUES ('SHARED1', 0, 'https://cdn/shared.jpg');
        INSERT INTO profile_post VALUES ('alice', 0, 'A1', 20, 0);
        INSERT INTO profile_post VALUES ('alice', 0, 'SHARED1', 20, 1);
        INSERT INTO profile_post VALUES ('alice', 1, 'A1', 21, 0);
        INSERT INTO profile_following VALUES (
            'alice', 'bob', '2', 'Bob', 0, 'https://cdn/bob.jpg', 30, 31, 0, 1
        );
        INSERT INTO profile_scrape VALUES ('alice', 'A1', 40, 'SHARED1', 41);
        INSERT INTO profile_failure (
            username, saved, shortid, stage, reason, failed_at, resolved_at
        ) VALUES ('alice', 0, 'A1', 'scan_direct', 'failed', 50, NULL);
        INSERT INTO profile_failure (
            username, saved, shortid, stage, reason, failed_at, resolved_at
        ) VALUES ('alice', 1, 'A1', 'scan_direct', 'resolved', 51, 52);
        """
    )
    source.commit()
    source.close()

    summary = import_db(source_path, target_path=target_path)

    assert summary.posts == 1
    assert summary.media == 2
    assert summary.profile_posts == 3
    assert summary.following == 1
    assert summary.profile_scrapes == 1
    assert summary.profile_failures == 2

    target = init_db(str(target_path))
    init_profile_table(target)
    try:
        shared = target.execute(
            "SELECT username, caption, post_type, scraped_at FROM post WHERE shortid = 'SHARED1'"
        ).fetchone()
        assert shared == ("owner", "shared cap", "image", 11)
        profile_rows = target.execute(
            "SELECT username, saved, shortid, seq FROM profile_post ORDER BY username, saved, seq"
        ).fetchall()
        assert profile_rows == [
            ("alice", 0, "A1", 0),
            ("alice", 0, "SHARED1", 1),
            ("alice", 1, "A1", 0),
        ]
        failures = target.execute(
            "SELECT username, saved, shortid, stage, reason, resolved_at FROM profile_failure ORDER BY saved"
        ).fetchall()
        assert failures == [
            ("alice", 0, "A1", "scan_direct", "failed", None),
            ("alice", 1, "A1", "scan_direct", "resolved", 52),
        ]
        assert target.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert target.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        target.close()


def test_import_db_rejects_invalid_profile_username_before_copying(tmp_path):
    target_path = tmp_path / "target.db"
    source_path = tmp_path / "source.db"

    source = init_db(str(source_path))
    source.executescript(
        """
        INSERT INTO post VALUES ('A1', 'owner', 'cap', 'image', 10);
        INSERT INTO profile_post VALUES ('../../outside', 0, 'A1', 20, 0);
        """
    )
    source.commit()
    source.close()

    with pytest.raises(ImportDbError, match="invalid Instagram username"):
        import_db(source_path, target_path=target_path)

    target = init_db(str(target_path))
    try:
        assert target.execute("SELECT COUNT(*) FROM post").fetchone()[0] == 0
        assert target.execute("SELECT COUNT(*) FROM profile_post").fetchone()[0] == 0
    finally:
        target.close()


def test_import_db_rejects_existing_profile_collection(tmp_path):
    target_path = tmp_path / "target.db"
    source_path = tmp_path / "source.db"

    target = init_db(str(target_path))
    target.executescript(
        """
        INSERT INTO post VALUES ('A1', 'owner', 'cap', 'image', 1);
        INSERT INTO profile_post VALUES ('alice', 0, 'A1', 1, 0);
        """
    )
    target.commit()
    target.close()

    source = init_db(str(source_path))
    source.executescript(
        """
        INSERT INTO post VALUES ('B1', 'owner', 'cap', 'image', 1);
        INSERT INTO profile_post VALUES ('alice', 0, 'B1', 1, 0);
        """
    )
    source.commit()
    source.close()

    with pytest.raises(ImportDbConflictError, match="source database contains collections"):
        import_db(source_path, target_path=target_path)

    target = init_db(str(target_path))
    try:
        assert target.execute("SELECT COUNT(*) FROM post WHERE shortid = 'B1'").fetchone()[0] == 0
        assert target.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        target.close()


@pytest.mark.asyncio
async def test_get_processes_profiles_sequentially(monkeypatch):
    db = MagicMock()
    active = 0
    max_active = 0
    events = []

    async def fake_download_scanned_profile(identifier, *_args, **_kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        events.append(("start", identifier))
        await asyncio.sleep(0)
        events.append(("end", identifier))
        active -= 1

    monkeypatch.setattr("tool.ig.cli.get.init_db", lambda: db)
    monkeypatch.setattr("tool.ig.cli.get.download_scanned_profile", fake_download_scanned_profile)

    await _get(["@alice", "@bob"])

    assert max_active == 1
    assert events == [
        ("start", "alice"),
        ("end", "alice"),
        ("start", "bob"),
        ("end", "bob"),
    ]
    db.close.assert_called_once()


@pytest.mark.asyncio
async def test_get_rejects_non_at_profile_targets(monkeypatch):
    db = MagicMock()
    monkeypatch.setattr("tool.ig.cli.get.init_db", lambda: db)

    with pytest.raises(ValueError, match="get expects @profile"):
        await _get(["alice"])

    db.close.assert_called_once()


@pytest.mark.asyncio
async def test_get_no_args_discovers_incomplete_scopes(monkeypatch):
    """When no urls given, discover and download all incomplete scopes."""
    calls: list[list[tuple[str, bool]]] = []

    async def fake_profiles(scopes, *_args, **kwargs):
        calls.append(sorted(scopes))

    # Build a real DB with incomplete posts in both scopes
    db = init_db(":memory:")
    init_profile_table(db)
    db.execute("INSERT INTO post (shortid, post_type) VALUES ('S1', 'image')")
    db.execute("INSERT INTO post (shortid, post_type) VALUES ('S2', 'image')")
    db.execute("INSERT INTO post (shortid, post_type) VALUES ('S3', 'image')")
    db.execute("INSERT INTO media (shortid, idx, url) VALUES ('S1', 0, 'http://x/s1.jpg')")
    db.execute("INSERT INTO media (shortid, idx, url) VALUES ('S2', 0, 'http://x/s2.jpg')")
    db.execute("INSERT INTO media (shortid, idx, url) VALUES ('S3', 0, 'http://x/s3.jpg')")
    # alice: normal is done (S1 file exists), saved is incomplete (no file)
    db.execute("INSERT INTO profile_post (username, saved, shortid, seen_at, seq) VALUES ('alice', 0, 'S1', 0, 0)")
    db.execute("INSERT INTO profile_post (username, saved, shortid, seen_at, seq) VALUES ('alice', 1, 'S2', 0, 0)")
    # bob: normal is incomplete (S3 has no file), no saved rows
    db.execute("INSERT INTO profile_post (username, saved, shortid, seen_at, seq) VALUES ('bob', 0, 'S3', 0, 0)")
    db.commit()

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        media_dir = Path(tmp)
        (media_dir / "S1").mkdir(parents=True)
        (media_dir / "S1" / "S1.jpg").write_bytes(b"x")

        monkeypatch.setattr("tool.ig.cli.get.init_db", lambda: db)
        monkeypatch.setattr("tool.ig.cli.get.download_scanned_profiles", fake_profiles)

        await _get([], media_dir=str(media_dir))

    # alice normal (S1) is done, alice saved (S2) incomplete, bob normal (S3) incomplete
    assert calls == [[("alice", True), ("bob", False)]]


@pytest.mark.asyncio
async def test_get_saved_no_args_discovers_saved_only(monkeypatch):
    """When --saved with no urls, discover only saved incomplete scopes."""
    calls: list[list[tuple[str, bool]]] = []

    async def fake_profiles(scopes, *_args, **kwargs):
        calls.append(sorted(scopes))

    db = init_db(":memory:")
    init_profile_table(db)
    db.execute("INSERT INTO post (shortid, post_type) VALUES ('S1', 'image')")
    db.execute("INSERT INTO post (shortid, post_type) VALUES ('S2', 'image')")
    db.execute("INSERT INTO media (shortid, idx, url) VALUES ('S1', 0, 'http://x/s1.jpg')")
    db.execute("INSERT INTO media (shortid, idx, url) VALUES ('S2', 0, 'http://x/s2.jpg')")
    # alice normal incomplete, saved incomplete
    db.execute("INSERT INTO profile_post (username, saved, shortid, seen_at, seq) VALUES ('alice', 0, 'S1', 0, 0)")
    db.execute("INSERT INTO profile_post (username, saved, shortid, seen_at, seq) VALUES ('alice', 1, 'S2', 0, 0)")
    db.commit()

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        media_dir = Path(tmp)
        monkeypatch.setattr("tool.ig.cli.get.init_db", lambda: db)
        monkeypatch.setattr("tool.ig.cli.get.download_scanned_profiles", fake_profiles)

        await _get([], saved=True, media_dir=str(media_dir))

    # Only saved scope should be discovered
    assert calls == [[("alice", True)]]


@pytest.mark.asyncio
async def test_get_saved_rejects_non_profile_targets(monkeypatch):
    db = MagicMock()
    monkeypatch.setattr("tool.ig.cli.get.init_db", lambda: db)

    with pytest.raises(ValueError, match="get expects @profile"):
        await _get(["DXzJCEmu5bI"], saved=True)

    db.close.assert_called_once()


@pytest.mark.asyncio
async def test_get_passes_jobs_to_profile_download(monkeypatch):
    db = MagicMock()
    calls = []

    async def fake_download_scanned_profile(identifier, *_args, **kwargs):
        calls.append((identifier, kwargs["jobs"]))

    monkeypatch.setattr("tool.ig.cli.get.init_db", lambda: db)
    monkeypatch.setattr("tool.ig.cli.get.download_scanned_profile", fake_download_scanned_profile)

    await _get(["@alice"], jobs=3)

    assert calls == [("alice", 3)]
    db.close.assert_called_once()


def test_remove_preserves_standalone_post_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = init_db("instagram.db")
    db.executescript(
        """
        INSERT INTO post VALUES ('PROFILE1', 'alice', 'cap', 'image', 1);
        INSERT INTO media VALUES ('PROFILE1', 0, 'https://cdn/profile.jpg');
        INSERT INTO profile_post VALUES ('alice', 0, 'PROFILE1', 1, 0);
        INSERT INTO post VALUES ('DIRECT1', 'owner', 'cap', 'image', 1);
        INSERT INTO media VALUES ('DIRECT1', 0, 'https://cdn/direct.jpg');
        """
    )
    db.commit()
    db.close()

    media_dir = tmp_path / "site" / "images"
    (media_dir / "E1").mkdir(parents=True)
    (media_dir / "E1" / "PROFILE1.jpg").write_bytes(b"profile")
    (media_dir / "T1").mkdir(parents=True)
    (media_dir / "T1" / "DIRECT1.jpg").write_bytes(b"direct")

    _remove("alice", media_dir=str(media_dir))

    db = init_db("instagram.db")
    try:
        assert db.execute("SELECT COUNT(*) FROM post WHERE shortid = 'PROFILE1'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM post WHERE shortid = 'DIRECT1'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM media WHERE shortid = 'DIRECT1'").fetchone()[0] == 1
    finally:
        db.close()


def test_remove_saved_removes_orphaned_metadata_stubs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = init_db("instagram.db")
    init_profile_table(db)
    db.executescript(
        """
        INSERT INTO post VALUES ('SAVEDSTUB', NULL, NULL, NULL, NULL);
        INSERT INTO post VALUES ('SHARED1', 'owner', 'cap', 'image', 1);
        INSERT INTO media VALUES ('SHARED1', 0, 'https://cdn/shared.jpg');
        INSERT INTO profile_post VALUES ('alice', 1, 'SAVEDSTUB', 1, 0);
        INSERT INTO profile_post VALUES ('alice', 1, 'SHARED1', 1, 1);
        INSERT INTO profile_post VALUES ('alice', 0, 'SHARED1', 1, 0);
        INSERT INTO profile_failure (username, saved, shortid, stage, reason, failed_at, resolved_at)
        VALUES ('alice', 1, 'SAVEDSTUB', 'scan_direct', 'failed', 1, NULL);
        """
    )
    db.commit()
    db.close()

    _remove("alice", saved=True, media_dir=str(tmp_path / "site" / "images"))

    db = init_db("instagram.db")
    try:
        assert db.execute("SELECT COUNT(*) FROM profile_post WHERE username = 'alice' AND saved = 1").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM post WHERE shortid = 'SAVEDSTUB'").fetchone()[0] == 0
        assert (
            db.execute("SELECT COUNT(*) FROM profile_failure WHERE username = 'alice' AND saved = 1").fetchone()[0] == 0
        )
        assert db.execute("SELECT COUNT(*) FROM post WHERE shortid = 'SHARED1'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM media WHERE shortid = 'SHARED1'").fetchone()[0] == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_scan_saved_defaults_to_logged_in_profile(monkeypatch):
    db = MagicMock()
    calls = []

    async def fake_scan_profiles(identifiers, *_args, **kwargs):
        calls.append((identifiers, kwargs["saved"], kwargs["stop_at_known"]))

    monkeypatch.setattr("tool.ig.cli.scan.init_db", lambda: db)
    monkeypatch.setattr("tool.ig.cli.scan.scan_profiles", fake_scan_profiles)

    await _scan([], saved=True)

    assert calls == [([DEFAULT_SAVED_PROFILE], True, True)]
    db.close.assert_called_once()


@pytest.mark.asyncio
async def test_scan_processes_multiple_profiles_in_one_call(monkeypatch):
    db = MagicMock()
    calls = []

    async def fake_scan_profiles(identifiers, *_args, **kwargs):
        calls.append((identifiers, kwargs["live"], kwargs["saved"], kwargs["stop_at_known"]))

    monkeypatch.setattr("tool.ig.cli.scan.init_db", lambda: db)
    monkeypatch.setattr("tool.ig.cli.scan.scan_profiles", fake_scan_profiles)

    await _scan(["@alice", "@bob"], live=True, full=True)

    assert calls == [(["alice", "bob"], True, False, False)]
    db.close.assert_called_once()


@pytest.mark.asyncio
async def test_scan_rejects_non_at_profile_targets(monkeypatch):
    db = MagicMock()
    monkeypatch.setattr("tool.ig.cli.scan.init_db", lambda: db)

    with pytest.raises(ValueError, match="scan expects @profile"):
        await _scan(["alice"])

    db.close.assert_called_once()


def test_run_reports_busy_browser_without_traceback(monkeypatch, capsys):
    def fake_run(main_coro):
        main_coro.close()
        raise BrowserInUseError("isolated browser is already controlled by another ig command")

    monkeypatch.setattr(cli.uvloop, "run", fake_run)

    with pytest.raises(SystemExit) as exc:
        cli.run()

    captured = capsys.readouterr()
    assert exc.value.code == 1
    assert captured.out == ""
    assert captured.err == "isolated browser is already controlled by another ig command\n"


@pytest.mark.asyncio
async def test_fingerprint_live_uses_headful_browser(monkeypatch, capsys):
    seen = {}

    class FakeTab:
        async def activate(self):
            pass

        async def navigate(self, url, wait="complete"):
            seen["navigate"] = (url, wait)

        async def evaluate(self, _js):
            return {
                "automationGlobals": [],
                "deviceMemory": None,
                "devicePixelRatio": 1,
                "hardwareConcurrency": 10,
                "hidden": False,
                "inner": [1920, 1080],
                "language": "en-US",
                "languages": ["en-US", "en"],
                "maxTouchPoints": 0,
                "mimeTypes": ["application/pdf"],
                "notificationPermission": "prompt",
                "outer": [1920, 1080],
                "platform": "MacIntel",
                "plugins": ["PDF Viewer"],
                "screen": [1920, 1080, 1920, 1055, 24, 24],
                "userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "visibilityState": "visible",
                "webdriver": None,
                "webgl": {"renderer": "Apple M1, or similar", "vendor": "Apple"},
            }

    @asynccontextmanager
    async def fake_browser_session(**kwargs):
        seen["headless"] = kwargs["headless"]
        yield object(), FakeTab()

    monkeypatch.setattr("tool.ig.cli.fingerprint.browser_session", fake_browser_session)
    monkeypatch.setattr("tool.ig.cli.fingerprint._host_fingerprint", lambda: {"macosArm": True})

    await _fingerprint(live=True)

    captured = capsys.readouterr()
    assert seen["headless"] is False
    assert seen["navigate"] == ("https://example.com/", "complete")
    assert '"mode": "headful"' in captured.out
    assert captured.err == ""


class TestFingerprintWarnings:
    def test_sane_headless_chrome_surface_has_no_warnings(self):
        assert (
            _fingerprint_warnings(
                {
                    "automationGlobals": [],
                    "deviceMemory": None,
                    "devicePixelRatio": 1,
                    "hardwareConcurrency": 8,
                    "hidden": False,
                    "inner": [1920, 1080],
                    "language": "en-US",
                    "languages": ["en-US", "en"],
                    "maxTouchPoints": 0,
                    "mimeTypes": ["application/pdf"],
                    "notificationPermission": "prompt",
                    "outer": [1920, 1080],
                    "platform": "MacIntel",
                    "plugins": ["PDF Viewer"],
                    "screen": [1920, 1080, 1920, 1055, 24, 24],
                    "userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "visibilityState": "visible",
                    "webdriver": None,
                    "webgl": {"renderer": "Apple M1, or similar", "vendor": "Apple"},
                }
            )
            == []
        )

    def test_botty_surface_reports_core_warnings(self):
        warnings = _fingerprint_warnings(
            {
                "automationGlobals": ["__webdriver_evaluate"],
                "deviceMemory": 2,
                "devicePixelRatio": 1,
                "hardwareConcurrency": 1,
                "hidden": True,
                "inner": [800, 600],
                "language": "en-US",
                "languages": ["en-US", "en"],
                "maxTouchPoints": 0,
                "mimeTypes": [],
                "notificationPermission": "denied",
                "outer": [0, 0],
                "platform": "MacIntel",
                "plugins": [],
                "screen": [800, 600, 800, 600, 24, 24],
                "userAgent": "Mozilla/5.0",
                "visibilityState": "hidden",
                "webdriver": True,
                "webgl": None,
            }
        )

        assert "navigator.webdriver is exposed" in warnings
        assert "automation globals are exposed" in warnings
        assert "document visibility does not look foregrounded" in warnings
        assert "deviceMemory is implausibly low for a Mac laptop" in warnings
        assert "viewport size is unexpected" in warnings
        assert "PDF plugin is missing" in warnings
        assert "WebGL renderer surface is unavailable" in warnings

    def test_deeper_probe_warnings_report_cross_context_leaks(self):
        warnings = _fingerprint_warnings(
            {
                "automationGlobals": ["__selenium_evaluate"],
                "deviceMemory": None,
                "devicePixelRatio": 1,
                "hardwareConcurrency": 8,
                "hidden": False,
                "inner": [1920, 1080],
                "language": "en-US",
                "languages": ["en-US", "en"],
                "maxTouchPoints": 0,
                "mimeTypes": ["application/pdf"],
                "mimeTypesClass": "[object Object]",
                "mimeTypesInstance": False,
                "notificationPermission": "default",
                "outer": [1920, 1080],
                "platform": "MacIntel",
                "plugins": ["PDF Viewer"],
                "pluginsClass": "[object Object]",
                "pluginsInstance": False,
                "screen": [1920, 1080, 1920, 1055, 24, 24],
                "userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "visibilityState": "visible",
                "webdriver": None,
                "descriptors": {
                    "webdriver": {"getterNative": False},
                    "hardwareConcurrency": {"getterNative": False},
                    "platform": {"getterNative": True},
                },
                "fonts": {
                    "base": {"monospace": 100},
                    "measured": {"-apple-system": 100, "Helvetica Neue": 100},
                },
                "mediaDevices": [],
                "worker": {
                    "userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "platform": "MacIntel",
                    "language": "en-US",
                    "languages": ["en-US", "en"],
                    "hardwareConcurrency": 10,
                    "deviceMemory": 8,
                    "maxTouchPoints": 0,
                },
                "webgl": {"renderer": "Apple M1, or similar", "vendor": "Apple"},
            },
            {"system": "Linux", "machine": "x86_64", "macosArm": False},
        )

        assert "host is not macOS ARM; Mac laptop fingerprint is not a trusted baseline" in warnings
        assert "automation globals are exposed" in warnings
        assert "navigator.plugins does not look like a native PluginArray" in warnings
        assert "navigator.mimeTypes does not look like a native MimeTypeArray" in warnings
        assert "navigator.webdriver getter does not look native" in warnings
        assert "navigator.hardwareConcurrency getter does not look native" in warnings
        assert "worker navigator.hardwareConcurrency does not match page context" in warnings
        assert "worker navigator.deviceMemory does not match page context" in warnings

    def test_macos_arm_persona_reports_unusual_native_surfaces(self):
        warnings = _fingerprint_warnings(
            {
                "automationGlobals": [],
                "deviceMemory": None,
                "devicePixelRatio": 1,
                "hardwareConcurrency": 6,
                "hidden": False,
                "inner": [1920, 1080],
                "language": "en-US",
                "languages": ["en-US", "en"],
                "maxTouchPoints": 0,
                "mimeTypes": ["application/pdf"],
                "notificationPermission": "default",
                "outer": [1920, 1080],
                "platform": "MacIntel",
                "plugins": ["PDF Viewer"],
                "screen": [1920, 1080, 1920, 1055, 24, 24],
                "userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "visibilityState": "visible",
                "webdriver": None,
                "fonts": {
                    "base": {"monospace": 100},
                    "measured": {"-apple-system": 100, "Helvetica Neue": 100},
                },
                "mediaDevices": [{"kind": "audioinput"}],
                "webgl": {"renderer": "Apple M1, or similar", "vendor": "Apple"},
            },
            {"system": "Darwin", "machine": "arm64", "macosArm": True},
        )

        assert "hardwareConcurrency is unusual for Apple Silicon" in warnings
        assert "media devices do not look like a Mac laptop" in warnings
        assert "Mac system font probes look absent" in warnings
