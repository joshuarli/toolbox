"""Single-post scraper: navigate directly to /p/<shortid>/ and scrape."""

from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
from pathlib import Path
from typing import Any

from tool.ig.browser import browser_session, fetch_bytes
from tool.ig.errors import InstagramHttpError
from tool.ig.hq_video import parse_retry_after
from tool.ig.media import dest_path, media_url_ext, profile_key
from tool.ig.scraper import (
    SegmentCapture,
    navigate_to,
    raise_if_login,
    resolve_audio_url,
    save_post,
    scrape_direct,
    wait_ready,
)
from tool.ig.types import Pace, SavedScope
from tool.ig.video_save import download_and_mux_av, save_video_post_media


def _progress(reporter: Any, message: str) -> None:
    progress = getattr(reporter, "progress", None)
    if progress is None:
        reporter.info(message)
    else:
        progress(message)


class RateLimitError(InstagramHttpError):
    def __init__(self, shortid: str, *, retry_after: float | None = None, url: str = "") -> None:
        self.shortid = shortid
        detail = f" for {shortid}" if shortid else ""
        super().__init__(
            f"Instagram returned HTTP 429{detail}; backing off",
            status=429,
            retry_after=retry_after,
            url=url,
        )


class RateLimitTracker:
    def __init__(self) -> None:
        self.status = 0
        self.url = ""
        self.retry_after: float | None = None

    def on_response_status(self, status: int, url: str, headers: dict[str, str]) -> None:
        if status != 429:
            return
        self.status = status
        self.url = url
        self.retry_after = parse_retry_after(headers.get("retry-after"))

    def clear(self) -> None:
        self.status = 0
        self.url = ""
        self.retry_after = None

    def raise_if_limited(self, shortid: str) -> None:
        if self.status == 429:
            raise RateLimitError(shortid, retry_after=self.retry_after, url=self.url)


async def _raise_if_page_rate_limited(tab, shortid: str) -> None:
    info = await tab.evaluate("""
        (function() {
            var href = window.location.href || "";
            var title = document.title || "";
            var text = document.body ? document.body.innerText || "" : "";
            var hasArticle = !!document.querySelector("article");
            var statuses = [];
            try {
                statuses = performance.getEntriesByType("navigation")
                    .concat(performance.getEntriesByType("resource"))
                    .map(function(e) { return e.responseStatus || e.status || 0; })
                    .filter(Boolean);
            } catch (_) {}
            return {href: href, title: title, text: text.slice(0, 2000), statuses: statuses, hasArticle: hasArticle};
        })()
    """)
    if not isinstance(info, dict):
        return
    text = " ".join(str(info.get(key) or "") for key in ("href", "title", "text")).lower()
    statuses = info.get("statuses") or []
    if 429 in statuses or "http error 429" in text or "please wait a few minutes" in text:
        raise RateLimitError(shortid, url=str(info.get("href") or ""))
    # Only flag "try again later" when paired with a rate-limit-specific phrase
    # from Instagram's known rate-limiting page, not generic error pages.
    if "try again later" in text and ("we restrict" in text or "we limit" in text):
        raise RateLimitError(shortid, url=str(info.get("href") or ""))


