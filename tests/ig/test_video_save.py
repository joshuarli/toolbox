"""Tests for tool/ig/video_save.py — save_video_post_media branches."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from tool.ig.db import init_db
from tool.ig.hq_video import HqVideoFetchError, HqVideoTracks
from tool.ig.scraper import SegmentCapture
from tool.ig.types import ScrapedPost
from tool.ig.video_save import (
    _best_video_url_from_captures,
    save_video_post_media,
)


class _FastClock:
    """Returns 0.0 for the first *budget* calls, then monotonically huge values
    so every busy-wait deadline check fails after the budget is exhausted."""

    def __init__(self, budget: int = 4):
        self._n = 0
        self._budget = budget

    def __call__(self):
        self._n += 1
        if self._n <= self._budget:
            return 0.0
        return self._n * 1e9


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr("tool.ig.video_save.asyncio.sleep", AsyncMock())
    mock_loop = MagicMock()
    mock_loop.time = _FastClock()
    monkeypatch.setattr("tool.ig.video_save.asyncio.get_running_loop", lambda: mock_loop)


def _video_url(tag: str, *, duration: int, bitrate: int) -> str:
    import base64
    import json

    efg = (
        base64.urlsafe_b64encode(json.dumps({"vencode_tag": tag, "duration_s": duration, "bitrate": bitrate}).encode())
        .decode()
        .rstrip("=")
    )
    return f"https://cdninstagram.com/o1/v/t2/f2/video-{duration}-{bitrate}.mp4?efg={efg}"


def _video_post(shortid: str = "VID1") -> ScrapedPost:
    return ScrapedPost(
        shortid=shortid,
        post_type="video",
        media_urls=["https://cdn/video.mp4"],
        username="owner",
        caption="cap",
    )


def _seg_capture_with_video_and_audio(
    video_url: str = "https://cdn/video.mp4",
    audio_url: str = "https://cdn/audio.mp4",
    video_bytes: bytes = b"v" * 100,
    audio_bytes: bytes = b"a" * 50,
) -> tuple[SegmentCapture, str]:
    """Returns (capture, video_base_url) so callers can match the URL."""
    seg = SegmentCapture()
    import base64
    import json

    video_tag = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"vencode_tag": "ig-xpvds.clips.c2-C3.dash_baseline_1_v1", "duration_s": 5, "bitrate": 937}
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    video_base = video_url + f"?efg={video_tag}"
    video_url_tagged = video_base + "&bytestart=0&byteend=99"
    audio_tag = (
        base64.urlsafe_b64encode(
            json.dumps({"vencode_tag": "dash_ln_heaac_vbr3_audio", "duration_s": 5, "bitrate": 64}).encode()
        )
        .decode()
        .rstrip("=")
    )
    audio_url_tagged = audio_url + f"?efg={audio_tag}"
    seg.add(video_url_tagged, 0, video_bytes, len(video_bytes))
    seg.add(audio_url_tagged, 0, audio_bytes, len(audio_bytes))
    return seg, video_base


def _seg_capture_with_video_only(
    video_url: str = "https://cdn/video.mp4",
    video_bytes: bytes = b"v" * 100,
) -> tuple[SegmentCapture, str]:
    """Returns (capture, video_base_url) so callers can match the URL."""
    import base64
    import json

    seg = SegmentCapture()
    video_tag = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"vencode_tag": "ig-xpvds.clips.c2-C3.dash_baseline_1_v1", "duration_s": 5, "bitrate": 937}
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    video_base = video_url + f"?efg={video_tag}"
    seg.add(video_base + "&bytestart=0&byteend=99", 0, video_bytes, len(video_bytes))
    return seg, video_base


async def _noop(*_args, **_kwargs):
    pass


@pytest.mark.asyncio
class TestSaveVideoPostMedia:
    async def test_hq_video_mux_path(self, tmp_path, monkeypatch):
        """HQ tracks selected and muxed with audio."""
        db = init_db(":memory:")
        post = _video_post()
        seg = SegmentCapture()
        tab = MagicMock()
        tab.command = AsyncMock(
            return_value={
                "cookies": [
                    {"domain": ".instagram.com", "name": "sessionid", "value": {"type": "string", "value": "s"}},
                    {"domain": ".instagram.com", "name": "csrftoken", "value": {"type": "string", "value": "c"}},
                ]
            }
        )
        media_dir = tmp_path / "media"

        hq_tracks = HqVideoTracks(
            video_url="https://cdn/hq_video.mp4",
            audio_url="https://cdn/hq_audio.mp4",
            width=1080,
            height=1920,
            video_bitrate=500000,
            audio_bitrate=64000,
        )

        async def fake_resolve_hq(tab, shortid):
            return hq_tracks

        mux_calls = []

        async def fake_download_and_mux(video_url, audio_url, dest, **_kwargs):
            mux_calls.append((video_url, audio_url, dest))

        save_called = []

        async def fake_save(*args, **kwargs):
            save_called.append(kwargs)

        monkeypatch.setattr("tool.ig.video_save.resolve_hq_video_tracks", fake_resolve_hq)
        monkeypatch.setattr("tool.ig.video_save.resolve_audio_url", AsyncMock(return_value=None))
        monkeypatch.setattr("tool.ig.video_save.download_and_mux_av", fake_download_and_mux)
        monkeypatch.setattr("tool.ig.video_save.save_post", fake_save)

        await save_video_post_media(
            tab,
            db,
            post,
            username="owner",
            username_hint="alice",
            seg_capture=seg,
            bases_before=set(),
            hq_video=True,
            media_dir=media_dir,
            hq_rate_limit_error=lambda exc: exc,
        )

        assert len(mux_calls) == 1
        assert mux_calls[0][0] == "https://cdn/hq_video.mp4"
        assert mux_calls[0][1] == "https://cdn/hq_audio.mp4"
        assert len(save_called) == 1
        assert save_called[0]["skip_existing_files"] is True
        assert save_called[0]["fresh_files"] == {media_dir / "by-profile" / "alice" / "VID1.mp4"}
        assert post.media_urls == ["https://cdn/hq_video.mp4"]

    async def test_hq_video_429_is_translated_to_rate_limit(self, tmp_path, monkeypatch):
        """HQ video 429 is caught and re-raised via callback."""
        db = init_db(":memory:")
        post = _video_post()
        seg = SegmentCapture()
        tab = MagicMock()
        tab.command = AsyncMock(
            return_value={
                "cookies": [
                    {"domain": ".instagram.com", "name": "sessionid", "value": {"type": "string", "value": "s"}},
                    {"domain": ".instagram.com", "name": "csrftoken", "value": {"type": "string", "value": "c"}},
                ]
            }
        )

        async def fake_resolve_hq(tab, shortid):
            raise HqVideoFetchError("rate limited", status=429, retry_after=120, url="https://x")

        monkeypatch.setattr("tool.ig.video_save.resolve_hq_video_tracks", fake_resolve_hq)

        class TestRateLimitError(Exception):
            pass

        with pytest.raises(TestRateLimitError):
            await save_video_post_media(
                tab,
                db,
                post,
                username="owner",
                username_hint="alice",
                seg_capture=seg,
                bases_before=set(),
                hq_video=True,
                media_dir=tmp_path / "media",
                hq_rate_limit_error=lambda exc: TestRateLimitError(str(exc)),
            )

    async def test_hq_non_429_error_falls_back_to_standard_quality(self, tmp_path, monkeypatch):
        """Non-429 HQ metadata errors warn and fall back to the standard chain instead of failing."""
        db = init_db(":memory:")
        post = _video_post()
        seg = SegmentCapture()
        tab = MagicMock()

        async def fake_resolve_hq(tab, shortid):
            raise HqVideoFetchError("http 403", status=403, url="https://x")

        save_called = []

        async def fake_save(*args, **kwargs):
            save_called.append((args, kwargs))

        monkeypatch.setattr("tool.ig.video_save.resolve_hq_video_tracks", fake_resolve_hq)
        monkeypatch.setattr("tool.ig.video_save.save_post", fake_save)
        monkeypatch.setattr("tool.ig.video_save.resolve_audio_url", AsyncMock(return_value=None))

        await save_video_post_media(
            tab,
            db,
            post,
            username="owner",
            username_hint="alice",
            seg_capture=seg,
            bases_before=set(),
            media_dir=tmp_path / "media",
            hq_rate_limit_error=lambda exc: exc,
        )

        assert len(save_called) == 1
        assert save_called[0][0][5] == ["https://cdn/video.mp4"]

    async def test_hq_protocol_error_falls_back_to_standard_quality(self, tmp_path, monkeypatch):
        """Cookie-read ProtocolError during HQ lookup warns and falls back to the standard chain."""
        db = init_db(":memory:")
        post = _video_post()
        seg = SegmentCapture()
        tab = MagicMock()

        from tool.ig.browser import ProtocolError

        async def fake_resolve_hq(tab, shortid):
            raise ProtocolError("storage.getCookies wasn't found")

        save_called = []

        async def fake_save(*args, **kwargs):
            save_called.append((args, kwargs))

        monkeypatch.setattr("tool.ig.video_save.resolve_hq_video_tracks", fake_resolve_hq)
        monkeypatch.setattr("tool.ig.video_save.save_post", fake_save)
        monkeypatch.setattr("tool.ig.video_save.resolve_audio_url", AsyncMock(return_value=None))

        await save_video_post_media(
            tab,
            db,
            post,
            username="owner",
            username_hint="alice",
            seg_capture=seg,
            bases_before=set(),
            media_dir=tmp_path / "media",
            hq_rate_limit_error=lambda exc: exc,
        )

        assert len(save_called) == 1
        assert save_called[0][0][5] == ["https://cdn/video.mp4"]

    async def test_hq_no_dash_manifest_warns_and_uses_standard_quality(self, tmp_path, monkeypatch):
        """No DASH manifest is reported as a warning and the standard chain still runs."""
        db = init_db(":memory:")
        post = _video_post()
        seg = SegmentCapture()
        tab = MagicMock()

        async def fake_resolve_hq(tab, shortid):
            return None

        save_called = []

        async def fake_save(*args, **kwargs):
            save_called.append((args, kwargs))

        monkeypatch.setattr("tool.ig.video_save.resolve_hq_video_tracks", fake_resolve_hq)
        monkeypatch.setattr("tool.ig.video_save.save_post", fake_save)
        monkeypatch.setattr("tool.ig.video_save.resolve_audio_url", AsyncMock(return_value=None))

        await save_video_post_media(
            tab,
            db,
            post,
            username="owner",
            username_hint="alice",
            seg_capture=seg,
            bases_before=set(),
            media_dir=tmp_path / "media",
            hq_rate_limit_error=lambda exc: exc,
        )

        assert len(save_called) == 1
        assert save_called[0][0][5] == ["https://cdn/video.mp4"]

    async def test_hq_mux_failure_falls_back_to_original_video_url(self, tmp_path, monkeypatch):
        db = init_db(":memory:")
        post = _video_post()
        seg = SegmentCapture()
        tab = MagicMock()
        hq_tracks = HqVideoTracks(
            video_url="https://cdn/hq_video_only.mp4",
            audio_url="https://cdn/hq_audio.mp4",
            width=1080,
            height=1920,
            video_bitrate=500000,
            audio_bitrate=64000,
        )

        async def fake_resolve_hq(tab, shortid):
            return hq_tracks

        async def failing_download_and_mux(video_url, audio_url, dest, **_kwargs):
            raise RuntimeError("ffmpeg failed")

        save_called = []

        async def fake_save(*args, **kwargs):
            save_called.append((args, kwargs))

        monkeypatch.setattr("tool.ig.video_save.resolve_hq_video_tracks", fake_resolve_hq)
        monkeypatch.setattr("tool.ig.video_save.download_and_mux_av", failing_download_and_mux)
        monkeypatch.setattr("tool.ig.video_save.save_post", fake_save)
        monkeypatch.setattr("tool.ig.video_save.resolve_audio_url", AsyncMock(return_value=None))

        await save_video_post_media(
            tab,
            db,
            post,
            username="owner",
            username_hint="alice",
            seg_capture=seg,
            bases_before=set(),
            hq_video=True,
            media_dir=tmp_path / "media",
            hq_rate_limit_error=lambda exc: exc,
        )

        assert save_called[0][0][5] == ["https://cdn/video.mp4"]
        assert post.media_urls == ["https://cdn/video.mp4"]

    async def test_captured_segments_mux_path(self, tmp_path, monkeypatch):
        """Complete captured video + audio segments are assembled and muxed."""
        db = init_db(":memory:")
        post = _video_post()
        seg, video_base = _seg_capture_with_video_and_audio()
        post.media_urls = [video_base]
        tab = MagicMock()

        mux_called = []

        async def fake_mux(video_bytes, audio_bytes, dest, **_kwargs):
            mux_called.append((video_bytes, audio_bytes, dest))

        save_called = []

        async def fake_save(*args, **kwargs):
            save_called.append((args, kwargs))

        monkeypatch.setattr("tool.ig.video_save.mux_av", fake_mux)
        monkeypatch.setattr("tool.ig.video_save.save_post", fake_save)
        monkeypatch.setattr("tool.ig.video_save.resolve_audio_url", AsyncMock(return_value="https://cdn/audio.mp4"))

        await save_video_post_media(
            tab,
            db,
            post,
            username="owner",
            username_hint="alice",
            seg_capture=seg,
            bases_before=set(),
            hq_video=False,
            media_dir=tmp_path / "media",
            hq_rate_limit_error=lambda exc: exc,
        )

        assert len(mux_called) == 1
        assert mux_called[0][0] == b"v" * 100
        assert mux_called[0][1] == b"a" * 50
        assert save_called[0][0][5] == [video_base]
        assert save_called[0][1]["skip_existing_files"] is True

    async def test_captured_video_only_path(self, tmp_path, monkeypatch):
        """Captured video segment is complete but no audio — saved directly."""
        db = init_db(":memory:")
        post = _video_post()
        seg, video_base = _seg_capture_with_video_only()
        post.media_urls = [video_base]
        tab = MagicMock()

        save_called = []

        async def fake_save(*args, **kwargs):
            save_called.append((args, kwargs))

        monkeypatch.setattr("tool.ig.video_save.save_post", fake_save)
        monkeypatch.setattr("tool.ig.video_save.resolve_audio_url", AsyncMock(return_value=None))

        await save_video_post_media(
            tab,
            db,
            post,
            username="owner",
            username_hint="alice",
            seg_capture=seg,
            bases_before=set(),
            hq_video=False,
            media_dir=tmp_path / "media",
            hq_rate_limit_error=lambda exc: exc,
        )

        assert len(save_called) == 1
        assert save_called[0][0][5] == [video_base]
        assert save_called[0][1]["skip_existing_files"] is False
        assert video_base in save_called[0][1]["captured_bodies"]

    async def test_cdn_download_with_audio_mux(self, tmp_path, monkeypatch):
        """Segments incomplete but audio URL resolved — downloads both from CDN."""
        db = init_db(":memory:")
        post = _video_post()
        seg = SegmentCapture()
        tab = MagicMock()

        mux_av_calls = []

        async def fake_download_and_mux(video_url, audio_url, dest, **_kwargs):
            mux_av_calls.append((video_url, audio_url, dest))

        save_called = []

        async def fake_save(*args, **kwargs):
            save_called.append((args, kwargs))

        monkeypatch.setattr("tool.ig.video_save.download_and_mux_av", fake_download_and_mux)
        monkeypatch.setattr("tool.ig.video_save.save_post", fake_save)
        monkeypatch.setattr("tool.ig.video_save.resolve_audio_url", AsyncMock(return_value="https://cdn/audio.mp4"))

        await save_video_post_media(
            tab,
            db,
            post,
            username="owner",
            username_hint="alice",
            seg_capture=seg,
            bases_before=set(),
            hq_video=False,
            media_dir=tmp_path / "media",
            hq_rate_limit_error=lambda exc: exc,
        )

        assert len(mux_av_calls) == 1
        assert mux_av_calls[0][0] == "https://cdn/video.mp4"
        assert mux_av_calls[0][1] == "https://cdn/audio.mp4"

    async def test_plain_cdn_fallback_when_no_audio(self, tmp_path, monkeypatch):
        """No segments, no audio URL — plain CDN download through save_post."""
        db = init_db(":memory:")
        post = _video_post()
        seg = SegmentCapture()
        tab = MagicMock()

        save_called = []

        async def fake_save(*args, **kwargs):
            save_called.append((args, kwargs))

        monkeypatch.setattr("tool.ig.video_save.save_post", fake_save)
        monkeypatch.setattr("tool.ig.video_save.resolve_audio_url", AsyncMock(return_value=None))

        await save_video_post_media(
            tab,
            db,
            post,
            username="owner",
            username_hint="alice",
            seg_capture=seg,
            bases_before=set(),
            hq_video=False,
            media_dir=tmp_path / "media",
            hq_rate_limit_error=lambda exc: exc,
        )

        assert len(save_called) == 1
        assert save_called[0][0][5] == ["https://cdn/video.mp4"]
        assert save_called[0][1]["skip_existing_files"] is False
        assert "captured_bodies" not in save_called[0][1] or save_called[0][1]["captured_bodies"] is None

    async def test_captured_mux_failure_falls_back_to_cdn(self, tmp_path, monkeypatch):
        """When segment mux_av fails, falls back to CDN download."""
        db = init_db(":memory:")
        post = _video_post()
        seg, video_base = _seg_capture_with_video_and_audio()
        post.media_urls = [video_base]
        tab = MagicMock()

        async def failing_mux(video_bytes, audio_bytes, dest, **_kwargs):
            raise RuntimeError("ffmpeg not found")

        save_called = []

        async def fake_save(*args, **kwargs):
            save_called.append(kwargs)

        monkeypatch.setattr("tool.ig.video_save.mux_av", failing_mux)
        monkeypatch.setattr("tool.ig.video_save.save_post", fake_save)
        monkeypatch.setattr("tool.ig.video_save.resolve_audio_url", AsyncMock(return_value="https://cdn/audio.mp4"))

        await save_video_post_media(
            tab,
            db,
            post,
            username="owner",
            username_hint="alice",
            seg_capture=seg,
            bases_before=set(),
            hq_video=False,
            media_dir=tmp_path / "media",
            hq_rate_limit_error=lambda exc: exc,
        )

        assert len(save_called) == 1
        assert save_called[0]["skip_existing_files"] is False

    async def test_cdn_mux_failure_falls_back_to_plain_cdn(self, tmp_path, monkeypatch):
        """When CDN download+mux fails, falls back to plain CDN download."""
        db = init_db(":memory:")
        post = _video_post()
        seg = SegmentCapture()
        tab = MagicMock()

        async def failing_download_and_mux(video_url, audio_url, dest, **_kwargs):
            raise RuntimeError("connection reset")

        save_called = []

        async def fake_save(*args, **kwargs):
            save_called.append(kwargs)

        monkeypatch.setattr("tool.ig.video_save.download_and_mux_av", failing_download_and_mux)
        monkeypatch.setattr("tool.ig.video_save.save_post", fake_save)
        monkeypatch.setattr("tool.ig.video_save.resolve_audio_url", AsyncMock(return_value="https://cdn/audio.mp4"))

        await save_video_post_media(
            tab,
            db,
            post,
            username="owner",
            username_hint="alice",
            seg_capture=seg,
            bases_before=set(),
            hq_video=False,
            media_dir=tmp_path / "media",
            hq_rate_limit_error=lambda exc: exc,
        )

        assert len(save_called) == 1
        assert save_called[0]["skip_existing_files"] is False

    async def test_best_video_url_from_captures_prefers_complete_base(self, tmp_path, monkeypatch):
        """When a captured base is complete and audio is available, it's preferred over selected_url in CDN+mux path."""
        db = init_db(":memory:")
        post = _video_post()
        post.media_urls = ["https://cdn/low_quality.mp4"]
        seg, high_quality_base = _seg_capture_with_video_only(
            video_url="https://cdn/high_quality.mp4",
            video_bytes=b"x" * 200,
        )
        tab = MagicMock()

        mux_calls = []

        async def fake_download_and_mux(video_url, audio_url, dest, **_kwargs):
            mux_calls.append(video_url)

        monkeypatch.setattr("tool.ig.video_save.download_and_mux_av", fake_download_and_mux)
        monkeypatch.setattr("tool.ig.video_save.save_post", AsyncMock())
        monkeypatch.setattr("tool.ig.video_save.resolve_audio_url", AsyncMock(return_value="https://cdn/audio.mp4"))

        await save_video_post_media(
            tab,
            db,
            post,
            username="owner",
            username_hint="alice",
            seg_capture=seg,
            bases_before=set(),
            hq_video=False,
            media_dir=tmp_path / "media",
            hq_rate_limit_error=lambda exc: exc,
        )

        assert len(mux_calls) == 1
        assert mux_calls[0] == high_quality_base


