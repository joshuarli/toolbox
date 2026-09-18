"""Unit tests for tool.ig.browser."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from tool.ig import browser as browser_mod
from tool.ig.browser import (
    ProtocolError,
    _apply_request_blocks,
    _apply_resource_observation,
    browser_session,
    close_browser,
    enable_network_logging,
    set_network_verbose,
)


@pytest.mark.asyncio
class TestCloseBrowser:
    async def test_awaits_async_close(self):
        closed = False

        class FakeBrowser:
            async def close(self, *, terminate=False):
                nonlocal closed
                closed = True

        await close_browser(FakeBrowser(), terminate=True)
        assert closed

    async def test_sync_close_is_awaited(self):
        closed = False

        class FakeBrowser:
            def close(self, *, terminate=False):
                nonlocal closed
                closed = True

        await close_browser(FakeBrowser(), terminate=True)
        assert closed

    async def test_falls_back_when_close_rejects_terminate_kwarg(self):
        closed = False

        class OldFakeBrowser:
            def close(self):
                nonlocal closed
                closed = True

        await close_browser(OldFakeBrowser(), terminate=True)
        assert closed

    async def test_missing_close_is_noop(self):
        await close_browser(object(), terminate=True)


@pytest.mark.asyncio
class TestBrowserSession:
    async def test_terminates_on_exit(self, monkeypatch):
        tab = AsyncMock()
        browser = AsyncMock()
        seen = {}

        async def fake_ensure(**kwargs):
            seen["ensure"] = kwargs
            return browser, tab

        async def fake_close(b, *, terminate=False):
            seen["close"] = (b, terminate)

        monkeypatch.setattr(browser_mod, "ensure_browser", fake_ensure)
        monkeypatch.setattr(browser_mod, "close_browser", fake_close)

        async with browser_session(headless=False) as yielded:
            assert yielded == (browser, tab)

        assert seen["ensure"]["headless"] is False
        tab.close.assert_awaited_once()
        assert seen["close"] == (browser, True)

    async def test_terminates_on_cancellation(self, monkeypatch):
        tab = AsyncMock()
        browser = AsyncMock()
        seen = {}

        async def fake_ensure(**_):
            return browser, tab

        async def fake_close(b, *, terminate=False):
            seen["close"] = (b, terminate)

        monkeypatch.setattr(browser_mod, "ensure_browser", fake_ensure)
        monkeypatch.setattr(browser_mod, "close_browser", fake_close)

        with pytest.raises(asyncio.CancelledError):
            async with browser_session():
                raise asyncio.CancelledError

        tab.close.assert_awaited_once()
        assert seen["close"] == (browser, True)


@pytest.mark.asyncio
class TestResourceObservation:
    async def test_media_observation_records_url_for_cdn_fallback(self):
        handlers = {}
        captured = []
        tab = MagicMock()
        tab.context = "ctx"
        tab.command = AsyncMock()
        tab.add_handler = lambda event, callback: handlers.setdefault(event, callback)

        await _apply_resource_observation(tab, on_media_segment=lambda *args: captured.append(args))

        url = "https://scontent.cdninstagram.com/o1/v/t2/f2/video.mp4?bytestart=0&byteend=99"
        handlers["network.responseStarted"](
            {
                "request": {"url": url, "destination": "video"},
                "response": {"url": url, "mimeType": "video/mp4"},
            }
        )

        assert captured == [(url, 0, b"", None)]

    async def test_media_observation_ignores_other_contexts(self):
        handlers = {}
        captured = []
        tab = MagicMock()
        tab.context = "ctx"
        tab.command = AsyncMock()
        tab.add_handler = lambda event, callback: handlers.setdefault(event, callback)

        await _apply_resource_observation(tab, on_media_segment=lambda *args: captured.append(args))

        url = "https://scontent.cdninstagram.com/o1/v/t2/f2/video.mp4"
        handlers["network.responseStarted"](
            {
                "context": "other",
                "request": {"url": url, "destination": "video"},
                "response": {"url": url, "mimeType": "video/mp4"},
            }
        )

        assert captured == []

    async def test_reports_response_status_and_headers(self):
        handlers = {}
        statuses = []
        tab = MagicMock()
        tab.context = "ctx"
        tab.command = AsyncMock()
        tab.add_handler = lambda event, callback: handlers.setdefault(event, callback)

        await _apply_resource_observation(tab, on_response_status=lambda *args: statuses.append(args))

        url = "https://www.instagram.com/p/ABC/"
        handlers["network.responseStarted"](
            {
                "request": {"url": url},
                "response": {
                    "url": url,
                    "status": 429,
                    "headers": [{"name": "Retry-After", "value": {"type": "string", "value": "120"}}],
                },
            }
        )

        assert statuses == [(429, url, {"retry-after": "120"})]

    async def test_falls_back_to_session_wide_subscription(self):
        calls = []
        tab = MagicMock()
        tab.context = "ctx"
        tab.add_handler = lambda *_args: None

        async def fake_command(method, params=None, timeout=30.0):
            calls.append((method, params or {}))
            if len(calls) == 1:
                raise ProtocolError("session.subscribe: some error")
            return {}

        tab.command = AsyncMock(side_effect=fake_command)

        await _apply_resource_observation(tab)

        assert calls[0][1]["contexts"] == ["ctx"]
        assert "contexts" not in calls[1][1]


@pytest.mark.asyncio
class TestRequestBlocks:
    async def test_blocks_edge_chat_pull(self):
        handlers = {}
        calls = []
        tab = MagicMock()
        tab.context = "ctx"
        tab.add_handler = lambda event, callback: handlers.setdefault(event, callback)
        tab.block_requests = AsyncMock()

        async def fake_command(method, params=None, timeout=30.0):
            calls.append((method, params or {}, timeout))
            return {}

        tab.command = AsyncMock(side_effect=fake_command)

        await _apply_request_blocks(tab)
        await handlers["network.beforeRequestSent"](
            {
                "request": {
                    "request": "request-1",
                    "url": "https://edge-chat.facebook.com/mqtt/pull?sid=123&chunked=true",
                }
            }
        )

        tab.block_requests.assert_awaited_once()
        block_args = tab.block_requests.call_args[0][0]
        assert block_args[0]["hostname"] == "edge-chat.facebook.com"
        assert block_args[0]["pathname"] == "/mqtt/pull"
        assert calls[0] == ("network.failRequest", {"request": "request-1"}, 5.0)

    async def test_ignores_other_urls(self):
        handlers = {}
        calls = []
        tab = MagicMock()
        tab.context = "ctx"
        tab.add_handler = lambda event, callback: handlers.setdefault(event, callback)
        tab.block_requests = AsyncMock()

        async def fake_command(method, params=None, timeout=30.0):
            calls.append((method, params or {}, timeout))
            return {}

        tab.command = AsyncMock(side_effect=fake_command)

        await _apply_request_blocks(tab)
        await handlers["network.beforeRequestSent"](
            {
                "request": {
                    "request": "request-1",
                    "url": "https://www.instagram.com/graphql/query",
                }
            }
        )

        assert [call[0] for call in calls] == []


@pytest.mark.asyncio
class TestNetworkLogging:
    def _tab_with_handlers(self):
        handlers = {}
        tab = MagicMock()
        tab.add_handler = lambda event, callback: handlers.setdefault(event, callback)
        return tab, handlers

    def _feed_blocked_request(self, handlers, url: str) -> None:
        handlers["network.beforeRequestSent"]({"request": {"request": "r1", "url": url}})
        # CDP reports the net:: scheme prefix on this error text.
        handlers["network.fetchError"]({"request": {"request": "r1"}, "errorText": "net::ERR_BLOCKED_BY_CLIENT"})

    async def test_prints_http_4xx_5xx_responses(self, capsys):
        tab = MagicMock()
        handlers = {}
        tab.add_handler = lambda event, callback: handlers.setdefault(event, callback)

        await browser_mod.enable_network_logging(tab)

        url = "https://www.instagram.com/graphql/query"
        handlers["network.responseStarted"]({"response": {"status": 429, "url": url}})

        captured = capsys.readouterr()
        assert "HTTP 429" in captured.out
        assert url in captured.out

    async def test_ignores_2xx_3xx_responses(self, capsys):
        tab = MagicMock()
        handlers = {}
        tab.add_handler = lambda event, callback: handlers.setdefault(event, callback)

        await browser_mod.enable_network_logging(tab)

        handlers["network.responseStarted"]({"response": {"status": 200, "url": "https://www.instagram.com/p/ABC/"}})

        captured = capsys.readouterr()
        assert captured.out == ""

    async def test_reports_fetch_errors(self, capsys):
        tab = MagicMock()
        handlers = {}
        tab.add_handler = lambda event, callback: handlers.setdefault(event, callback)

        await browser_mod.enable_network_logging(tab)

        url = "https://scontent.cdninstagram.com/v/t51/photo.jpg"
        handlers["network.fetchError"]({"request": {"url": url}, "errorText": "net::ERR_NAME_NOT_RESOLVED"})

        captured = capsys.readouterr()
        assert "FAILED" in captured.out
        assert url in captured.out
        assert "net::ERR_NAME_NOT_RESOLVED" in captured.out

    async def test_suppresses_blocked_fetch_errors(self, capsys):
        tab = MagicMock()
        handlers = {}
        tab.add_handler = lambda event, callback: handlers.setdefault(event, callback)

        await browser_mod.enable_network_logging(tab)

        handlers["network.fetchError"](
            {
                "request": {"url": "https://edge-chat.facebook.com/mqtt/pull"},
                "errorText": "net::ERR_ABORTED",
            }
        )

        captured = capsys.readouterr()
        assert captured.out == ""

    @pytest.mark.asyncio
    async def test_ublock_blocked_by_client_is_silent_by_default(self, capsys):
        tab, handlers = self._tab_with_handlers()
        await enable_network_logging(tab)

        self._feed_blocked_request(handlers, "https://www.instagram.com/ajax/bz?__a=1")

        assert capsys.readouterr().out == ""

    @pytest.mark.asyncio
    async def test_verbose_prints_ublock_notice_once_per_url(self, capsys):
        set_network_verbose(True)
        try:
            tab, handlers = self._tab_with_handlers()
            await enable_network_logging(tab)
            url = "https://www.instagram.com/ajax/bz?__a=1&__comet_req=7"

            self._feed_blocked_request(handlers, url)
            self._feed_blocked_request(handlers, url)

            out = capsys.readouterr().out
            assert out.count("[ublock]") == 1
            assert "instagram.com/ajax/bz" in out
        finally:
            set_network_verbose(False)
