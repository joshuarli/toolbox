"""Unit tests for tool.ig.profile_timeline — web v1 profile feed scraping.

All tests are fully offline: no browser, no network.
"""

import urllib.parse
from unittest.mock import AsyncMock, MagicMock

import pytest

from tool.ig.profile_timeline import (
    ProfileTimelineSession,
    ProfileTimelineUnavailableError,
    _feed_page_url,
    fetch_profile_posts_page,
    open_profile_timeline,
    post_from_timeline_node,
)
from tool.ig.web_api import WebApiError


def test_feed_page_url_omits_max_id_on_first_page():
    url = _feed_page_url("123")

    parsed = urllib.parse.urlparse(url)
    assert parsed.path == "/api/v1/feed/user/123/"
    assert urllib.parse.parse_qs(parsed.query) == {"count": ["12"]}


def test_feed_page_url_adds_max_id_on_later_pages():
    url = _feed_page_url("123", after="CURSOR")

    parsed = urllib.parse.urlparse(url)
    assert urllib.parse.parse_qs(parsed.query) == {"count": ["12"], "max_id": ["CURSOR"]}


def _session() -> ProfileTimelineSession:
    return ProfileTimelineSession("alice", None, {"sessionid": "s", "csrftoken": "c"})


@pytest.mark.asyncio
async def test_open_profile_timeline_does_not_resolve_id_through_profile_info(monkeypatch):
    tab = MagicMock()
    tab.get_cookies = AsyncMock(
        return_value={
            "cookies": [
                {"domain": ".instagram.com", "name": "sessionid", "value": {"type": "string", "value": "s"}},
                {"domain": ".instagram.com", "name": "csrftoken", "value": {"type": "string", "value": "c"}},
            ]
        }
    )

    def fail(*_args, **_kwargs):
        raise AssertionError("profile info should not be requested for timeline scans")

    monkeypatch.setattr("tool.ig.web_api.request_json", fail)

    session = await open_profile_timeline(tab, "alice")

    assert session.username == "alice"
    assert session.user_id is None


@pytest.mark.asyncio
async def test_fetch_profile_posts_page_uses_username_feed_when_id_is_unset(monkeypatch):
    captured: list[str] = []

    def fake_request_json(url, *_args, **_kwargs):
        captured.append(url)
        return {"items": []}

    monkeypatch.setattr("tool.ig.profile_timeline.request_json", fake_request_json)

    session = ProfileTimelineSession("alice smith", None, {"sessionid": "s", "csrftoken": "c"})
    await fetch_profile_posts_page(session, after="CURSOR")

    assert captured == ["https://www.instagram.com/api/v1/feed/user/alice%20smith/username/?count=12&max_id=CURSOR"]


@pytest.mark.asyncio
async def test_fetch_profile_posts_page_parses_items(monkeypatch):
    def fake_request_json(url, cookies, referer, label, *, error_cls=WebApiError):
        return {
            "items": [
                {
                    "code": "ABC123",
                    "media_type": 1,
                    "user": {"username": "owner"},
                    "image_versions2": {
                        "candidates": [
                            {"url": "https://cdn/small.jpg", "width": 320, "height": 320},
                            {"url": "https://cdn/large.jpg", "width": 1440, "height": 1800},
                        ]
                    },
                }
            ],
            "more_available": True,
            "next_max_id": "NEXT",
        }

    monkeypatch.setattr("tool.ig.profile_timeline.request_json", fake_request_json)

    page = await fetch_profile_posts_page(_session())

    assert page.posts[0].shortid == "ABC123"
    assert page.posts[0].post_type == "image"
    assert page.posts[0].username == "owner"
    assert page.posts[0].media_urls == ["https://cdn/large.jpg"]
    assert page.end_cursor == "NEXT"
    assert page.has_next_page is True


