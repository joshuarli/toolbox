"""Unit tests for tool.ig.scraper — pure functions and DB helpers.

All tests are fully offline: no browser, no network, no real filesystem paths
(file-existence checks use tmp_path or monkeypatching).
"""

import base64
import json
import sqlite3
import urllib.error
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tool.ig.db import init_db
from tool.ig.hq_video import (
    HqVideoFetchError,
    _download_hq_media_json,
    _parse_hq_video_tracks,
    instagram_api_user_agent,
    resolve_hq_video_tracks,
    shortcode_to_media_id,
)
from tool.ig.media import dest_path, media_path
from tool.ig.scraper import (
    SegmentCapture,
    _best_audio_url,
    _best_video_url,
    _download,
    extract_shortid,
    is_ig_image_url,
    mux_av,
    raise_if_login,
    save_post,
    scrape_direct,
)

# ---------------------------------------------------------------------------
# extract_shortid
# ---------------------------------------------------------------------------


class TestExtractShortid:
    def test_full_post_url(self):
        assert extract_shortid("https://www.instagram.com/p/ABC123/") == "ABC123"

    def test_url_without_trailing_slash(self):
        assert extract_shortid("https://www.instagram.com/p/ABC123") == "ABC123"

    def test_url_with_query_string(self):
        assert extract_shortid("https://www.instagram.com/p/ABC123/?img_index=2") == "ABC123"

    def test_reel_url(self):
        assert extract_shortid("https://www.instagram.com/reel/DQe4PJvkkB8/") == "DQe4PJvkkB8"

    def test_bare_shortid(self):
        assert extract_shortid("ABC123") == "ABC123"

    def test_shortid_with_hyphens_and_underscores(self):
        assert extract_shortid("Ab-C_12") == "Ab-C_12"

    def test_profile_post_url(self):
        # URL with username prefix before /p/
        assert extract_shortid("https://www.instagram.com/antiques_seed/p/DYEeLM9ETfZ/") == "DYEeLM9ETfZ"

    def test_profile_reel_url(self):
        assert extract_shortid("https://www.instagram.com/porcelainlung/reel/DQe4PJvkkB8/") == "DQe4PJvkkB8"

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="cannot extract shortid"):
            extract_shortid("https://www.instagram.com/antiques_seed/")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="cannot extract shortid"):
            extract_shortid("")


class TestImageUrlFilter:
    def test_current_post_image_cdn_path_accepted(self):
        url = "https://scontent.cdninstagram.com/v/t51.75761-15/photo.jpg?stp=dst-jpg_e35_p1080x1080_tt6"
        assert is_ig_image_url(url)

    def test_older_post_image_cdn_path_accepted(self):
        url = "https://scontent.cdninstagram.com/v/t39.30808-6/photo.jpg?stp=dst-jpg_e35_p1080x1080_tt6"
        assert is_ig_image_url(url)

    def test_profile_avatar_path_rejected(self):
        url = "https://scontent.cdninstagram.com/v/t51.82787-19/avatar.jpg?stp=dst-jpg_s150x150_tt6"
        assert not is_ig_image_url(url)


# ---------------------------------------------------------------------------
# raise_if_login
# ---------------------------------------------------------------------------


class TestRaiseIfLogin:
    def test_login_redirect_raises(self):
        with pytest.raises(RuntimeError, match="Redirected to login"):
            raise_if_login("https://www.instagram.com/accounts/login/?next=/p/Abc/")

    def test_normal_url_does_not_raise(self):
        raise_if_login("https://www.instagram.com/p/ABC123/")

    def test_profile_url_does_not_raise(self):
        raise_if_login("https://www.instagram.com/antiques_seed/")


# ---------------------------------------------------------------------------
# dest_path
# ---------------------------------------------------------------------------


class TestDest:
    def test_single_image(self):
        assert dest_path("ABC", 0, 1, ".jpg") == Path("site/images/bc/ABC.jpg")

    def test_single_video(self):
        assert dest_path("ABC", 0, 1, ".mp4") == Path("site/images/bc/ABC.mp4")

    def test_carousel_first(self):
        assert dest_path("ABC", 0, 3, ".jpg") == Path("site/images/bc/ABC_1.jpg")

    def test_carousel_last(self):
        assert dest_path("ABC", 2, 3, ".jpg") == Path("site/images/bc/ABC_3.jpg")

    def test_carousel_index_is_one_based(self):
        # idx=0 → _1, idx=1 → _2, etc.
        for i in range(5):
            assert dest_path("X", i, 5, ".jpg") == Path(f"site/images/x/X_{i + 1}.jpg")

    def test_custom_media_dir(self, tmp_path):
        assert dest_path("ABC", 0, 1, ".jpg", media_dir=tmp_path / "media") == tmp_path / "media" / "bc" / "ABC.jpg"

    def test_rejects_profile_path_traversal(self, tmp_path):
        with pytest.raises(ValueError, match="outside the media directory"):
            dest_path("ABC", 0, 1, ".jpg", media_dir=tmp_path / "media", profile="../../outside")

    def test_rejects_profile_symlink_escape(self, tmp_path):
        media_dir = tmp_path / "media"
        outside = tmp_path / "outside"
        (media_dir / "by-profile").mkdir(parents=True)
        outside.mkdir()
        (media_dir / "by-profile" / "alice").symlink_to(outside, target_is_directory=True)

        with pytest.raises(ValueError, match="outside the media directory"):
            dest_path("ABC", 0, 1, ".jpg", media_dir=media_dir, profile="alice")

    def test_mixed_carousel_path_uses_url_extension(self):
        path = media_path("ABC", "carousel", 0, 2, url="https://cdn/video.mp4")

        assert path == Path("site/images/bc/ABC_1.mp4")


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------