class TestBestVideoUrlFromCaptures:
    def test_returns_selected_url_when_no_captures(self):
        seg = SegmentCapture()
        assert _best_video_url_from_captures(seg, "https://cdn/selected.mp4") == "https://cdn/selected.mp4"

    def test_returns_complete_captured_base(self):
        seg = SegmentCapture()
        captured_base = _video_url("ig-xpvds.clips.c2-C3.dash_baseline_1_v1", duration=5, bitrate=937)
        seg.add(captured_base + "&bytestart=0&byteend=99", 0, b"x" * 100, 100)
        assert _best_video_url_from_captures(seg, captured_base) == captured_base

    def test_returns_highest_resolution_complete_capture(self):
        seg = SegmentCapture()
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
        seg.add(low + "&bytestart=0&byteend=99", 0, b"l" * 100, 100)
        seg.add(high + "&bytestart=0&byteend=199", 0, b"h" * 200, 200)

        assert _best_video_url_from_captures(seg, low) == high

    def test_returns_selected_when_captured_is_incomplete(self):
        seg = SegmentCapture()
        captured_base = _video_url("ig-xpvds.clips.c2-C3.dash_baseline_1_v1", duration=5, bitrate=937)
        seg.add(captured_base + "&bytestart=0&byteend=99", 0, b"x" * 50, 100)
        assert _best_video_url_from_captures(seg, "https://cdn/selected.mp4") == "https://cdn/selected.mp4"