@pytest.mark.asyncio
async def test_fetch_profile_posts_page_uses_username_hint_for_missing_owner(monkeypatch):
    def fake_request_json(url, cookies, referer, label, *, error_cls=WebApiError):
        return {
            "items": [
                {
                    "code": "X1",
                    "media_type": 1,
                    "image_versions2": {"candidates": [{"url": "https://cdn/a.jpg", "width": 1, "height": 1}]},
                }
            ]
        }

    monkeypatch.setattr("tool.ig.profile_timeline.request_json", fake_request_json)

    page = await fetch_profile_posts_page(_session())

    assert page.posts[0].username == "alice"
    assert page.end_cursor is None
    assert page.has_next_page is False


@pytest.mark.asyncio
async def test_fetch_profile_posts_page_raises_when_items_missing(monkeypatch):
    monkeypatch.setattr(
        "tool.ig.profile_timeline.request_json",
        lambda *a, **k: {"status": "ok"},
    )

    with pytest.raises(ProfileTimelineUnavailableError, match="did not include items"):
        await fetch_profile_posts_page(_session())


@pytest.mark.asyncio
async def test_fetch_profile_posts_page_translates_web_api_errors(monkeypatch):
    def fail(*a, **k):
        raise WebApiError("profile timeline HTTP 429", status=429, retry_after=60, url="https://x")

    monkeypatch.setattr("tool.ig.profile_timeline.request_json", fail)

    with pytest.raises(ProfileTimelineUnavailableError, match="HTTP 429"):
        await fetch_profile_posts_page(_session())


@pytest.mark.asyncio
async def test_fetch_profile_posts_page_retries_transient_400(monkeypatch):
    calls = 0

    def flaky_request(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise WebApiError("profile timeline HTTP 400", status=400)
        return {"items": []}

    async def no_sleep(_delay):
        pass

    monkeypatch.setattr("tool.ig.profile_timeline.request_json", flaky_request)
    monkeypatch.setattr("tool.ig.profile_timeline.asyncio.sleep", no_sleep)

    page = await fetch_profile_posts_page(_session())

    assert page.posts == []
    assert calls == 3


@pytest.mark.asyncio
async def test_open_profile_timeline_snapshots_cookies():
    tab = MagicMock()
    tab.get_cookies = AsyncMock(
        return_value={
            "cookies": [
                {"domain": ".instagram.com", "name": "sessionid", "value": {"type": "string", "value": "s"}},
                {"domain": ".instagram.com", "name": "csrftoken", "value": {"type": "string", "value": "c"}},
            ]
        }
    )

    session = await open_profile_timeline(tab, "alice")

    assert session.username == "alice"
    assert session.user_id is None
    assert session.cookies["sessionid"] == "s"


def test_timeline_node_to_mixed_carousel_post():
    post = post_from_timeline_node(
        {
            "code": "ABC123",
            "media_type": 8,
            "caption": {"text": "caption"},
            "user": {"username": "owner"},
            "carousel_media": [
                {
                    "media_type": 1,
                    "image_versions2": {
                        "candidates": [
                            {"url": "https://cdn/small.jpg", "width": 320, "height": 320},
                            {"url": "https://cdn/large.jpg", "width": 1440, "height": 1800},
                        ]
                    },
                },
                {
                    "media_type": 2,
                    "video_versions": [
                        {"url": "https://cdn/low.mp4", "width": 480, "height": 480},
                        {"url": "https://cdn/high.mp4", "width": 1080, "height": 1920},
                    ],
                },
            ],
        }
    )

    assert post is not None
    assert post.shortid == "ABC123"
    assert post.post_type == "carousel"
    assert post.username == "owner"
    assert post.caption == "caption"
    assert post.media_urls == ["https://cdn/large.jpg", "https://cdn/high.mp4"]


def test_timeline_node_to_video_post():
    post = post_from_timeline_node(
        {
            "code": "VID123",
            "media_type": 2,
            "video_versions": [
                {"url": "https://cdn/low.mp4", "width": 480, "height": 480},
                {"url": "https://cdn/high.mp4", "width": 1080, "height": 1920},
            ],
        },
        username_hint="alice",
    )

    assert post is not None
    assert post.post_type == "video"
    assert post.username == "alice"
    assert post.media_urls == ["https://cdn/high.mp4"]