class TestInitDb:
    def test_creates_post_and_media_tables(self):
        db = init_db(":memory:")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "post" in tables
        assert "media" in tables

    def test_post_columns(self):
        db = init_db(":memory:")
        cols = {r[1] for r in db.execute("PRAGMA table_info(post)")}
        assert cols == {"shortid", "username", "caption", "post_type", "scraped_at"}

    def test_media_columns(self):
        db = init_db(":memory:")
        cols = {r[1] for r in db.execute("PRAGMA table_info(media)")}
        assert cols == {"shortid", "idx", "url"}

    def test_profile_post_columns(self):
        db = init_db(":memory:")
        cols = {r[1] for r in db.execute("PRAGMA table_info(profile_post)")}
        assert cols == {"username", "saved", "shortid", "seen_at", "seq"}

    def test_migrates_profile_post_saved_scope(self, tmp_path):
        db_path = tmp_path / "old.db"
        db = sqlite3.connect(db_path)
        db.executescript(
            """
            CREATE TABLE post (
                shortid    TEXT PRIMARY KEY,
                username   TEXT,
                caption    TEXT,
                post_type  TEXT,
                scraped_at INTEGER
            ) WITHOUT ROWID;
            CREATE TABLE profile_post (
                username TEXT NOT NULL,
                shortid  TEXT NOT NULL REFERENCES post(shortid),
                seen_at  INTEGER NOT NULL,
                seq      INTEGER NOT NULL,
                PRIMARY KEY(username, shortid)
            ) WITHOUT ROWID;
            INSERT INTO post VALUES ('X', NULL, NULL, NULL, NULL);
            INSERT INTO profile_post VALUES ('alice', 'X', 1, 0);
            """
        )
        db.commit()
        db.close()

        migrated = init_db(str(db_path))

        cols = {r[1] for r in migrated.execute("PRAGMA table_info(profile_post)")}
        row = migrated.execute("SELECT username, saved, shortid, seen_at, seq FROM profile_post").fetchone()
        assert cols == {"username", "saved", "shortid", "seen_at", "seq"}
        assert row == ("alice", 0, "X", 1, 0)
        migrated.execute("INSERT INTO profile_post VALUES ('alice', 1, 'X', 2, 0)")
        migrated.commit()
        assert migrated.execute("SELECT COUNT(*) FROM profile_post WHERE username = 'alice'").fetchone()[0] == 2
        migrated.close()

    def test_recovers_memberships_from_interrupted_rename(self, tmp_path):
        db_path = tmp_path / "interrupted.db"
        db = sqlite3.connect(db_path)
        db.executescript(
            """
            CREATE TABLE post (
                shortid    TEXT PRIMARY KEY,
                username   TEXT,
                caption    TEXT,
                post_type  TEXT,
                scraped_at INTEGER
            ) WITHOUT ROWID;
            CREATE TABLE profile_post_old (
                username TEXT NOT NULL,
                shortid  TEXT NOT NULL REFERENCES post(shortid),
                seen_at  INTEGER NOT NULL,
                seq      INTEGER NOT NULL,
                PRIMARY KEY(username, shortid)
            ) WITHOUT ROWID;
            INSERT INTO post VALUES ('OLD1', NULL, NULL, NULL, NULL);
            INSERT INTO profile_post_old VALUES ('alice', 'OLD1', 10, 3);
            """
        )
        db.commit()
        db.close()

        migrated = init_db(str(db_path))
        try:
            rows = migrated.execute(
                "SELECT username, saved, shortid, seen_at, seq FROM profile_post"
            ).fetchall()
            assert rows == [("alice", 0, "OLD1", 10, 3)]
            assert migrated.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'profile_post_old'"
            ).fetchone() is None
        finally:
            migrated.close()

    def test_recovers_old_and_saved_memberships_from_interrupted_copy(self, tmp_path):
        db_path = tmp_path / "interrupted-copy.db"
        db = sqlite3.connect(db_path)
        db.executescript(
            """
            CREATE TABLE post (
                shortid    TEXT PRIMARY KEY,
                username   TEXT,
                caption    TEXT,
                post_type  TEXT,
                scraped_at INTEGER
            ) WITHOUT ROWID;
            CREATE TABLE profile_post_old (
                username TEXT NOT NULL,
                shortid  TEXT NOT NULL REFERENCES post(shortid),
                seen_at  INTEGER NOT NULL,
                seq      INTEGER NOT NULL,
                PRIMARY KEY(username, shortid)
            ) WITHOUT ROWID;
            CREATE TABLE profile_post (
                username TEXT NOT NULL,
                saved    INTEGER NOT NULL DEFAULT 0,
                shortid  TEXT NOT NULL REFERENCES post(shortid),
                seen_at  INTEGER NOT NULL,
                seq      INTEGER NOT NULL,
                PRIMARY KEY(username, saved, shortid)
            ) WITHOUT ROWID;
            INSERT INTO post VALUES ('OLD1', NULL, NULL, NULL, NULL);
            INSERT INTO post VALUES ('SAVED1', NULL, NULL, NULL, NULL);
            INSERT INTO profile_post_old VALUES ('alice', 'OLD1', 10, 3);
            INSERT INTO profile_post VALUES ('alice', 1, 'SAVED1', 11, 0);
            """
        )
        db.commit()
        db.close()

        migrated = init_db(str(db_path))
        try:
            rows = migrated.execute(
                "SELECT username, saved, shortid, seen_at, seq FROM profile_post ORDER BY saved, shortid"
            ).fetchall()
            assert rows == [("alice", 0, "OLD1", 10, 3), ("alice", 1, "SAVED1", 11, 0)]
            assert migrated.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'profile_post_old'"
            ).fetchone() is None
        finally:
            migrated.close()

    def test_profile_post_migration_failure_leaves_original_schema_and_rows(self, tmp_path):
        db_path = tmp_path / "failed-migration.db"
        db = sqlite3.connect(db_path)
        db.executescript(
            """
            CREATE TABLE post (
                shortid    TEXT PRIMARY KEY,
                username   TEXT,
                caption    TEXT,
                post_type  TEXT,
                scraped_at INTEGER
            ) WITHOUT ROWID;
            CREATE TABLE profile_post (
                username TEXT NOT NULL,
                shortid  TEXT NOT NULL REFERENCES post(shortid),
                seen_at  INTEGER,
                seq      INTEGER NOT NULL,
                PRIMARY KEY(username, shortid)
            ) WITHOUT ROWID;
            INSERT INTO post VALUES ('BAD1', NULL, NULL, NULL, NULL);
            INSERT INTO profile_post VALUES ('alice', 'BAD1', NULL, 0);
            """
        )
        db.commit()
        db.close()

        with pytest.raises(sqlite3.IntegrityError):
            init_db(str(db_path))

        check = sqlite3.connect(db_path)
        try:
            cols = {row[1] for row in check.execute("PRAGMA table_info(profile_post)")}
            row = check.execute("SELECT username, shortid, seen_at, seq FROM profile_post").fetchone()
            old_table = check.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'profile_post_old'"
            ).fetchone()
            staging_table = check.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'profile_post_new'"
            ).fetchone()
            assert "saved" not in cols
            assert row == ("alice", "BAD1", None, 0)
            assert old_table is None
            assert staging_table is None
        finally:
            check.close()

    def test_idempotent(self):
        # Calling a second time on the same file should not raise.
        # Use a real file so we can open twice.
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name
        try:
            db1 = init_db(path)
            db1.close()
            db2 = init_db(path)
            db2.close()
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# save_post
# ---------------------------------------------------------------------------


