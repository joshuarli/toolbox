from __future__ import annotations

import inspect
import time
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# Browser adapter over the optional shadowdriver extension. Plain installs omit
# the ``browser`` extra, so this module must import without shadowdriver
# present. Browser entry points call ``require_shadowdriver()`` to fail
# with the install hint.

from tool.browser_require import require_shadowdriver
from tool.browser_require import shadowdriver as _shadowdriver

if _shadowdriver is not None:
    ProtocolError = _shadowdriver.ProtocolError
    BrowserInUseError = _shadowdriver.BrowserInUseError
    BrowserCrashedError = _shadowdriver.BrowserCrashedError
    UblockError = _shadowdriver.UblockError
else:

    class ProtocolError(Exception):
        pass

    class BrowserInUseError(Exception):
        pass

    class BrowserCrashedError(Exception):
        pass

    class UblockError(Exception):
        pass

BLOCKED_URL_PATTERNS = (
    {
        "protocol": "https",
        "hostname": "edge-chat.facebook.com",
        "pathname": "/mqtt/pull",
    },
)


async def _profile_process_commands() -> list[str]:
    return list(await require_shadowdriver().profile_process_commands())


async def _browser_log_tail(lines: int = 40) -> str:
    return str(await require_shadowdriver().browser_log_tail(lines))


async def close_browser(browser: Any, *, terminate: bool = False) -> None:
    close = getattr(browser, "close", None)
    if close is None:
        return
    try:
        result = close(terminate=terminate)
    except TypeError as exc:
        if "terminate" not in str(exc):
            raise
        result = close()
    if inspect.isawaitable(result):
        await result


@asynccontextmanager
async def browser_session(
    on_url_captured: Callable[[str], None] | None = None,
    url_filter: Callable[[str], bool] | None = None,
    headless: bool = True,
    on_media_segment: Callable[[str, int, bytes, int | None], None] | None = None,
    on_response_status: Callable[[int, str, dict[str, str]], None] | None = None,
    terminate: bool = True,
    extensions: Sequence[Path | str] | None = None,
    ublock: bool = True,
):
    browser, tab = await ensure_browser(
        on_url_captured=on_url_captured,
        url_filter=url_filter,
        headless=headless,
        on_media_segment=on_media_segment,
        on_response_status=on_response_status,
        extensions=extensions,
        ublock=ublock,
    )
    try:
        yield browser, tab
    finally:
        with suppress(Exception):
            await tab.close()
        await close_browser(browser, terminate=terminate)


async def ensure_browser(
    on_url_captured: Callable[[str], None] | None = None,
    url_filter: Callable[[str], bool] | None = None,
    headless: bool = True,
    on_media_segment: Callable[[str, int, bytes, int | None], None] | None = None,
    on_response_status: Callable[[int, str, dict[str, str]], None] | None = None,
    extensions: Sequence[Path | str] | None = None,
    ublock: bool = True,
):
    ext_paths = [Path(p) if isinstance(p, str) else p for p in (extensions or [])]
    sd = require_shadowdriver()
    if ublock:
        try:
            ublock_dir = await sd.ensure_ublock_origin()
            ext_paths.append(Path(ublock_dir))
        except UblockError as exc:
            print(f"uBlock Origin setup failed: {exc}", flush=True)

    browser, tab = await sd.ensure_browser(
        headless=headless,
        persistent=True,
        stealth=True,
        extensions=[str(p) for p in ext_paths] if ext_paths else None,
    )
    await _setup_tab(
        tab,
        on_url_captured=on_url_captured,
        url_filter=url_filter,
        on_media_segment=on_media_segment,
        on_response_status=on_response_status,
    )
    return browser, tab


async def new_tab(
    browser,
    on_url_captured: Callable[[str], None] | None = None,
    url_filter: Callable[[str], bool] | None = None,
    block_types: frozenset[str] | set[str] | None = None,
    on_body_captured: Callable[[str, bytes], None] | None = None,
    on_media_segment: Callable[[str, int, bytes, int | None], None] | None = None,
    on_response_status: Callable[[int, str, dict[str, str]], None] | None = None,
):
    del block_types, on_body_captured
    tab = await browser.new_tab()
    await _setup_tab(
        tab,
        on_url_captured=on_url_captured,
        url_filter=url_filter,
        on_media_segment=on_media_segment,
        on_response_status=on_response_status,
    )
    return tab


async def _setup_tab(
    tab,
    on_url_captured: Callable[[str], None] | None = None,
    url_filter: Callable[[str], bool] | None = None,
    on_media_segment: Callable[[str, int, bytes, int | None], None] | None = None,
    on_response_status: Callable[[int, str, dict[str, str]], None] | None = None,
) -> None:
    await _apply_resource_observation(
        tab,
        on_url_captured=on_url_captured,
        url_filter=url_filter,
        on_media_segment=on_media_segment,
        on_response_status=on_response_status,
    )
    await _apply_request_blocks(tab)
    await enable_network_logging(tab)


async def fetch_bytes(tab, url: str) -> bytes:
    return await tab.fetch_bytes(url)


async def navigate(tab, url: str, timeout: float = 15.0) -> None:
    await require_shadowdriver().navigate(tab, url, timeout)


async def human_click(*args, **kwargs):
    return await require_shadowdriver().human_click(*args, **kwargs)


async def human_scroll(*args, **kwargs):
    return await require_shadowdriver().human_scroll(*args, **kwargs)


async def think(*args, **kwargs):
    return await require_shadowdriver().think(*args, **kwargs)


async def sleep(*args, **kwargs):
    return await require_shadowdriver().sleep(*args, **kwargs)

