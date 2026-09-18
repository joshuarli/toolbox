"""Tests for profile scan stub rows."""

import asyncio
import base64
import json
from contextlib import asynccontextmanager

import pytest

from tool.ig.db import init_db
from tool.ig.media import media_path
from tool.ig.post import RateLimitError
from tool.ig.profile_scan import (
    _GRID_LAYOUT_JS,
    _PAGE_TEXT_JS,
    _SCAN_SHORTIDS_JS,
    ProfileScope,
    _download_stored_post_metadata,
    _hard_refresh_profile_page,
    _pending_profile_shortids,
    _record_failure,
    _wait_scan_grid,
    download_scanned_profile,
    download_scanned_profiles,
    init_profile_scan,
    save_profile_posts_metadata,
    save_profile_shortids,
    scan_profile,
    scan_profiles,
    scanned_profile_shortids,
)
from tool.ig.profile_timeline import ProfilePostsPage, ProfileTimelineSession
from tool.ig.types import Pace, ScrapedPost


class _LongScanTab:
    def __init__(self, total: int) -> None:
        self.total = total
        self.visible = 1
        self.height = 1000.0
        self.grow_calls = 0

    async def activate(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def evaluate(self, js: str):
        if "window.location.href" in js:
            return "https://www.instagram.com/alice/"
        if "querySelectorAll" in js:
            return [f"S{i}" for i in range(self.visible)]
        if "document.documentElement.scrollHeight" in js:
            return self.height
        return None

    async def grow(self, scroll_h: float) -> float:
        self.grow_calls += 1
        if self.visible < self.total:
            self.visible += 1
            self.height += 100.0
            return self.height
        return scroll_h


def _video_url(tag: str) -> str:
    efg = base64.urlsafe_b64encode(json.dumps({"vencode_tag": tag, "duration_s": 48, "bitrate": 1000}).encode())
    return f"https://cdn/video.mp4?efg={efg.decode().rstrip('=')}"


class _DownloadTab:
    async def activate(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def evaluate(self, js: str):
        if "window.location.href" in js:
            return "https://www.instagram.com/alice/"
        return None


class _CollectableGridTab:
    async def evaluate(self, js: str):
        if js == _GRID_LAYOUT_JS:
            return None
        if js == _SCAN_SHORTIDS_JS:
            return ["REEL1"]
        if js == _PAGE_TEXT_JS:
            return ""
        raise AssertionError(f"unexpected JavaScript: {js[:40]}")


def _browser_session_for(tab):
    @asynccontextmanager
    async def session(**_):
        yield object(), tab

    return session


class _ReloadTab:
    """Records protocol commands so tests can pin the reload to real CDP methods."""

    def __init__(self, fail_refresh_js: bool = False) -> None:
        self.fail_refresh_js = fail_refresh_js
        self.commands: list[tuple[str, dict]] = []

    async def evaluate(self, expression: str):
        if self.fail_refresh_js and "serviceWorker" in expression:
            raise RuntimeError("js failed")
        return "complete"

    async def command(self, method: str, params=None, timeout=None):
        self.commands.append((method, params or {}))


class TestHardRefresh:
    @pytest.mark.asyncio
    async def test_hard_refresh_reloads_with_cdp_page_reload(self):
        tab = _ReloadTab()
        await _hard_refresh_profile_page(tab)
        methods = [method for method, _params in tab.commands]
        assert "Page.reload" in methods
        # shadowdriver only shims a few BiDi names; these are not among them.
        assert "browsingContext.reload" not in methods
        assert "input.performActions" not in methods

    @pytest.mark.asyncio
    async def test_hard_refresh_still_reloads_when_refresh_js_fails(self):
        tab = _ReloadTab(fail_refresh_js=True)
        await _hard_refresh_profile_page(tab)
        assert any(method == "Page.reload" for method, _params in tab.commands)


class TestProfileScanStubs:
    def test_scan_selector_includes_reels(self):
        assert 'a[href*="/reel/"]' in _SCAN_SHORTIDS_JS
        assert "(?:p|reel)" in _SCAN_SHORTIDS_JS

    @pytest.mark.asyncio
    async def test_wait_scan_grid_accepts_collectable_post_links_without_layout(self, monkeypatch):
        tab = _CollectableGridTab()

        async def no_sleep(*_args, **_kwargs):
            pass

        monkeypatch.setattr("tool.ig.profile_scan.asyncio.sleep", no_sleep)

        async def fail_refresh(_tab):
            raise AssertionError("a collectable post link should count as a rendered grid")

        monkeypatch.setattr("tool.ig.profile_scan._hard_refresh_profile_page", fail_refresh)

        await _wait_scan_grid(tab)

    def test_uses_existing_post_table(self):
        db = init_db(":memory:")
        init_profile_scan(db)
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "post" in tables
        assert "profile_post" in tables

    def test_save_and_read_shortids_in_scan_order(self):
        db = init_db(":memory:")
        save_profile_shortids(db, "alice", ["A1", "B2", "C3"])
        assert scanned_profile_shortids(db, "alice") == ["A1", "B2", "C3"]
        row = db.execute("SELECT caption, post_type, scraped_at FROM post WHERE shortid = 'A1'").fetchone()
        assert row == (None, None, None)

    def test_rescan_inserts_only_new_stubs(self):
        db = init_db(":memory:")
        assert save_profile_shortids(db, "alice", ["A1", "B2", "C3"]) == 3
        assert save_profile_shortids(db, "alice", ["N0", "A1", "C3", "B2"]) == 1
        assert scanned_profile_shortids(db, "alice") == ["A1", "B2", "C3", "N0"]
        count = db.execute("SELECT COUNT(*) FROM profile_post WHERE username = 'alice'").fetchone()[0]
        assert count == 4

    def test_saved_shortids_are_separate_from_profile_shortids(self):
        db = init_db(":memory:")
        assert save_profile_shortids(db, "alice", ["A1", "B2"]) == 2
        assert save_profile_shortids(db, "alice", ["B2", "S1"], saved=True) == 2

        assert scanned_profile_shortids(db, "alice") == ["A1", "B2"]
        assert scanned_profile_shortids(db, "alice", saved=True) == ["B2", "S1"]
        rows = db.execute(
            "SELECT saved, shortid FROM profile_post WHERE username = 'alice' ORDER BY saved, seq"
        ).fetchall()
        assert rows == [(0, "A1"), (0, "B2"), (1, "B2"), (1, "S1")]

    def test_record_failure_updates_existing_open_failure(self):
        db = init_db(":memory:")
        init_profile_scan(db)

        _record_failure(db, "alice", "A1", "scan_direct", "first")
        _record_failure(db, "alice", "A1", "scan_direct", "second")

        rows = db.execute(
            """
            SELECT reason, resolved_at
            FROM profile_failure
            WHERE username = 'alice' AND saved = 0 AND shortid = 'A1' AND stage = 'scan_direct'
            """
        ).fetchall()
        assert rows == [("second", None)]

    def test_scan_does_not_modify_existing_post_rows(self):
        db = init_db(":memory:")
        db.execute("INSERT INTO post VALUES ('A1', 'alice', 'cap', 'image', 123)")
        db.commit()
        assert save_profile_shortids(db, "alice", ["A1", "B2"]) == 2
        row = db.execute("SELECT username, caption, post_type, scraped_at FROM post WHERE shortid = 'A1'").fetchone()
        assert row == ("alice", "cap", "image", 123)
        stub = db.execute("SELECT username, caption, post_type, scraped_at FROM post WHERE shortid = 'B2'").fetchone()
        assert stub == (None, None, None, None)

    def test_save_profile_posts_metadata_updates_known_stubs(self):
        db = init_db(":memory:")
        save_profile_shortids(db, "alice", ["A1"])

        inserted = save_profile_posts_metadata(
            db,
            "alice",
            [ScrapedPost("A1", "image", ["https://cdn/image.jpg"], username="owner", caption="cap")],
        )

        assert inserted == 0
        row = db.execute("SELECT username, caption, post_type, scraped_at FROM post WHERE shortid = 'A1'").fetchone()
        assert row[:3] == ("owner", "cap", "image")
        assert row[3] is not None
        media = db.execute("SELECT idx, url FROM media WHERE shortid = 'A1'").fetchall()
        assert media == [(0, "https://cdn/image.jpg")]

    @pytest.mark.asyncio
    async def test_scan_profile_default_scrolls_past_old_limit(self, monkeypatch):
        db = init_db(":memory:")
        tab = _LongScanTab(total=1005)

        async def noop(*_):
            pass

        monkeypatch.setattr("tool.ig.profile_scan.browser_session", _browser_session_for(tab))
        monkeypatch.setattr("tool.ig.profile_scan.navigate_to", noop)
        monkeypatch.setattr("tool.ig.profile_scan.wait_ready", noop)
        monkeypatch.setattr("tool.ig.profile_scan._wait_scan_grid", noop)
        monkeypatch.setattr(
            "tool.ig.profile_scan._wait_scroll_height_growth",
            lambda tab, scroll_h, **_: tab.grow(scroll_h),
        )

        await scan_profile("alice", db)

        assert len(scanned_profile_shortids(db, "alice")) == 1005

    @pytest.mark.asyncio
    async def test_scan_profile_continues_past_reordered_known_post(self, monkeypatch):
        db = init_db(":memory:")
        save_profile_shortids(db, "alice", ["S3"])
        tab = _LongScanTab(total=10)

        async def noop(*_):
            pass

        monkeypatch.setattr("tool.ig.profile_scan.browser_session", _browser_session_for(tab))
        monkeypatch.setattr("tool.ig.profile_scan.navigate_to", noop)
        monkeypatch.setattr("tool.ig.profile_scan.wait_ready", noop)
        monkeypatch.setattr("tool.ig.profile_scan._wait_scan_grid", noop)
        monkeypatch.setattr(
            "tool.ig.profile_scan._wait_scroll_height_growth",
            lambda tab, scroll_h, **_: tab.grow(scroll_h),
        )

        await scan_profile("alice", db)

        assert scanned_profile_shortids(db, "alice") == [
            "S3",
            "S0",
            "S1",
            "S2",
            "S4",
            "S5",
            "S6",
            "S7",
            "S8",
            "S9",
        ]
        assert tab.visible == 10

    @pytest.mark.asyncio
    async def test_scan_profile_can_rescan_full_profile(self, monkeypatch):
        db = init_db(":memory:")
        save_profile_shortids(db, "alice", ["S3"])
        tab = _LongScanTab(total=10)

        async def noop(*_):
            pass

        monkeypatch.setattr("tool.ig.profile_scan.browser_session", _browser_session_for(tab))
        monkeypatch.setattr("tool.ig.profile_scan.navigate_to", noop)
        monkeypatch.setattr("tool.ig.profile_scan.wait_ready", noop)
        monkeypatch.setattr("tool.ig.profile_scan._wait_scan_grid", noop)
        monkeypatch.setattr(
            "tool.ig.profile_scan._wait_scroll_height_growth",
            lambda tab, scroll_h, **_: tab.grow(scroll_h),
        )

        await scan_profile("alice", db, stop_at_known=False)

        assert len(scanned_profile_shortids(db, "alice")) == 10
        assert tab.visible == 10

    @pytest.mark.asyncio
    async def test_scan_profile_continues_past_known_stubs_without_metadata(self, monkeypatch):
        db = init_db(":memory:")
        save_profile_shortids(db, "alice", ["A1"])
        tab = _DownloadTab()
        afters = []
        pages = [
            ProfilePostsPage(
                [ScrapedPost("A1", "image", ["https://cdn/a1.jpg"], username="owner")],
                "cursor-1",
                True,
            ),
            ProfilePostsPage(
                [ScrapedPost("A2", "image", ["https://cdn/a2.jpg"], username="owner")],
                None,
                False,
            ),
        ]

        async def fake_open(_tab, _username):
            return ProfileTimelineSession("alice", "123", {"sessionid": "s", "csrftoken": "c"})

        async def fake_fetch(_session, *, after=None):
            afters.append(after)
            return pages.pop(0)

        async def fail_auto_follow(*_):
            raise AssertionError("profile scans must not auto-follow")

        monkeypatch.setattr("tool.ig.profile_scan.browser_session", _browser_session_for(tab))
        monkeypatch.setattr("tool.ig.profile_scan.open_profile_timeline", fake_open)
        monkeypatch.setattr("tool.ig.profile_scan.fetch_profile_posts_page", fake_fetch)
        monkeypatch.setattr("tool.ig.profile_scan._auto_follow_profile", fail_auto_follow)

        await scan_profile("alice", db)

        assert afters == [None, "cursor-1"]
        assert scanned_profile_shortids(db, "alice") == ["A1", "A2"]
        media = db.execute("SELECT shortid, url FROM media ORDER BY shortid").fetchall()
        assert media == [("A1", "https://cdn/a1.jpg"), ("A2", "https://cdn/a2.jpg")]

    @pytest.mark.asyncio
    async def test_scan_profile_saved_uses_saved_url_and_scope(self, monkeypatch):
        db = init_db(":memory:")
        save_profile_shortids(db, "alice", ["S0"])
        tab = _LongScanTab(total=3)
        navigated = []

        async def fake_navigate(_tab, url):
            navigated.append(url)

        async def noop(*_):
            pass

        auto_follows = []

        async def record_auto_follow(_tab, username):
            auto_follows.append(username)

        monkeypatch.setattr("tool.ig.profile_scan.browser_session", _browser_session_for(tab))
        monkeypatch.setattr("tool.ig.profile_scan.navigate_to", fake_navigate)
        monkeypatch.setattr("tool.ig.profile_scan.wait_ready", noop)
        monkeypatch.setattr("tool.ig.profile_scan._wait_saved_scan_grid", noop)
        monkeypatch.setattr(
            "tool.ig.profile_scan._wait_scroll_height_growth",
            lambda tab, scroll_h, **_: tab.grow(scroll_h),
        )
        monkeypatch.setattr("tool.ig.profile_scan._auto_follow_profile", record_auto_follow)

        await scan_profile("alice", db, saved=True)

        assert navigated == ["https://www.instagram.com/saved/all-posts/"]
        assert scanned_profile_shortids(db, "alice") == ["S0"]
        assert scanned_profile_shortids(db, "alice", saved=True) == ["S0", "S1", "S2"]
        assert auto_follows == []

    @pytest.mark.asyncio
    async def test_scan_profiles_reuses_one_browser_session(self, monkeypatch):
        db = init_db(":memory:")
        tab = _DownloadTab()
        sessions = 0
        calls = []

        @asynccontextmanager
        async def fake_browser_session(**_):
            nonlocal sessions
            sessions += 1
            yield object(), tab

        async def fake_scan_profile_in_tab(got_tab, username, got_db, **kwargs):
            calls.append((got_tab, username, got_db, kwargs["saved"], kwargs["stop_at_known"]))

        auto_follows = []

        async def record_auto_follow(_tab, username):
            auto_follows.append(username)

        monkeypatch.setattr("tool.ig.profile_scan.browser_session", fake_browser_session)
        monkeypatch.setattr("tool.ig.profile_scan._scan_profile_in_tab", fake_scan_profile_in_tab)
        monkeypatch.setattr("tool.ig.profile_scan._auto_follow_profile", record_auto_follow)

        await scan_profiles(["alice", "bob"], db, saved=True, stop_at_known=False)

        assert sessions == 1
        assert calls == [
            (tab, "alice", db, True, False),
            (tab, "bob", db, True, False),
        ]
        assert auto_follows == []

    @pytest.mark.asyncio
    async def test_scan_profiles_continues_after_one_profile_fails(self, monkeypatch, capsys):
        db = init_db(":memory:")
        tab = _DownloadTab()
        calls = []

        @asynccontextmanager
        async def fake_browser_session(**_):
            yield object(), tab

        async def fake_scan_profile_in_tab(_tab, username, _db, **_kwargs):
            calls.append(username)
            if username == "broken":
                raise RuntimeError("profile grid did not render")

        monkeypatch.setattr("tool.ig.profile_scan.browser_session", fake_browser_session)
        monkeypatch.setattr("tool.ig.profile_scan._scan_profile_in_tab", fake_scan_profile_in_tab)

        await scan_profiles(["broken", "healthy"], db)

        assert calls == ["broken", "healthy"]
        assert "scan: failed to scan @broken" in capsys.readouterr().err

    @pytest.mark.asyncio
    async def test_download_saved_profile_does_not_auto_follow(self, monkeypatch):
        db = init_db(":memory:")
        save_profile_shortids(db, "alice", ["SAVED1"], saved=True)
        tab = _DownloadTab()
        downloaded_scopes = []
        auto_follows = []

        async def download_scope(_browser, _tab, scope, *_args, **_kwargs):
            downloaded_scopes.append(scope)

        async def record_auto_follow(_tab, username):
            auto_follows.append(username)

        monkeypatch.setattr("tool.ig.profile_scan.browser_session", _browser_session_for(tab))
        monkeypatch.setattr("tool.ig.profile_scan._download_scope_posts", download_scope)
        monkeypatch.setattr("tool.ig.profile_scan._auto_follow_profile", record_auto_follow)

        await download_scanned_profile("alice", db, saved=True)

        assert downloaded_scopes == [ProfileScope("alice", saved=True)]
        assert auto_follows == []

    @pytest.mark.asyncio
    async def test_download_saved_profiles_does_not_auto_follow(self, monkeypatch):
        db = init_db(":memory:")
        save_profile_shortids(db, "alice", ["SAVED1"], saved=True)
        tab = _DownloadTab()
        downloaded_scopes = []
        auto_follows = []

        async def download_scope(_browser, _tab, scope, *_args, **_kwargs):
            downloaded_scopes.append(scope)

        async def record_auto_follow(_tab, username):
            auto_follows.append(username)

        monkeypatch.setattr("tool.ig.profile_scan.browser_session", _browser_session_for(tab))
        monkeypatch.setattr("tool.ig.profile_scan._download_scope_posts", download_scope)
        monkeypatch.setattr("tool.ig.profile_scan._auto_follow_profile", record_auto_follow)

        await download_scanned_profiles([("alice", True)], db)

        assert downloaded_scopes == [ProfileScope("alice", saved=True)]
        assert auto_follows == []

    @pytest.mark.asyncio
    async def test_completed_post_processing_failure_does_not_leave_pending_failure(self, tmp_path, monkeypatch):
        db = init_db(":memory:")
        save_profile_shortids(db, "alice", ["DONE1"])
        media_dir = tmp_path / "images"
        tab = _DownloadTab()

        @asynccontextmanager
        async def browser_session(**_):
            yield object(), _DownloadTab()

        async def new_tab(*_args, **_kwargs):
            return tab

        async def scrape_then_fail(_tab, shortid, db, **kwargs):
            (media_dir / shortid[-2:]).mkdir(parents=True)
            (media_dir / shortid[-2:] / f"{shortid}.jpg").write_bytes(b"image")
            db.execute("UPDATE post SET username = 'owner', caption = 'cap', post_type = 'image', scraped_at = 1")
            db.execute("INSERT INTO media (shortid, idx, url) VALUES (?, 0, 'https://cdn/image.jpg')", (shortid,))
            db.commit()
            raise RuntimeError("like failed after save")

        async def noop(*_):
            pass

        monkeypatch.setattr("tool.ig.profile_scan.browser_session", browser_session)
        monkeypatch.setattr("tool.ig.profile_scan.new_tab", new_tab)
        monkeypatch.setattr("tool.ig.profile_scan.scrape_post_in_tab", scrape_then_fail)
        monkeypatch.setattr("tool.ig.profile_scan._auto_follow_profile", noop)

        async def no_sleep(*_args, **_kwargs):
            pass

        monkeypatch.setattr("tool.ig.profile_scan.asyncio.sleep", no_sleep)

        await download_scanned_profile("alice", db, media_dir=media_dir)

        failures = db.execute("SELECT shortid FROM profile_failure WHERE resolved_at IS NULL").fetchall()
        assert failures == []

    @pytest.mark.asyncio
    async def test_download_clears_stale_failure_for_already_done_post(self, tmp_path, monkeypatch):
        db = init_db(":memory:")
        init_profile_scan(db)
        save_profile_shortids(db, "alice", ["DONE1"])
        media_dir = tmp_path / "images"
        (media_dir / "E1").mkdir(parents=True)
        (media_dir / "E1" / "DONE1.jpg").write_bytes(b"image")
        db.execute("UPDATE post SET username = 'owner', caption = 'cap', post_type = 'image', scraped_at = 1")
        db.execute("INSERT INTO media (shortid, idx, url) VALUES ('DONE1', 0, 'https://cdn/image.jpg')")
        db.execute(
            """
            INSERT INTO profile_failure (username, saved, shortid, stage, reason, failed_at, resolved_at)
            VALUES ('alice', 0, 'DONE1', 'scan_direct', 'old failure', 1, NULL)
            """
        )
        db.commit()

        async def fail_browser_session(**_):
            raise AssertionError("completed posts should not open a browser")

        monkeypatch.setattr("tool.ig.profile_scan.browser_session", fail_browser_session)

        await download_scanned_profile("alice", db, media_dir=media_dir)

        unresolved = db.execute("SELECT COUNT(*) FROM profile_failure WHERE resolved_at IS NULL").fetchone()[0]
        assert unresolved == 0

    @pytest.mark.asyncio
    async def test_download_uses_stored_metadata_without_direct_post_tab(self, tmp_path, monkeypatch):
        db = init_db(":memory:")
        media_dir = tmp_path / "images"
        save_profile_posts_metadata(
            db,
            "alice",
            [ScrapedPost("A1", "image", ["https://cdn/image.jpg"], username="owner", caption="cap")],
        )
        bootstrap_tab = _DownloadTab()
        saved = []

        @asynccontextmanager
        async def browser_session(**_):
            yield object(), bootstrap_tab

        async def fail_new_tab(*_args, **_kwargs):
            raise AssertionError("stored metadata downloads should not open direct post tabs")

        async def fake_navigate(_tab, _url):
            pass

        async def noop(*_args, **_kwargs):
            pass

        async def fake_save_post(*args, **kwargs):
            saved.append((args, kwargs))
            (media_dir / "A1").mkdir(parents=True)
            (media_dir / "A1" / "A1.jpg").write_bytes(b"image")
            db.execute("UPDATE post SET scraped_at = 1 WHERE shortid = 'A1'")
            db.commit()

        monkeypatch.setattr("tool.ig.profile_scan.browser_session", browser_session)
        monkeypatch.setattr("tool.ig.profile_scan.new_tab", fail_new_tab)
        monkeypatch.setattr("tool.ig.profile_scan.navigate_to", fake_navigate)
        monkeypatch.setattr("tool.ig.profile_scan.wait_ready", noop)
        monkeypatch.setattr("tool.ig.profile_scan.raise_if_login", lambda _href: None)
        monkeypatch.setattr("tool.ig.profile_scan.save_post", fake_save_post)

        async def no_sleep(*_args, **_kwargs):
            pass

        monkeypatch.setattr("tool.ig.profile_scan.asyncio.sleep", no_sleep)

        await download_scanned_profile("alice", db, media_dir=media_dir)

        assert len(saved) == 1
        args, kwargs = saved[0]
        assert args[:6] == (
            db,
            "A1",
            "owner",
            "cap",
            "image",
            ["https://cdn/image.jpg"],
        )
        assert kwargs["profile_username"] == "alice"

    @pytest.mark.asyncio
    async def test_stored_video_metadata_uses_authenticated_hq_save(self, tmp_path, monkeypatch):
        db = init_db(":memory:")
        low_url = _video_url("ig-xpvds.clips.c2-C3.dash_baseline_1_v1")
        post = ScrapedPost("V1", "video", [low_url], username="owner", caption="cap")
        save_profile_posts_metadata(db, "alice", [post])
        calls = []

        async def fake_save_video(*args, **kwargs):
            calls.append((args, kwargs))

        monkeypatch.setattr("tool.ig.profile_scan.save_video_post_media", fake_save_video)

        await _download_stored_post_metadata(
            _DownloadTab(),
            db,
            ProfileScope("alice"),
            post,
            media_dir=tmp_path / "images",
        )

        assert len(calls) == 1
        assert calls[0][0][2] is post
        assert calls[0][1]["hq_video"] is True

    @pytest.mark.asyncio
    async def test_completed_low_quality_video_is_pending_for_hq_upgrade(self, tmp_path):
        db = init_db(":memory:")
        low_url = _video_url("ig-xpvds.clips.c2-C3.dash_baseline_1_v1")
        save_profile_posts_metadata(
            db,
            "alice",
            [ScrapedPost("V1", "video", [low_url], username="owner", caption="cap")],
        )
        path = media_path("V1", "video", 0, 1, tmp_path / "images", low_url)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"existing video")

        assert _pending_profile_shortids(db, ProfileScope("alice"), ["V1"], media_dir=tmp_path / "images") == ["V1"]

    @pytest.mark.asyncio
    async def test_download_refreshes_direct_post_when_stored_metadata_download_fails(
        self, tmp_path, monkeypatch, capsys
    ):
        db = init_db(":memory:")
        media_dir = tmp_path / "images"
        save_profile_posts_metadata(
            db,
            "alice",
            [ScrapedPost("A1", "image", ["https://cdn/expired.jpg"], username="owner", caption="cap")],
        )
        bootstrap_tab = _DownloadTab()
        direct_tabs = []

        @asynccontextmanager
        async def browser_session(**_):
            yield object(), bootstrap_tab

        async def new_tab(*_args, **_kwargs):
            tab = _DownloadTab()
            direct_tabs.append(tab)
            return tab

        async def fake_navigate(_tab, _url):
            pass

        async def noop(*_args, **_kwargs):
            pass

        async def stale_save_post(*_args, **_kwargs):
            raise RuntimeError("A1: 1 download(s) failed: HTTP Error 403: Forbidden")

        async def scrape_post(_tab, shortid, db, **kwargs):
            (media_dir / shortid[-2:]).mkdir(parents=True)
            (media_dir / shortid[-2:] / f"{shortid}.jpg").write_bytes(b"image")
            async with kwargs["db_lock"]:
                db.execute(
                    """
                    UPDATE post
                    SET username = 'owner', caption = 'cap', post_type = 'image', scraped_at = 1
                    WHERE shortid = ?
                    """,
                    (shortid,),
                )
                db.execute("DELETE FROM media WHERE shortid = ?", (shortid,))
                db.execute("INSERT INTO media (shortid, idx, url) VALUES (?, 0, 'https://cdn/fresh.jpg')", (shortid,))
                db.commit()

        monkeypatch.setattr("tool.ig.profile_scan.browser_session", browser_session)
        monkeypatch.setattr("tool.ig.profile_scan.new_tab", new_tab)
        monkeypatch.setattr("tool.ig.profile_scan.navigate_to", fake_navigate)
        monkeypatch.setattr("tool.ig.profile_scan.wait_ready", noop)
        monkeypatch.setattr("tool.ig.profile_scan.raise_if_login", lambda _href: None)
        monkeypatch.setattr("tool.ig.profile_scan.save_post", stale_save_post)
        monkeypatch.setattr("tool.ig.profile_scan.scrape_post_in_tab", scrape_post)
        monkeypatch.setattr("tool.ig.profile_scan._auto_follow_profile", noop)

        async def no_sleep(*_args, **_kwargs):
            pass

        monkeypatch.setattr("tool.ig.profile_scan.asyncio.sleep", no_sleep)

        await download_scanned_profile("alice", db, media_dir=media_dir)

        assert len(direct_tabs) == 1
        assert db.execute("SELECT url FROM media WHERE shortid = 'A1'").fetchone()[0] == "https://cdn/fresh.jpg"
        assert db.execute("SELECT COUNT(*) FROM profile_failure WHERE resolved_at IS NULL").fetchone()[0] == 0
        out = capsys.readouterr().out
        assert "stored profile metadata failed (RuntimeError); refreshing direct post" in out
        assert "[1/1] A1: completed" in out

    @pytest.mark.asyncio
    async def test_download_jobs_process_direct_posts_concurrently_with_serialized_db_writes(
        self, tmp_path, monkeypatch, capsys
    ):
        db = init_db(":memory:")
        save_profile_shortids(db, "alice", ["A1", "A2", "A3"])
        media_dir = tmp_path / "images"
        active_scrapes = 0
        max_active_scrapes = 0
        active_writes = 0
        max_active_writes = 0

        @asynccontextmanager
        async def browser_session(**_):
            yield object(), _DownloadTab()

        async def new_tab(*_args, **_kwargs):
            return _DownloadTab()

        async def scrape_post(_tab, shortid, db, **kwargs):
            nonlocal active_scrapes, max_active_scrapes, active_writes, max_active_writes
            kwargs["reporter"].progress("test scrape running")
            active_scrapes += 1
            max_active_scrapes = max(max_active_scrapes, active_scrapes)
            await asyncio.sleep(0)
            async with kwargs["db_lock"]:
                kwargs["reporter"].progress("test DB write running")
                active_writes += 1
                max_active_writes = max(max_active_writes, active_writes)
                await asyncio.sleep(0)
                (media_dir / shortid[-2:]).mkdir(parents=True, exist_ok=True)
                (media_dir / shortid[-2:] / f"{shortid}.jpg").write_bytes(b"image")
                db.execute(
                    """
                    UPDATE post
                    SET username = 'owner', caption = 'cap', post_type = 'image', scraped_at = 1
                    WHERE shortid = ?
                    """,
                    (shortid,),
                )
                db.execute("INSERT INTO media (shortid, idx, url) VALUES (?, 0, 'https://cdn/image.jpg')", (shortid,))
                db.commit()
                active_writes -= 1
            active_scrapes -= 1

        async def noop(*_):
            pass

        monkeypatch.setattr("tool.ig.profile_scan.browser_session", browser_session)
        monkeypatch.setattr("tool.ig.profile_scan.new_tab", new_tab)
        monkeypatch.setattr("tool.ig.profile_scan.scrape_post_in_tab", scrape_post)
        monkeypatch.setattr("tool.ig.profile_scan._auto_follow_profile", noop)

        pace = Pace(inter_post=(0, 0), skip_post=(0, 0), resume_cooldown=(0, 0), modal_think=(0, 0), slide_render=0)
        await download_scanned_profile("alice", db, media_dir=media_dir, jobs=2, pace=pace)

        assert max_active_scrapes == 2
        assert max_active_writes == 1
        out = capsys.readouterr().out
        assert "[1/3] A1: started scanned post" in out
        assert "[1/3] A1: test scrape running" in out
        assert "[1/3] A1: test DB write running" in out
        assert "[1/3] A1: completed" in out

    @pytest.mark.asyncio
    async def test_download_jobs_cancel_active_workers_on_first_rate_limit(self, tmp_path, monkeypatch):
        db = init_db(":memory:")
        save_profile_shortids(db, "alice", ["A1", "A2", "A3", "A4"])
        media_dir = tmp_path / "images"
        started = []
        cancelled = []

        @asynccontextmanager
        async def browser_session(**_):
            yield object(), _DownloadTab()

        async def new_tab(*_args, **_kwargs):
            return _DownloadTab()

        async def scrape_post(_tab, shortid, _db, **_kwargs):
            started.append(shortid)
            if shortid == "A1":
                await asyncio.sleep(0)
                raise RateLimitError(shortid, retry_after=120)
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.append(shortid)
                raise

        async def noop(*_):
            pass

        monkeypatch.setattr("tool.ig.profile_scan.browser_session", browser_session)
        monkeypatch.setattr("tool.ig.profile_scan.new_tab", new_tab)
        monkeypatch.setattr("tool.ig.profile_scan.scrape_post_in_tab", scrape_post)
        monkeypatch.setattr("tool.ig.profile_scan._auto_follow_profile", noop)

        pace = Pace(inter_post=(0, 0), skip_post=(0, 0), resume_cooldown=(0, 0), modal_think=(0, 0), slide_render=0)
        await download_scanned_profile("alice", db, media_dir=media_dir, jobs=3, pace=pace)

        assert started == ["A1", "A2", "A3"]
        assert sorted(cancelled) == ["A2", "A3"]
        failures = db.execute(
            "SELECT shortid, reason FROM profile_failure WHERE resolved_at IS NULL ORDER BY shortid"
        ).fetchall()
        assert failures == [("A1", "RateLimitError: Instagram returned HTTP 429 for A1; backing off")]