class TestSegmentCapture:
    def test_assembles_complete_progressive_mp4_ranges(self):
        capture = SegmentCapture()
        url = _video_url("ig-xpvds.clips.c2-C3.dash_baseline_1_v1", duration=90, bitrate=937348)
        capture.add(url, 3, b"def", 6)
        capture.add(url, 0, b"abc", 6)
        bases = capture.get_video_bases()
        assert bases == [url]
        assert capture.is_complete(url)
        assert capture.assemble(url) == b"abcdef"

    def test_empty_media_observation_still_exposes_cdn_base(self):
        capture = SegmentCapture()
        url = _video_url("ig-xpvds.clips.c2-C3.dash_baseline_1_v1", duration=15, bitrate=199414)
        capture.add(url + "&bytestart=0&byteend=99", 0, b"", None)

        bases = capture.get_video_bases()
        assert bases == [url]
        assert not capture.is_complete(url)


def _video_url(tag: str, *, duration: int, bitrate: int) -> str:
    efg = (
        base64.urlsafe_b64encode(json.dumps({"vencode_tag": tag, "duration_s": duration, "bitrate": bitrate}).encode())
        .decode()
        .rstrip("=")
    )
    return f"https://cdninstagram.com/o1/v/t2/f2/video-{duration}-{bitrate}.mp4?efg={efg}"


class TestVideoUrlSelection:
    def test_prefers_highest_bitrate_full_duration_video(self):
        low = _video_url("ig-xpvds.clips.c2-C3.dash_baseline_3_v1", duration=90, bitrate=199414)
        high = _video_url("ig-xpvds.clips.c2-C3.dash_baseline_1_v1", duration=90, bitrate=937348)
        short = _video_url("ig-xpvds.clips.c2-C3.dash_baseline_1_v1", duration=15, bitrate=1200000)

        assert _best_video_url([low, short, high]) == high

    def test_ignores_audio_tracks(self):
        video = _video_url("ig-xpvds.clips.c2-C3.dash_baseline_1_v1", duration=90, bitrate=937348)
        audio = _video_url("ig-xpvds.clips.c2-C3.dash_ln_heaac_vbr3_audio", duration=90, bitrate=72785)

        assert _best_video_url([audio, video]) == video
        assert _best_audio_url([audio, video]) == audio

    def test_prefers_higher_bitrate_vp9_video_over_low_h264_preview(self):
        low = _video_url("ig-xpvds.clips.c2-C3.dash_baseline_3_v1", duration=5, bitrate=132362)
        high = _video_url("ig-xpvds.clips.c2-C3.dash_vp9_1080p_v1", duration=5, bitrate=651637)

        assert _best_video_url([low, high]) == high

    def test_prefers_higher_resolution_vp9_when_h264_bitrate_is_higher(self):
        # This is the shape of Db706nYIUZk: Instagram's 988x720 H.264
        # progressive stream has a higher nominal bitrate than its 1484x1080
        # VP9 DASH stream, so bitrate-only ranking picks the wrong file.
        low = _video_url(
            "ig-xpvds.clips.c2-C3.dash_baseline_1_v1",
            duration=48,
            bitrate=1166481,
        )
        high = _video_url(
            "ig-xpvds.clips.igwww-C3.dash_vp9-basic-gen2_1080p",
            duration=48,
            bitrate=1022275,
        )

        assert _best_video_url([low, high]) == high

    def test_prefers_higher_bitrate_audio_track(self):
        low = _video_url("ig-xpvds.clips.c2-C3.dash_ln_heaac_vbr3_audio", duration=5, bitrate=32000)
        high = _video_url("ig-xpvds.clips.c2-C3.dash_ln_heaac_vbr4_audio", duration=5, bitrate=69340)
        video = _video_url("ig-xpvds.clips.c2-C3.dash_vp9_1080p_v1", duration=5, bitrate=651637)

        assert _best_audio_url([low, high, video]) == high


@pytest.mark.asyncio
async def test_scrape_direct_keeps_video_and_images_in_mixed_carousel(monkeypatch):
    video = _video_url("ig-xpvds.carousel_item.c2-C3.dash_baseline_1_v1", duration=6, bitrate=840187)
    images = [
        "https://scontent.cdninstagram.com/v/t51.75761-15/one.jpg?stp=dst-jpg_e35_p1080x1080_tt6",
        "https://scontent.cdninstagram.com/v/t51.75761-15/two.jpg?stp=dst-jpg_e35_p1080x1080_tt6",
    ]

    from tool.ig import scraper

    async def fake_evaluate(js):
        if js == "window.location.href":
            return "https://www.instagram.com/p/MIX/"
        if js == scraper._SIGNAL_JS:
            return True
        if js == scraper._SCRAPE_JS:
            return {"type": "image", "username": "u", "caption": "c", "mediaUrls": images[:1]}
        if js == scraper._SCRIPT_SLIDE_URLS_JS:
            return images
        if js == scraper._SCRIPT_VIDEO_URLS_JS:
            return [video]
        if js == scraper._PERFORMANCE_VIDEO_URLS_JS:
            return []
        if js == '!!document.querySelector("article video, main video")':
            return True
        if js == "!!document.querySelector('article button[aria-label=\"Next\"]')":
            return True
        return None

    tab = MagicMock()
    tab.evaluate = AsyncMock(side_effect=fake_evaluate)
    monkeypatch.setattr("tool.ig.scraper.think", AsyncMock())

    post = await scrape_direct(tab, "MIX")

    assert post is not None
    assert post.post_type == "carousel"
    assert post.media_urls == [video, *images]


