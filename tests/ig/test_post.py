"""Tests for tool/ig/post.py."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from tool.ig.hq_video import parse_retry_after
from tool.ig.post import (
    RateLimitError,
    RateLimitTracker,
    _maybe_like,
    _raise_if_page_rate_limited,
    _wait_for_navigation_start,
)
from tool.ig.scraper import SegmentCapture
from tool.ig.video_save import captured_video_base_for_url, download_and_mux_av


def _video_url(tag: str, *, duration: int, bitrate: int) -> str:
    import base64
    import json

    efg = (
        base64.urlsafe_b64encode(json.dumps({"vencode_tag": tag, "duration_s": duration, "bitrate": bitrate}).encode())
        .decode()
        .rstrip("=")
    )
    return f"https://cdninstagram.com/o1/v/t2/f2/video-{duration}-{bitrate}.mp4?efg={efg}"


def test_rate_limit_tracker_raises_with_retry_after_header():
    tracker = RateLimitTracker()
    tracker.on_response_status(429, "https://www.instagram.com/p/ABC/", {"retry-after": "120"})

    with pytest.raises(RateLimitError) as exc_info:
        tracker.raise_if_limited("ABC")

    assert exc_info.value.retry_after == 120
    assert exc_info.value.backoff_seconds() == 120


def test_rate_limit_tracker_ignores_other_statuses():
    tracker = RateLimitTracker()
    tracker.on_response_status(500, "https://www.instagram.com/p/ABC/", {})

    tracker.raise_if_limited("ABC")


def test_rate_limit_error_uses_default_backoff_without_header():
    assert RateLimitError("ABC").backoff_seconds() == 15 * 60


def test_parse_retry_after_http_date(monkeypatch):
    monkeypatch.setattr("tool.ig.hq_video.time.time", lambda: 1_700_000_000.0)

    assert parse_retry_after("Tue, 14 Nov 2023 22:15:20 GMT") == 120


def test_captured_video_base_must_match_selected_url():
    capture = SegmentCapture()
    low = _video_url("ig-xpvds.clips.c2-C3.dash_baseline_3_v1", duration=5, bitrate=132362) + "&bytestart=0&byteend=99"
    high = _video_url("ig-xpvds.clips.c2-C3.dash_baseline_1_v1", duration=5, bitrate=937348)
    capture.add(low, 0, b"preview", 7)

    assert captured_video_base_for_url(capture, high) is None
    assert captured_video_base_for_url(capture, low) == _video_url(
        "ig-xpvds.clips.c2-C3.dash_baseline_3_v1", duration=5, bitrate=132362
    )


@pytest.mark.asyncio
async def test_download_and_mux_av_uses_selected_video_and_audio(monkeypatch, tmp_path):
    downloaded = []

    def fake_download(url):
        downloaded.append(url)
        return f"bytes:{url}".encode()

    mux = AsyncMock()
    monkeypatch.setattr("tool.ig.video_save.download_bytes", fake_download)
    monkeypatch.setattr("tool.ig.video_save.mux_av", mux)

    dest = tmp_path / "out.mp4"
    await download_and_mux_av("https://cdn/video.mp4", "https://cdn/audio.mp4", dest)

    assert sorted(downloaded) == ["https://cdn/audio.mp4", "https://cdn/video.mp4"]
    mux.assert_awaited_once_with(b"bytes:https://cdn/video.mp4", b"bytes:https://cdn/audio.mp4", dest)


@pytest.mark.asyncio
async def test_page_rate_limit_detection_uses_performance_status():
    tab = MagicMock()
    tab.evaluate = AsyncMock(
        return_value={
            "href": "https://www.instagram.com/p/ABC/",
            "title": "Instagram",
            "text": "",
            "statuses": [200, 429],
        }
    )

    with pytest.raises(RateLimitError):
        await _raise_if_page_rate_limited(tab, "ABC")


@pytest.mark.asyncio
async def test_page_rate_limit_detection_uses_instagram_wait_text():
    tab = MagicMock()
    tab.evaluate = AsyncMock(
        return_value={
            "href": "https://www.instagram.com/p/ABC/",
            "title": "Instagram",
            "text": "Please wait a few minutes before you try again.",
            "statuses": [],
        }
    )

    with pytest.raises(RateLimitError):
        await _raise_if_page_rate_limited(tab, "ABC")


@pytest.mark.asyncio
async def test_page_rate_limit_detection_ignores_plain_error_text():
    tab = MagicMock()
    tab.evaluate = AsyncMock(
        return_value={
            "href": "https://www.instagram.com/p/ABC/",
            "title": "Instagram",
            "text": "Something went wrong",
            "statuses": [200],
            "hasArticle": True,
        }
    )

    await _raise_if_page_rate_limited(tab, "ABC")


@pytest.mark.asyncio
async def test_wait_for_navigation_start_waits_past_about_blank(monkeypatch):
    sleeps = []
    tab = MagicMock()
    tab.evaluate = AsyncMock(side_effect=["about:blank", "https://www.instagram.com/p/ABC/"])
    monkeypatch.setattr("tool.ig.post.asyncio.sleep", AsyncMock(side_effect=lambda delay: sleeps.append(delay)))

    await _wait_for_navigation_start(tab, "https://www.instagram.com/p/ABC/")

    assert sleeps == [0.1]


@pytest.mark.asyncio
class TestMaybeLike:
    @pytest.fixture(autouse=True)
    def _fast(self, monkeypatch):
        monkeypatch.setattr("tool.ig.post.asyncio.sleep", AsyncMock())

    async def test_no_click_when_random_above_threshold(self, monkeypatch):
        monkeypatch.setattr("tool.ig.post.random.random", lambda: 0.10)
        tab = MagicMock()
        tab.evaluate = AsyncMock()
        await _maybe_like(tab)
        tab.evaluate.assert_not_called()

    async def test_no_click_when_random_exactly_at_threshold(self, monkeypatch):
        monkeypatch.setattr("tool.ig.post.random.random", lambda: 0.05)
        tab = MagicMock()
        tab.evaluate = AsyncMock()
        await _maybe_like(tab)
        tab.evaluate.assert_not_called()

    async def test_clicks_and_sleeps_when_like_button_present(self, monkeypatch):
        monkeypatch.setattr("tool.ig.post.random.random", lambda: 0.01)
        monkeypatch.setattr("tool.ig.post.random.uniform", lambda a, b: a)
        sleep_mock = AsyncMock()
        monkeypatch.setattr("tool.ig.post.asyncio.sleep", sleep_mock)

        tab = MagicMock()
        tab.evaluate = AsyncMock(return_value=True)
        await _maybe_like(tab)

        tab.evaluate.assert_awaited_once()
        sleep_mock.assert_awaited_once()

    async def test_like_selector_handles_role_button(self, monkeypatch):
        monkeypatch.setattr("tool.ig.post.random.random", lambda: 0.01)
        tab = MagicMock()
        tab.evaluate = AsyncMock(return_value=True)

        await _maybe_like(tab)

        js = tab.evaluate.await_args.args[0]
        assert "closest('button, [role=\"button\"]')" in js

    async def test_no_sleep_when_like_button_absent(self, monkeypatch):
        monkeypatch.setattr("tool.ig.post.random.random", lambda: 0.01)
        sleep_mock = AsyncMock()
        monkeypatch.setattr("tool.ig.post.asyncio.sleep", sleep_mock)

        tab = MagicMock()
        tab.evaluate = AsyncMock(return_value=False)
        await _maybe_like(tab)

        tab.evaluate.assert_awaited_once()
        sleep_mock.assert_not_called()

    async def test_like_errors_are_ignored(self, monkeypatch):
        monkeypatch.setattr("tool.ig.post.random.random", lambda: 0.01)
        tab = MagicMock()
        tab.evaluate = AsyncMock(side_effect=RuntimeError("gone"))
        await _maybe_like(tab)
        tab.evaluate.assert_awaited_once()