# uBlock block notices are noise for normal runs; `ig --verbose` opts into them.
_net_verbose = False


def set_network_verbose(verbose: bool) -> None:
    global _net_verbose
    _net_verbose = verbose


async def enable_network_logging(tab) -> None:
    # Track requestId → URL for fetch errors (CDP's Network.loadingFailed
    # doesn't include the URL, only a requestId).
    _request_urls: dict[str, str] = {}

    def _on_before_request(params: dict[str, Any]) -> None:
        request = params.get("request") or {}
        req_id = request.get("request") or ""
        url = request.get("url") or ""
        if req_id and url:
            _request_urls[req_id] = url

    def _on_response(params: dict[str, Any]) -> None:
        response = params.get("response") or {}
        status = response.get("status") or 0
        if status < 400:
            return
        url = response.get("url") or ""
        short = url[:120] + "..." if len(url) > 120 else url
        print(f"  [{time.strftime('%H:%M:%S')}] [net] HTTP {status} {short}", flush=True)

    _blocked_urls: set[str] = set()

    def _on_fetch_error(params: dict[str, Any]) -> None:
        request = params.get("request") or {}
        url = request.get("url") or ""
        if not url:
            req_id = request.get("request") or ""
            url = _request_urls.pop(req_id, "") if req_id else ""
        if not url:
            url = "(url unknown)"
        if _is_blocked_url(url):
            return
        error_text = params.get("errorText") or ""
        # CDP reports "net::ERR_BLOCKED_BY_CLIENT"; match without the scheme
        # prefix so uBlock blocks stay out of the FAILED channel.
        if error_text.endswith("ERR_BLOCKED_BY_CLIENT"):
            if _net_verbose and url not in _blocked_urls:
                _blocked_urls.add(url)
                if url != "(url unknown)":
                    parts = urlsplit(url)
                    path = parts.path + (
                        "?" + parts.query[:60] + "..."
                        if len(parts.query) > 60
                        else "?" + parts.query
                        if parts.query
                        else ""
                    )
                    print(f"  [ublock] {parts.hostname}{path}", flush=True)
            return
        short = url[:120] + "..." if len(url) > 120 else url
        suffix = f" error={error_text}" if error_text else ""
        print(f"  [{time.strftime('%H:%M:%S')}] [net] FAILED {short}{suffix}", flush=True)

    tab.add_handler("network.beforeRequestSent", _on_before_request)
    tab.add_handler("network.responseStarted", _on_response)
    tab.add_handler("network.fetchError", _on_fetch_error)


def _is_blocked_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.hostname == "edge-chat.facebook.com" and parsed.path == "/mqtt/pull"


async def _apply_request_blocks(tab) -> None:
    with suppress(ProtocolError):
        await tab.block_requests(list(BLOCKED_URL_PATTERNS))

    async def _block(params: dict[str, Any]) -> None:
        request = params.get("request") or {}
        url = request.get("url") or ""
        request_id = request.get("request")
        if not request_id or not _is_blocked_url(url):
            return
        with suppress(ProtocolError):
            await tab.command("network.failRequest", {"request": request_id}, timeout=5.0)

    tab.add_handler("network.beforeRequestSent", _block)


async def _apply_resource_observation(
    tab,
    on_url_captured: Callable[[str], None] | None = None,
    url_filter: Callable[[str], bool] | None = None,
    on_media_segment: Callable[[str, int, bytes, int | None], None] | None = None,
    on_response_status: Callable[[int, str, dict[str, str]], None] | None = None,
) -> None:
    events = [
        "network.beforeRequestSent",
        "network.responseStarted",
        "network.responseCompleted",
        "network.fetchError",
    ]
    try:
        await tab.command("session.subscribe", {"events": events, "contexts": [tab.context]})
    except ProtocolError:
        await tab.command("session.subscribe", {"events": events})

    seen: set[str] = set()

    def _headers(response: dict[str, Any]) -> dict[str, str]:
        raw = response.get("headers") or []
        headers: dict[str, str] = {}
        if isinstance(raw, dict):
            raw = raw.items()
        for item in raw:
            if isinstance(item, tuple):
                name, value = item
            elif isinstance(item, dict):
                name = item.get("name")
                value = item.get("value")
                if isinstance(value, dict):
                    value = value.get("value")
            else:
                continue
            if name is not None and value is not None:
                headers[str(name).lower()] = str(value)
        return headers

    def _capture(params: dict[str, Any]) -> None:
        event_context = params.get("context")
        if event_context is not None and event_context != tab.context:
            return
        request = params.get("request") or {}
        response = params.get("response") or {}
        url = response.get("url") or request.get("url") or ""
        if on_response_status is not None and response.get("status") is not None:
            on_response_status(int(response.get("status") or 0), url, _headers(response))
        if not url or url in seen:
            return
        destination = request.get("destination")
        mime_type = response.get("mimeType") or ""
        is_image = destination == "image" or str(mime_type).startswith("image/")
        is_media = destination in {"video", "audio"} or ".mp4" in url
        if is_image and on_url_captured is not None and (url_filter is None or url_filter(url)):
            seen.add(url)
            on_url_captured(url)
        if is_media and on_media_segment is not None:
            seen.add(url)
            on_media_segment(url, 0, b"", None)

    tab.add_handler("network.responseStarted", _capture)
    tab.add_handler("network.responseCompleted", _capture)


async def _kill() -> None:
    sd = require_shadowdriver()
    profile_dir = await sd.default_profile_dir()
    await sd.kill()
    print(f"browser stopped ({profile_dir})")