async def _wait_for_navigation_start(tab, url: str, *, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        href = await tab.evaluate("window.location.href") or ""
        if href == url or href.startswith("chrome-error://"):
            return
        await asyncio.sleep(0.1)


async def _maybe_like(tab, *, reporter: Any | None = None) -> None:
    """Click Like on the current post with 1-in-20 probability."""
    if random.random() >= 0.05:
        return
    if reporter is None:
        reporter = logging.getLogger(__name__)

    from tool.ig.browser import ProtocolError

    try:
        liked = await tab.evaluate("""
            (function() {
                var svgs = Array.from(document.querySelectorAll('svg[aria-label="Like"]'));
                for (var i = 0; i < svgs.length; i++) {
                    var target = svgs[i].closest('button, [role="button"]');
                    if (!target) continue;
                    var r = target.getBoundingClientRect();
                    if (!r.width || !r.height) continue;
                    if (r.bottom <= 0 || r.right <= 0 || r.top >= window.innerHeight || r.left >= window.innerWidth) {
                        continue;
                    }
                    target.click();
                    return true;
                }
                return false;
            })()
        """)
    except ProtocolError, RuntimeError:
        reporter.debug("  like evaluate failed", exc_info=True)
        return
    if liked:
        reporter.info("  liked")
        try:
            await asyncio.sleep(random.uniform(0.3, 0.8))
        except asyncio.CancelledError:
            raise
        except Exception:
            reporter.debug("  like sleep interrupted", exc_info=True)


async def _save_mixed_carousel_videos(
    tab,
    post,
    *,
    seg_capture: SegmentCapture,
    bases_before: set[str],
    media_dir: str | Path,
    profile: str | None = None,
    reporter: Any | None = None,
) -> set[Path]:
    fresh_files: set[Path] = set()
    video_items = [
        (idx, url) for idx, url in enumerate(post.media_urls) if media_url_ext(post.post_type, url) == ".mp4"
    ]
    if not video_items:
        return fresh_files

    audio_url = await resolve_audio_url(tab, seg_capture=seg_capture, known_bases=bases_before)
    if not audio_url:
        return fresh_files

    total = len(post.media_urls)
    for idx, video_url in video_items:
        dest = dest_path(post.shortid, idx, total, ".mp4", media_dir=media_dir, profile=profile)
        if reporter is not None:
            _progress(reporter, f"carousel video mux started ({idx + 1}/{total})")
        await download_and_mux_av(video_url, audio_url, dest, reporter=reporter)
        if reporter is not None:
            _progress(reporter, f"carousel video mux finished ({idx + 1}/{total})")
        fresh_files.add(dest)
    return fresh_files


async def scrape_post(
    shortid: str,
    db: sqlite3.Connection,
    *,
    live: bool = False,
    pace: Pace | None = None,
    username_hint: str | None = None,
    hq_video: bool = True,
    media_dir: str | Path = "site/images",
    extensions: list[str] | None = None,
    ublock: bool = True,
) -> None:
    if pace is None:
        pace = Pace.fast()

    seg_capture = SegmentCapture()
    rate_limit = RateLimitTracker()
    async with browser_session(
        headless=not live,
        on_media_segment=seg_capture.add,
        on_response_status=rate_limit.on_response_status,
        extensions=extensions,
        ublock=ublock,
    ) as (_browser, tab):
        await tab.activate()
        try:
            await scrape_post_in_tab(
                tab,
                shortid,
                db,
                pace=pace,
                username_hint=username_hint,
                seg_capture=seg_capture,
                rate_limit=rate_limit,
                hq_video=hq_video,
                media_dir=media_dir,
            )
        except RuntimeError as exc:
            if str(exc) == f"scrape failed for {shortid}":
                raise SystemExit(1) from exc
            raise


async def scrape_post_in_tab(
    tab,
    shortid: str,
    db: sqlite3.Connection,
    *,
    pace: Pace | None = None,
    username_hint: str | None = None,
    seg_capture: SegmentCapture | None = None,
    rate_limit: RateLimitTracker | None = None,
    hq_video: bool = True,
    media_dir: str | Path = "site/images",
    profile_saved: SavedScope = "profile",
    reporter: Any | None = None,
    db_lock: asyncio.Lock | None = None,
) -> None:
    if pace is None:
        pace = Pace.fast()
    if seg_capture is None:
        seg_capture = SegmentCapture()
    if reporter is None:
        reporter = logging.getLogger(__name__)

    url = f"https://www.instagram.com/p/{shortid}/"
    reporter.info(f"  navigating to {url}")
    bases_before = seg_capture.known_bases()
    _progress(reporter, "page load started")
    await navigate_to(tab, url)
    await _wait_for_navigation_start(tab, url)
    if rate_limit is not None:
        rate_limit.raise_if_limited(shortid)
    await wait_ready(tab)
    if rate_limit is not None:
        rate_limit.raise_if_limited(shortid)
    await _raise_if_page_rate_limited(tab, shortid)
    raise_if_login(await tab.evaluate("window.location.href"))
    _progress(reporter, "page load finished")

    _progress(reporter, "scrape started")
    post = await scrape_direct(
        tab,
        shortid,
        pace=pace,
        seg_capture=seg_capture,
        known_bases=bases_before,
        reporter=reporter,
    )
    if rate_limit is not None:
        rate_limit.raise_if_limited(shortid)
    await _raise_if_page_rate_limited(tab, shortid)
    if not post:
        reporter.error(f"error: scrape failed for {shortid}")
        raise RuntimeError(f"scrape failed for {shortid}")
    _progress(reporter, f"scrape finished ({post.post_type}, {len(post.media_urls)} media)")

    existing = db.execute("SELECT username FROM post WHERE shortid = ?", (post.shortid,)).fetchone()
    existing_username = existing[0] if existing else None
    username = post.username or existing_username or username_hint or "unknown"
    ext = ".mp4" if post.post_type == "video" else ".jpg"
    reporter.info(f"  shortid:  {post.shortid}")
    reporter.info(f"  type:     {post.post_type}")
    reporter.info(f"  username: {username}")
    reporter.info(f"  media:    {len(post.media_urls)} file(s)")

    if post.post_type == "video":
        _progress(reporter, "video save started")
        await save_video_post_media(
            tab,
            db,
            post,
            username=username,
            username_hint=username_hint,
            seg_capture=seg_capture,
            bases_before=bases_before,
            hq_video=hq_video,
            media_dir=media_dir,
            profile_saved=profile_saved,
            hq_rate_limit_error=lambda exc: RateLimitError(post.shortid, retry_after=exc.retry_after, url=exc.url),
            reporter=reporter,
            db_lock=db_lock,
        )
        _progress(reporter, "video save finished")
    else:
        _profile = profile_key(username_hint, profile_saved) if username_hint else username
        fresh_files = await _save_mixed_carousel_videos(
            tab,
            post,
            seg_capture=seg_capture,
            bases_before=bases_before,
            media_dir=media_dir,
            profile=_profile,
            reporter=reporter,
        )
        _progress(reporter, "media save started")
        await save_post(
            db,
            post.shortid,
            username,
            post.caption,
            post.post_type,
            post.media_urls,
            ext,
            skip_existing_files=True,
            fresh_files=fresh_files,
            browser_fetch=lambda url: fetch_bytes(tab, url),
            media_dir=media_dir,
            profile_username=username_hint,
            profile_saved=profile_saved,
            reporter=reporter,
            db_lock=db_lock,
        )
        _progress(reporter, "media save finished")

    try:
        await _maybe_like(tab, reporter=reporter)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        reporter.info(f"  like skipped ({type(exc).__name__})")