class TestHqVideoTracks:
    def test_api_user_agent_is_chrome(self):
        assert "Chrome/" in instagram_api_user_agent()
        assert "Firefox/" not in instagram_api_user_agent()

    def test_parse_hq_video_tracks_selects_largest_video_and_best_audio(self):
        manifest = """
        <MPD xmlns="urn:mpeg:dash:schema:mpd:2011">
          <Period>
            <AdaptationSet mimeType="video/mp4" contentType="video">
              <Representation id="v720" width="720" height="1280" bandwidth="378276" codecs="vp09">
                <BaseURL>https://cdn/v720.mp4?x=1&amp;y=2</BaseURL>
              </Representation>
              <Representation id="v1080" width="1080" height="1920" bandwidth="580637" codecs="vp09">
                <BaseURL>https://cdn/v1080.mp4</BaseURL>
              </Representation>
            </AdaptationSet>
            <AdaptationSet mimeType="audio/mp4" contentType="audio">
              <Representation id="a1" bandwidth="32000" codecs="mp4a.40.5">
                <BaseURL>https://cdn/a-low.mp4</BaseURL>
              </Representation>
              <Representation id="a2" bandwidth="73400" codecs="mp4a.40.5">
                <BaseURL>https://cdn/a-high.mp4</BaseURL>
              </Representation>
            </AdaptationSet>
          </Period>
        </MPD>
        """

        tracks = _parse_hq_video_tracks(manifest)

        assert tracks is not None
        assert tracks.video_url == "https://cdn/v1080.mp4"
        assert tracks.audio_url == "https://cdn/a-high.mp4"
        assert tracks.width == 1080
        assert tracks.height == 1920

    @pytest.mark.asyncio
    async def test_resolve_hq_video_tracks_requires_authenticated_browser_cookie(self):
        tab = MagicMock()
        tab.get_cookies = AsyncMock(return_value={"cookies": []})

        with pytest.raises(HqVideoFetchError, match="authenticated browser cookies"):
            await resolve_hq_video_tracks(tab, "TESTID")

    @pytest.mark.asyncio
    async def test_resolve_hq_video_tracks_detects_http_errors(self, monkeypatch):
        tab = MagicMock()
        tab.get_cookies = AsyncMock(
            return_value={
                "cookies": [
                    {"domain": ".instagram.com", "name": "sessionid", "value": {"type": "string", "value": "s"}},
                    {"domain": ".instagram.com", "name": "csrftoken", "value": {"type": "string", "value": "c"}},
                ]
            }
        )

        def fail(*_):
            raise HqVideoFetchError("HQ video metadata HTTP 429", status=429, retry_after=120, url="https://x")

        monkeypatch.setattr("tool.ig.hq_video._download_hq_media_json", fail)

        with pytest.raises(HqVideoFetchError) as exc_info:
            await resolve_hq_video_tracks(tab, "TESTID")

        assert exc_info.value.status == 429
        assert exc_info.value.retry_after == 120

    @pytest.mark.asyncio
    async def test_resolve_hq_video_tracks_returns_none_when_media_items_missing(self, monkeypatch):
        tab = MagicMock()
        tab.get_cookies = AsyncMock(
            return_value={
                "cookies": [
                    {"domain": ".instagram.com", "name": "sessionid", "value": {"type": "string", "value": "s"}},
                    {"domain": ".instagram.com", "name": "csrftoken", "value": {"type": "string", "value": "c"}},
                ]
            }
        )

        monkeypatch.setattr(
            "tool.ig.hq_video._download_hq_media_json",
            lambda _cookies, _shortid: {"status": "ok"},
        )

        assert await resolve_hq_video_tracks(tab, "TESTID") is None

    @pytest.mark.asyncio
    async def test_resolve_hq_video_tracks_returns_none_when_manifest_missing(self, monkeypatch):
        tab = MagicMock()
        tab.get_cookies = AsyncMock(
            return_value={
                "cookies": [
                    {"domain": ".instagram.com", "name": "sessionid", "value": {"type": "string", "value": "s"}},
                    {"domain": ".instagram.com", "name": "csrftoken", "value": {"type": "string", "value": "c"}},
                ]
            }
        )

        monkeypatch.setattr(
            "tool.ig.hq_video._download_hq_media_json",
            lambda _cookies, _shortid: {"items": [{"code": "TESTID", "media_type": 2}]},
        )

        assert await resolve_hq_video_tracks(tab, "TESTID") is None

    @pytest.mark.asyncio
    async def test_resolve_hq_video_tracks_parses_manifest_from_media_items(self, monkeypatch):
        manifest = """
        <MPD xmlns="urn:mpeg:dash:schema:mpd:2011">
          <AdaptationSet mimeType="video/mp4" contentType="video">
            <Representation id="v720" width="720" height="1280" bandwidth="378276" codecs="vp09">
              <BaseURL>https://cdn/v720.mp4</BaseURL>
            </Representation>
            <Representation id="v1080" width="1080" height="1920" bandwidth="580637" codecs="vp09">
              <BaseURL>https://cdn/v1080.mp4</BaseURL>
            </Representation>
          </AdaptationSet>
          <AdaptationSet mimeType="audio/mp4" contentType="audio">
            <Representation id="a1" bandwidth="73400" codecs="mp4a.40.5">
              <BaseURL>https://cdn/a-high.mp4</BaseURL>
            </Representation>
          </AdaptationSet>
        </MPD>
        """
        tab = MagicMock()
        tab.get_cookies = AsyncMock(
            return_value={
                "cookies": [
                    {"domain": ".instagram.com", "name": "sessionid", "value": {"type": "string", "value": "s"}},
                    {"domain": ".instagram.com", "name": "csrftoken", "value": {"type": "string", "value": "c"}},
                ]
            }
        )

        monkeypatch.setattr(
            "tool.ig.hq_video._download_hq_media_json",
            lambda _cookies, _shortid: {"items": [{"video_dash_manifest": manifest}]},
        )

        tracks = await resolve_hq_video_tracks(tab, "TESTID")
        assert tracks is not None
        assert tracks.video_url == "https://cdn/v1080.mp4"
        assert tracks.audio_url == "https://cdn/a-high.mp4"
        assert tracks.width == 1080

    def test_shortcode_to_media_id_decodes_base64_alphabet(self):
        # "TESTID" round-trips through the same alphabet yt-dlp uses.
        assert shortcode_to_media_id("TESTID") == str(int(shortcode_to_media_id("TESTID")))

    def test_shortcode_to_media_id_rejects_invalid_characters(self):
        with pytest.raises(HqVideoFetchError, match="invalid shortcode"):
            shortcode_to_media_id("bad!shortid")

    def test_hq_media_json_uses_media_id_and_retries_transient_http_errors(self, monkeypatch):
        response_mock = MagicMock()
        response_mock.read.return_value = b'{"items": [{"code": "TESTID"}]}'
        response_mock.__enter__ = lambda s: s
        response_mock.__exit__ = MagicMock(return_value=False)
        err = urllib.error.HTTPError("https://x", 502, "bad gateway", {}, None)
        calls = []

        def fake_urlopen(req, **kwargs):
            calls.append(req)
            if len(calls) == 1:
                raise err
            return response_mock

        monkeypatch.setattr("tool.ig.hq_video.time.sleep", lambda _delay: None)

        result = None
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = _download_hq_media_json({"sessionid": "s", "csrftoken": "c"}, "TESTID")

        assert result == {"items": [{"code": "TESTID"}]}
        assert len(calls) == 2
        assert str(calls[-1].full_url) == (
            "https://www.instagram.com/api/v1/media/{}/info/".format(shortcode_to_media_id("TESTID"))
        )
        assert any(name.lower() == "x-asbd-id" and value == "359341" for name, value in calls[-1].headers.items())
        assert "Chrome/" in calls[-1].headers["User-agent"]