class TestClassifyScrapedPost:
    def test_plain_image_stays_image(self):
        from tool.ig.scraper import _classify_scraped_post

        post_type, has_video = _classify_scraped_post(
            "image",
            [],
            [],
            False,
            False,
        )
        assert post_type == "image"
        assert has_video is False

    def test_declared_video_stays_video(self):
        from tool.ig.scraper import _classify_scraped_post

        post_type, has_video = _classify_scraped_post(
            "video",
            [],
            ["https://cdn/v.mp4"],
            False,
            False,
        )
        assert post_type == "video"
        assert has_video is False

    def test_image_with_video_urls_becomes_video(self):
        from tool.ig.scraper import _classify_scraped_post

        post_type, has_video = _classify_scraped_post(
            "image",
            [],
            ["https://cdn/v.mp4"],
            False,
            False,
        )
        assert post_type == "video"
        assert has_video is False

    def test_image_with_video_element_becomes_video(self):
        from tool.ig.scraper import _classify_scraped_post

        post_type, has_video = _classify_scraped_post(
            "image",
            [],
            [],
            True,
            False,
        )
        assert post_type == "video"
        assert has_video is False

    def test_multiple_script_urls_becomes_carousel(self):
        from tool.ig.scraper import _classify_scraped_post

        post_type, has_video = _classify_scraped_post(
            "image",
            ["https://cdn/1.jpg", "https://cdn/2.jpg"],
            [],
            False,
            False,
        )
        assert post_type == "carousel"
        assert has_video is False

    def test_carousel_with_video_urls_returns_has_video(self):
        from tool.ig.scraper import _classify_scraped_post

        post_type, has_video = _classify_scraped_post(
            "image",
            ["https://cdn/1.jpg"],
            ["https://cdn/v.mp4"],
            False,
            True,
        )
        assert post_type == "carousel"
        assert has_video is True

    def test_next_button_makes_carousel(self):
        from tool.ig.scraper import _classify_scraped_post

        post_type, has_video = _classify_scraped_post(
            "image",
            ["https://cdn/1.jpg"],
            [],
            False,
            True,
        )
        assert post_type == "carousel"
        assert has_video is False

    @pytest.mark.asyncio
    async def test_carousel_video_resolution_keeps_multiple_video_urls(self):
        from tool.ig.scraper import _resolve_video_urls

        tab = MagicMock()
        tab.evaluate = AsyncMock(return_value=[])

        urls = await _resolve_video_urls(
            tab,
            ["https://cdn/one.mp4", "https://cdn/two.mp4"],
            select_best=False,
        )

        assert urls == ["https://cdn/one.mp4", "https://cdn/two.mp4"]

    def test_declared_carousel_without_video(self):
        from tool.ig.scraper import _classify_scraped_post

        post_type, has_video = _classify_scraped_post(
            "carousel",
            ["https://cdn/1.jpg", "https://cdn/2.jpg"],
            [],
            False,
            False,
        )
        assert post_type == "carousel"
        assert has_video is False