@pytest.mark.asyncio
class TestSavePost:
    def _make_db(self) -> sqlite3.Connection:
        return init_db(":memory:")

    def _fake_download(self, url: str, dest: Path, **_kwargs) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Write valid magic bytes so _validate_downloaded_file passes.
        # The dest here is the pending temp path, not the final path, so
        # inspect the URL to determine the expected media type.
        if url.endswith(".mp4"):
            dest.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        else:
            dest.write_bytes(b"\xff\xd8\xff" + url.encode())

    async def test_inserts_post_row(self, tmp_path):
        db = self._make_db()
        media_dir = tmp_path / "media"
        with patch("tool.ig.scraper._download", side_effect=self._fake_download):
            await save_post(db, "ABC", "user1", "hello", "image", ["http://cdn/img.jpg"], ".jpg", media_dir=media_dir)
        row = db.execute("SELECT username, caption, post_type FROM post WHERE shortid='ABC'").fetchone()
        assert row == ("user1", "hello", "image")

    async def test_inserts_media_rows(self, tmp_path):
        db = self._make_db()
        media_dir = tmp_path / "media"
        urls = ["http://cdn/1.jpg", "http://cdn/2.jpg", "http://cdn/3.jpg"]
        with patch("tool.ig.scraper._download", side_effect=self._fake_download):
            await save_post(db, "CAR", "user1", None, "carousel", urls, ".jpg", media_dir=media_dir)
        rows = db.execute("SELECT idx, url FROM media WHERE shortid='CAR' ORDER BY idx").fetchall()
        assert len(rows) == 3
        assert rows[0] == (0, "http://cdn/1.jpg")
        assert rows[2] == (2, "http://cdn/3.jpg")

    async def test_upsert_updates_existing_post(self, tmp_path):
        db = self._make_db()
        media_dir = tmp_path / "media"
        with patch("tool.ig.scraper._download", side_effect=self._fake_download):
            await save_post(
                db, "ABC", "old_user", "old cap", "image", ["http://cdn/img.jpg"], ".jpg", media_dir=media_dir
            )
            await save_post(
                db, "ABC", "new_user", "new cap", "image", ["http://cdn/img.jpg"], ".jpg", media_dir=media_dir
            )
        row = db.execute("SELECT username, caption FROM post WHERE shortid='ABC'").fetchone()
        assert row == ("new_user", "new cap")

    async def test_skips_download_when_file_exists(self, tmp_path):
        db = self._make_db()
        dest = tmp_path / "images" / "BC" / "ABC.jpg"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"fake")

        with patch("tool.ig.scraper.dest_path", return_value=dest), patch("tool.ig.scraper._download") as mock_dl:
            await save_post(
                db,
                "ABC",
                "u",
                None,
                "image",
                ["http://cdn/img.jpg"],
                ".jpg",
                skip_existing_files=True,
                media_dir=tmp_path / "images",
            )
        mock_dl.assert_not_called()

    async def test_reports_muxed_file_as_created_not_cached(self, tmp_path):
        db = self._make_db()
        dest = tmp_path / "images" / "BC" / "ABC.jpg"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"already-created-by-earlier-step")
        reporter = MagicMock()

        with patch("tool.ig.scraper.dest_path", return_value=dest), patch("tool.ig.scraper._download") as mock_dl:
            await save_post(
                db,
                "ABC",
                "u",
                None,
                "image",
                ["http://cdn/img.jpg"],
                ".jpg",
                skip_existing_files=True,
                fresh_files={dest},
                reporter=reporter,
                media_dir=tmp_path / "images",
            )

        mock_dl.assert_not_called()
        assert any("(created)" in call.args[0] for call in reporter.info.call_args_list)
        assert not any("(cached)" in call.args[0] for call in reporter.info.call_args_list)

    async def test_downloads_when_file_missing(self, tmp_path):
        db = self._make_db()
        dest = tmp_path / "images" / "EW" / "NEW.jpg"

        with (
            patch("tool.ig.scraper.dest_path", return_value=dest),
            patch("tool.ig.scraper._download", side_effect=self._fake_download) as mock_dl,
        ):
            await save_post(
                db,
                "NEW",
                "u",
                None,
                "image",
                ["http://cdn/img.jpg"],
                ".jpg",
                skip_existing_files=True,
                media_dir=tmp_path / "images",
            )
        assert mock_dl.call_args.args[0] == "http://cdn/img.jpg"
        assert dest.read_bytes().endswith(b"http://cdn/img.jpg")

    async def test_custom_media_dir_controls_download_path(self, tmp_path):
        db = self._make_db()
        media_dir = tmp_path / "media"

        with patch("tool.ig.scraper._download", side_effect=self._fake_download) as mock_dl:
            await save_post(db, "ABC", "u", None, "image", ["http://cdn/img.jpg"], ".jpg", media_dir=media_dir)

        assert mock_dl.call_args.args[0] == "http://cdn/img.jpg"
        assert (media_dir / "by-profile" / "u" / "ABC.jpg").exists()
        cols = {r[1] for r in db.execute("PRAGMA table_info(media)")}
        assert "path" not in cols

    async def test_rejects_symlink_escape_before_writing(self, tmp_path):
        db = self._make_db()
        media_dir = tmp_path / "media"
        outside = tmp_path / "outside"
        (media_dir / "by-profile").mkdir(parents=True)
        outside.mkdir()
        (media_dir / "by-profile" / "alice").symlink_to(outside, target_is_directory=True)
        url = "https://cdn/image.jpg"

        with pytest.raises(ValueError, match="outside the media directory"):
            await save_post(
                db,
                "SAFE1",
                "owner",
                None,
                "image",
                [url],
                ".jpg",
                captured_bodies={url: b"\xff\xd8\xffimage"},
                media_dir=media_dir,
                profile_username="alice",
            )

        assert list(outside.iterdir()) == []
        assert db.execute("SELECT 1 FROM post WHERE shortid = 'SAFE1'").fetchone() is None

    async def test_mixed_carousel_uses_per_item_extensions(self, tmp_path):
        db = self._make_db()
        media_dir = tmp_path / "media"
        urls = ["https://cdn/video.mp4", "https://cdn/img.jpg"]

        with patch("tool.ig.scraper._download", side_effect=self._fake_download) as mock_dl:
            await save_post(db, "MIX", "u", None, "carousel", urls, ".jpg", media_dir=media_dir)

        assert [call.args[0] for call in mock_dl.call_args_list] == urls
        assert (media_dir / "by-profile" / "u" / "MIX_1.mp4").exists()
        assert (media_dir / "by-profile" / "u" / "MIX_2.jpg").exists()

    async def test_download_failure_does_not_promote_sibling_files_or_media_rows(self, tmp_path):
        db = self._make_db()
        media_dir = tmp_path / "media"

        def fake_download(url: str, dest: Path, **_kwargs) -> None:
            if url.endswith("fail.jpg"):
                raise RuntimeError("download failed")
            self._fake_download(url, dest)

        with (
            patch("tool.ig.scraper._download", side_effect=fake_download),
            pytest.raises(RuntimeError, match="download failed"),
        ):
            await save_post(
                db,
                "PARTIAL",
                "u",
                None,
                "carousel",
                ["https://cdn/ok.jpg", "https://cdn/fail.jpg"],
                ".jpg",
                media_dir=media_dir,
            )

        assert not (media_dir / "AL" / "PARTIAL_1.jpg").exists()
        assert not (media_dir / "AL" / "PARTIAL_2.jpg").exists()
        assert db.execute("SELECT COUNT(*) FROM post WHERE shortid = 'PARTIAL'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM media WHERE shortid = 'PARTIAL'").fetchone()[0] == 0

    async def test_removes_stale_media_rows_when_media_count_shrinks(self, tmp_path):
        db = self._make_db()
        with patch("tool.ig.scraper._download", side_effect=self._fake_download):
            await save_post(
                db,
                "ABC",
                "u",
                None,
                "carousel",
                ["http://cdn/1.jpg", "http://cdn/2.jpg"],
                ".jpg",
                media_dir=tmp_path,
            )
            await save_post(
                db,
                "ABC",
                "u",
                None,
                "video",
                ["http://cdn/1.mp4"],
                ".mp4",
                media_dir=tmp_path,
            )
        rows = db.execute("SELECT idx, url FROM media WHERE shortid='ABC' ORDER BY idx").fetchall()
        assert rows == [(0, "http://cdn/1.mp4")]

    async def test_video_requires_single_final_url(self):
        db = self._make_db()
        with pytest.raises(ValueError, match="expected exactly 1"):
            await save_post(
                db,
                "ABC",
                "u",
                None,
                "video",
                ["http://cdn/1.mp4", "http://cdn/2.mp4"],
                ".mp4",
            )

    async def test_rejects_low_quality_instagram_image_url(self):
        db = self._make_db()
        low = "https://scontent.cdninstagram.com/v/t51.82787-15/x.jpg?stp=dst-jpg_e35_p640x640_tt6"
        with pytest.raises(ValueError, match="low-quality"):
            await save_post(db, "ABC", "u", None, "image", [low], ".jpg")

    async def test_accepts_unconstrained_jpegr_instagram_image_url(self, tmp_path):
        db = self._make_db()
        url = "https://scontent.cdninstagram.com/v/t51.82787-15/x.jpg?stp=dst-jpegr_e35_tt6"
        with patch("tool.ig.scraper._download", side_effect=self._fake_download):
            await save_post(db, "ABC", "u", None, "image", [url], ".jpg", media_dir=tmp_path)

    async def test_accepts_unconstrained_cp_prefixed_instagram_image_url(self, tmp_path):
        db = self._make_db()
        url = "https://scontent.cdninstagram.com/v/t51.82787-15/x.jpg?stp=cp6_dst-jpg_e35_tt6"
        with patch("tool.ig.scraper._download", side_effect=self._fake_download):
            await save_post(db, "ABC", "u", None, "image", [url], ".jpg", media_dir=tmp_path)

    async def test_rejects_low_quality_cp_prefixed_instagram_image_url(self):
        db = self._make_db()
        low = "https://scontent.cdninstagram.com/v/t51.82787-15/x.jpg?stp=cp6_dst-jpg_e35_p640x640_tt6"
        with pytest.raises(ValueError, match="low-quality"):
            await save_post(db, "ABC", "u", None, "image", [low], ".jpg")

    async def test_accepts_large_crop_dimension_instagram_image_url(self, tmp_path):
        db = self._make_db()
        url = "https://scontent.cdninstagram.com/v/t39.30808-6/x.jpg?stp=c0.0.1278.1598a_dst-jpg_e35_tt6"
        with patch("tool.ig.scraper._download", side_effect=self._fake_download):
            await save_post(db, "ABC", "u", None, "image", [url], ".jpg", media_dir=tmp_path)

    async def test_no_media_urls_raises(self):
        db = self._make_db()
        with pytest.raises(ValueError, match="no media URLs"):
            await save_post(db, "EMPTY", "u", None, "image", [], ".jpg")

    async def test_captured_body_written_directly_without_download(self, tmp_path):
        db = self._make_db()
        dest = tmp_path / "images" / "AP" / "CAP.jpg"
        url = "http://cdn/captured.jpg"
        captured_bodies = {url: b"\xff\xd8\xff" + b"captured image data"}

        with patch("tool.ig.scraper.dest_path", return_value=dest), patch("tool.ig.scraper._download") as mock_dl:
            await save_post(
                db,
                "CAP",
                "u",
                None,
                "image",
                [url],
                ".jpg",
                captured_bodies=captured_bodies,
                media_dir=tmp_path / "images",
            )

        mock_dl.assert_not_called()
        assert dest.exists()
        assert dest.read_bytes().startswith(b"\xff\xd8\xff")

    async def test_browser_fetch_written_without_python_download(self, tmp_path):
        db = self._make_db()
        dest = tmp_path / "images" / "ER" / "BROWSER.jpg"
        url = "http://cdn/browser.jpg"

        async def browser_fetch(fetch_url: str) -> bytes:
            assert fetch_url == url
            return b"\xff\xd8\xff" + b"browser image data"

        with patch("tool.ig.scraper.dest_path", return_value=dest), patch("tool.ig.scraper._download") as mock_dl:
            await save_post(
                db,
                "BROWSER",
                "u",
                None,
                "image",
                [url],
                ".jpg",
                browser_fetch=browser_fetch,
                media_dir=tmp_path / "images",
            )

        mock_dl.assert_not_called()
        assert dest.read_bytes().startswith(b"\xff\xd8\xff")

    async def test_browser_fetch_falls_back_to_python_download(self, tmp_path):
        db = self._make_db()
        dest = tmp_path / "images" / "CK" / "FALLBACK.jpg"
        url = "http://cdn/fallback.jpg"

        async def browser_fetch(_url: str) -> bytes:
            raise OSError("browser cache miss")

        with (
            patch("tool.ig.scraper.dest_path", return_value=dest),
            patch("tool.ig.scraper._download", side_effect=self._fake_download) as mock_dl,
        ):
            await save_post(
                db,
                "FALLBACK",
                "u",
                None,
                "image",
                [url],
                ".jpg",
                browser_fetch=browser_fetch,
                media_dir=tmp_path / "images",
            )

        assert mock_dl.call_args.args[0] == url
        assert dest.read_bytes().endswith(url.encode())


def _make_tab_no_next(
    img_index: int = 1,
    dom_paths: list[str] | None = None,
    script_urls: list[str] | None = None,
) -> MagicMock:
    """Tab mock for the trivial case: no Next button on slide 1 → total_slides=1."""

    async def fake_evaluate(js):
        if "PolarisPostRootQuery" in js:
            return script_urls or []
        if "img_index" in js:
            return img_index
        if "length" in js:  # btn_count
            return 0
        return False  # has_next_js, on_post_js, etc.

    tab = MagicMock()
    tab.evaluate = AsyncMock(side_effect=fake_evaluate)
    tab.send = AsyncMock()
    return tab


class TestIsDone:
    # Import here so the test module doesn't depend on browser at collection time.
    @pytest.fixture(autouse=True)
    def _import(self):
        from tool.ig.db import is_done

        self._is_done = is_done

    def _make_db(self) -> sqlite3.Connection:
        return init_db(":memory:")

    def test_missing_post_returns_false(self):
        db = self._make_db()
        assert not self._is_done(db, "NOPE")

    def test_post_with_no_media_returns_false(self):
        db = self._make_db()
        db.execute("INSERT INTO post VALUES ('X','u','c','image',0)")
        db.commit()
        assert not self._is_done(db, "X")

    def test_post_with_missing_file_returns_false(self, tmp_path):
        db = self._make_db()
        db.execute("INSERT INTO post VALUES ('X','u','c','image',0)")
        db.execute("INSERT INTO media (shortid,idx,url) VALUES ('X',0,'http://x')")
        db.commit()
        assert not self._is_done(db, "X", media_dir=tmp_path)

    def test_post_with_zero_byte_file_returns_false(self, tmp_path):
        db = self._make_db()
        (tmp_path / "X" / "X.jpg").parent.mkdir(parents=True)
        (tmp_path / "X" / "X.jpg").write_bytes(b"")
        db.execute("INSERT INTO post VALUES ('X','u','c','image',0)")
        db.execute("INSERT INTO media (shortid,idx,url) VALUES ('X',0,'http://x')")
        db.commit()
        assert not self._is_done(db, "X", media_dir=tmp_path)

    def test_post_with_all_files_present_returns_true(self, tmp_path):
        db = self._make_db()
        f1 = tmp_path / "X" / "X_1.jpg"
        f2 = tmp_path / "X" / "X_2.jpg"
        f1.parent.mkdir(parents=True)
        f1.write_bytes(b"x")
        f2.write_bytes(b"x")
        db.execute("INSERT INTO post VALUES ('X','u','c','carousel',0)")
        db.execute("INSERT INTO media (shortid,idx,url) VALUES ('X',0,'u1')")
        db.execute("INSERT INTO media (shortid,idx,url) VALUES ('X',1,'u2')")
        db.commit()
        assert self._is_done(db, "X", media_dir=tmp_path)

    def test_single_image_path_is_inferred(self, tmp_path):
        db = self._make_db()
        f1 = tmp_path / "X" / "X.jpg"
        f1.parent.mkdir(parents=True)
        f1.write_bytes(b"x")
        db.execute("INSERT INTO post VALUES ('X','u','c','image',0)")
        db.execute("INSERT INTO media (shortid,idx,url) VALUES ('X',0,'u1')")
        db.commit()
        assert self._is_done(db, "X", media_dir=tmp_path)

    def test_partial_files_returns_false(self, tmp_path):
        db = self._make_db()
        f1 = tmp_path / "X" / "X_1.jpg"
        f1.parent.mkdir(parents=True)
        f1.write_bytes(b"x")
        db.execute("INSERT INTO post VALUES ('X','u','c','carousel',0)")
        db.execute("INSERT INTO media (shortid,idx,url) VALUES ('X',0,'u1')")
        db.execute("INSERT INTO media (shortid,idx,url) VALUES ('X',1,'u2')")
        db.commit()
        assert not self._is_done(db, "X", media_dir=tmp_path)

    def test_mixed_carousel_paths_are_inferred_from_urls(self, tmp_path):
        db = self._make_db()
        (tmp_path / "X" / "X_1.mp4").parent.mkdir(parents=True)
        (tmp_path / "X" / "X_1.mp4").write_bytes(b"x")
        (tmp_path / "X" / "X_2.jpg").write_bytes(b"x")
        db.execute("INSERT INTO post VALUES ('X','u','c','carousel',0)")
        db.execute("INSERT INTO media (shortid,idx,url) VALUES ('X',0,'https://cdn/video.mp4')")
        db.execute("INSERT INTO media (shortid,idx,url) VALUES ('X',1,'https://cdn/img.jpg')")
        db.commit()
        assert self._is_done(db, "X", media_dir=tmp_path)

    def test_profile_scope_includes_saved_flag(self, tmp_path):
        db = self._make_db()
        f1 = tmp_path / "X" / "X.jpg"
        f1.parent.mkdir(parents=True)
        f1.write_bytes(b"x")
        db.execute("INSERT INTO post VALUES ('X','u','c','image',0)")
        db.execute("INSERT INTO media (shortid,idx,url) VALUES ('X',0,'u1')")
        db.execute("INSERT INTO profile_post VALUES ('alice',0,'X',1,0)")
        db.commit()

        assert self._is_done(db, "X", "alice", media_dir=tmp_path)
        assert not self._is_done(db, "X", "alice", media_dir=tmp_path, saved=True)

        db.execute("INSERT INTO profile_post VALUES ('alice',1,'X',2,0)")
        db.commit()
        assert self._is_done(db, "X", "alice", media_dir=tmp_path, saved=True)


# ---------------------------------------------------------------------------
# _download
# ---------------------------------------------------------------------------


class TestDownload:
    def test_creates_file_with_content(self, tmp_path):
        dest = tmp_path / "images" / "ABC.jpg"
        body = b"fake image data"

        response_mock = MagicMock()
        response_mock.read.return_value = body
        response_mock.__enter__ = lambda s: s
        response_mock.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=response_mock):
            _download("http://cdn/img.jpg", dest, media_dir=tmp_path)

        assert dest.exists()
        assert dest.read_bytes() == body

    def test_creates_parent_directory(self, tmp_path):
        dest = tmp_path / "images" / "ABC.jpg"
        assert not dest.parent.exists()

        response_mock = MagicMock()
        response_mock.read.return_value = b"data"
        response_mock.__enter__ = lambda s: s
        response_mock.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=response_mock):
            _download("http://cdn/img.jpg", dest, media_dir=tmp_path)

        assert dest.parent.exists()

    def test_sends_instagram_referer(self, tmp_path):
        dest = tmp_path / "img.jpg"
        response_mock = MagicMock()
        response_mock.read.return_value = b""
        response_mock.__enter__ = lambda s: s
        response_mock.__exit__ = MagicMock(return_value=False)

        captured_req = []

        def fake_urlopen(req, **kwargs):
            captured_req.append(req)
            return response_mock

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            _download("http://cdn/img.jpg", dest, media_dir=tmp_path)

        assert captured_req[0].get_header("Referer") == "https://www.instagram.com/"

    def test_download_retries_transient_transport_errors(self, tmp_path, monkeypatch):
        dest = tmp_path / "img.jpg"
        response_mock = MagicMock()
        response_mock.read.return_value = b"new"
        response_mock.__enter__ = lambda s: s
        response_mock.__exit__ = MagicMock(return_value=False)
        calls = []

        def fake_urlopen(req, **kwargs):
            calls.append(req)
            if len(calls) == 1:
                raise urllib.error.URLError("temporary DNS failure")
            return response_mock

        monkeypatch.setattr("tool.ig.scraper.time.sleep", lambda _delay: None)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            _download("http://cdn/img.jpg", dest, media_dir=tmp_path)

        assert dest.read_bytes() == b"new"
        assert len(calls) == 2

    def test_failed_download_preserves_existing_file(self, tmp_path):
        dest = tmp_path / "img.jpg"
        dest.write_bytes(b"old")

        response_mock = MagicMock()
        response_mock.read.side_effect = OSError("connection reset")
        response_mock.__enter__ = lambda s: s
        response_mock.__exit__ = MagicMock(return_value=False)

        with (
            patch("urllib.request.urlopen", return_value=response_mock),
            pytest.raises(OSError, match="connection reset"),
        ):
            _download("http://cdn/img.jpg", dest, media_dir=tmp_path)

        assert dest.read_bytes() == b"old"
        assert list(tmp_path.glob(".*.tmp")) == []


@pytest.mark.asyncio
async def test_mux_av_uses_mp4_temp_output(tmp_path, monkeypatch):
    output_paths = []

    def fake_run(cmd, *, check, timeout):
        output = Path(cmd[-1])
        output_paths.append(output)
        output.write_bytes(b"muxed")

    monkeypatch.setattr("subprocess.run", fake_run)

    dest = tmp_path / "out.mp4"
    await mux_av(b"video", b"audio", dest, media_dir=tmp_path)

    assert dest.read_bytes() == b"muxed"
    assert len(output_paths) == 1
    assert output_paths[0].suffix == ".mp4"
    assert output_paths[0].name.startswith(".out.mp4.")
