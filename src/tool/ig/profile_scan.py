"""Profile scan stubs and direct-post downloading."""

from __future__ import annotations

import asyncio
import re
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from tool.ig.browser import browser_session, fetch_bytes, new_tab
from tool.ig.db import clear_media, is_done, upsert_media, upsert_post
from tool.ig.post import RateLimitError, RateLimitTracker, scrape_post_in_tab
from tool.ig.profile_timeline import (
    ProfileTimelineUnavailableError,
    fetch_profile_posts_page,
    open_profile_timeline,
)
from tool.ig.scraper import (
    SegmentCapture,
    _video_quality_hint,
    navigate_to,
    raise_if_login,
    read_js,
    save_post,
    wait_ready,
)
from tool.ig.types import Pace, ScrapedPost
from tool.ig.video_save import save_video_post_media

_PAGE_TEXT_JS = read_js("page_text.js")
_GRID_LAYOUT_JS = read_js("grid_layout.js")
_PROFILE_APP_REFRESH_JS = read_js("profile_app_refresh.js")
_SCAN_SHORTIDS_JS = read_js("scan_shortids.js")
DEFAULT_SAVED_PROFILE = "________emanon________"


@dataclass(frozen=True)
class ProfileScope:
    username: str
    saved: bool = False

    @property
    def saved_int(self) -> int:
        return int(self.saved)

    @property
    def saved_arg(self) -> str:
        return " --saved" if self.saved else ""

    @property
    def saved_label(self) -> str:
        return " saved" if self.saved else ""


class _PostOutput:
    def __init__(self, prefix: str, progress_lock: threading.Lock) -> None:
        self.prefix = prefix
        self._progress_lock = progress_lock
        self._lines: list[tuple[TextIO | None, str]] = []

    def line(self, message: str, *, file=None) -> None:
        self._lines.append((file, message))

    def progress(self, message: str, *, file=None) -> None:
        with self._progress_lock:
            print(f"{self.prefix}: {message}", file=file, flush=True)

    def info(self, message: str, *args, **_kwargs) -> None:
        self.line(message % args if args else message)

    def warning(self, message: str, *args, **_kwargs) -> None:
        self.line(message % args if args else message, file=sys.stderr)

    def error(self, message: str, *args, **_kwargs) -> None:
        self.line(message % args if args else message, file=sys.stderr)

    def debug(self, _message: str, *args, **_kwargs) -> None:
        return

    async def flush(self, lock: asyncio.Lock) -> None:
        async with lock:
            for file, message in self._lines:
                print(f"{self.prefix}: {message.strip()}", file=file, flush=True)
            self._lines.clear()


class _ParallelRateLimitShutdownError(Exception):
    def __init__(self, error: RateLimitError) -> None:
        self.error = error
        super().__init__(str(error))


# -- DB helpers ----------------------------------------------------------------


def init_profile_table(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS profile_scrape (
            username           TEXT PRIMARY KEY,
            last_shortid       TEXT,
            updated_at         INTEGER,
            current_shortid    TEXT,
            current_updated_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS profile_failure (
            id          INTEGER PRIMARY KEY,
            username    TEXT NOT NULL,
            saved       INTEGER NOT NULL DEFAULT 0,
            shortid     TEXT NOT NULL,
            stage       TEXT NOT NULL,
            reason      TEXT NOT NULL,
            failed_at   INTEGER NOT NULL,
            resolved_at INTEGER
        );
    """)
    cols = {row[1] for row in db.execute("PRAGMA table_info(profile_failure)")}
    if "saved" not in cols:
        db.execute("ALTER TABLE profile_failure ADD COLUMN saved INTEGER NOT NULL DEFAULT 0")
    db.execute(
        """
        UPDATE profile_failure
        SET resolved_at = failed_at
        WHERE resolved_at IS NULL
          AND id NOT IN (
              SELECT MAX(id)
              FROM profile_failure
              WHERE resolved_at IS NULL
              GROUP BY username, saved, shortid, stage
          )
        """
    )
    db.execute("DROP INDEX IF EXISTS profile_failure_pending_idx")
    db.execute(
        """
        CREATE INDEX profile_failure_pending_idx
        ON profile_failure (username, saved, shortid, resolved_at)
        """
    )
    db.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS profile_failure_open_unique_idx
        ON profile_failure (username, saved, shortid, stage)
        WHERE resolved_at IS NULL
        """
    )
    db.commit()


def _format_failure(exc: Exception) -> str:
    detail = str(exc).strip()
    if detail:
        return f"{type(exc).__name__}: {detail}"
    return type(exc).__name__


def _truncate_reason(raw: str, *, max_len: int = 4000) -> str:
    """Truncate a failure reason, preserving the tail when possible.

    Chained exception traces often place the root cause near the end, so a
    simple [:max_len] slice discards the most diagnostic information.  Instead
    keep both ends when the string overflows.
    """
    if len(raw) <= max_len:
        return raw
    head = max_len // 2
    tail = max_len - head - 100
    return f"{raw[:head]} ... [truncated {len(raw) - max_len} chars] ... {raw[-tail:]}"


def _record_failure(
    db: sqlite3.Connection,
    username: str,
    shortid: str,
    stage: str,
    reason: str,
    *,
    saved: bool = False,
) -> None:
    db.execute(
        """
        INSERT INTO profile_failure (username, saved, shortid, stage, reason, failed_at, resolved_at)
        VALUES (?, ?, ?, ?, ?, ?, NULL)
        ON CONFLICT(username, saved, shortid, stage) WHERE resolved_at IS NULL DO UPDATE SET
            reason = excluded.reason,
            failed_at = excluded.failed_at
        """,
        (username, int(saved), shortid, stage, _truncate_reason(reason), int(time.time())),
    )
    db.commit()


def _clear_failure(db: sqlite3.Connection, username: str, shortid: str, *, saved: bool = False) -> None:
    db.execute(
        """
        UPDATE profile_failure
        SET resolved_at = ?
        WHERE username = ? AND saved = ? AND shortid = ? AND resolved_at IS NULL
        """,
        (int(time.time()), username, int(saved), shortid),
    )
    db.commit()


# -- Browser helpers -----------------------------------------------------------


async def _wait_grid_layout(tab, *, attempts: int = 20, delay: float = 0.5):
    for _ in range(attempts):
        layout = await tab.evaluate(_GRID_LAYOUT_JS)
        if layout:
            return layout
        await asyncio.sleep(delay)
    return None


async def _wait_scroll_height_growth(
    tab,
    previous_h: float,
    *,
    timeout: float = 3.0,
    poll: float = 0.25,
) -> float:
    polls = max(1, int(timeout / poll))
    current_h = previous_h
    for _ in range(polls):
        await asyncio.sleep(poll)
        current_h = float(await tab.evaluate("document.documentElement.scrollHeight"))
        if current_h > previous_h + 10:
            return current_h
    return current_h


async def _hard_refresh_profile_page(tab) -> None:
    try:
        await tab.evaluate(_PROFILE_APP_REFRESH_JS)
    except Exception:
        from logging import getLogger

        getLogger(__name__).debug("profile_app_refresh JS failed, falling back to protocol reload")
    # tab.command speaks raw CDP: Page.reload is the supported reload primitive.
    # WebDriver-BiDi names such as browsingContext.reload are not shimmed and
    # fail with "'...' wasn't found".
    await tab.command("Page.reload", {"ignoreCache": True}, timeout=30.0)
    await wait_ready(tab)


async def _auto_follow_profile(tab, username: str) -> None:
    """Legacy compatibility hook; automatic follows are intentionally disabled."""
    del tab, username


# -- Public API ----------------------------------------------------------------


_profile_tables_ensured: set[int] = set()


def init_profile_scan(db: sqlite3.Connection) -> None:
    """Ensure profile-scrape and profile-failure tables exist.

    Idempotent and cheap after the first call per connection — repeated
    invocations from save_profile_shortids, scanned_profile_shortids, etc.
    are short-circuited via connection id tracking.
    """
    if id(db) in _profile_tables_ensured:
        row = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'profile_failure'").fetchone()
        if row:
            return
    init_profile_table(db)
    _profile_tables_ensured.add(id(db))


def save_profile_shortids(db: sqlite3.Connection, username: str, shortids: list[str], *, saved: bool = False) -> int:
    init_profile_scan(db)
    saved_int = int(saved)
    before = int(
        db.execute(
            "SELECT COUNT(*) FROM profile_post WHERE username = ? AND saved = ?",
            (username, saved_int),
        ).fetchone()[0]
    )
    db.executemany(
        """
        INSERT OR IGNORE INTO post (shortid, username, caption, post_type, scraped_at)
        VALUES (?, NULL, NULL, NULL, NULL)
        """,
        [(shortid,) for shortid in shortids],
    )
    start_seq = db.execute(
        "SELECT COALESCE(MAX(seq) + 1, 0) FROM profile_post WHERE username = ? AND saved = ?",
        (username, saved_int),
    ).fetchone()[0]
    seen_at = int(time.time())
    db.executemany(
        """
        INSERT OR IGNORE INTO profile_post (username, saved, shortid, seen_at, seq)
        VALUES (?, ?, ?, ?, ?)
        """,
        [(username, saved_int, shortid, seen_at, start_seq + i) for i, shortid in enumerate(shortids)],
    )
    db.commit()
    after = int(
        db.execute(
            "SELECT COUNT(*) FROM profile_post WHERE username = ? AND saved = ?",
            (username, saved_int),
        ).fetchone()[0]
    )
    return after - before


def save_profile_posts_metadata(
    db: sqlite3.Connection,
    username: str,
    posts: list[ScrapedPost],
    *,
    saved: bool = False,
) -> int:
    init_profile_scan(db)
    saved_int = int(saved)
    before = int(
        db.execute(
            "SELECT COUNT(*) FROM profile_post WHERE username = ? AND saved = ?",
            (username, saved_int),
        ).fetchone()[0]
    )
    start_seq = db.execute(
        "SELECT COALESCE(MAX(seq) + 1, 0) FROM profile_post WHERE username = ? AND saved = ?",
        (username, saved_int),
    ).fetchone()[0]
    seen_at = int(time.time())
    for offset, post in enumerate(posts):
        upsert_post(db, post.shortid, post.username or username, post.caption, post.post_type)
        clear_media(db, post.shortid, len(post.media_urls))
        for idx, url in enumerate(post.media_urls):
            upsert_media(db, post.shortid, idx, url)
        db.execute(
            """
            INSERT OR IGNORE INTO profile_post (username, saved, shortid, seen_at, seq)
            VALUES (?, ?, ?, ?, ?)
            """,
            (username, saved_int, post.shortid, seen_at, start_seq + offset),
        )
    db.commit()
    after = int(
        db.execute(
            "SELECT COUNT(*) FROM profile_post WHERE username = ? AND saved = ?",
            (username, saved_int),
        ).fetchone()[0]
    )
    return after - before


def scanned_profile_shortids(db: sqlite3.Connection, username: str, *, saved: bool = False) -> list[str]:
    init_profile_scan(db)
    rows = db.execute(
        """
        SELECT shortid
        FROM profile_post
        WHERE username = ? AND saved = ?
        ORDER BY seq, shortid
        """,
        (username, int(saved)),
    ).fetchall()
    return [row[0] for row in rows]


def _pending_profile_shortids(
    db: sqlite3.Connection,
    scope: ProfileScope,
    shortids: list[str],
    *,
    media_dir: str | Path,
) -> list[str]:
    pending: list[str] = []
    for shortid in shortids:
        if is_done(db, shortid, scope.username, media_dir=media_dir, saved=scope.saved):
            # Profile timeline metadata often exposes only the 720p H.264
            # preview.  A completed file with a known low-quality URL remains
            # pending until the download path replaces it with authenticated
            # DASH media.
            quality_row = db.execute(
                "SELECT p.post_type, m.url FROM post p LEFT JOIN media m ON m.shortid = p.shortid AND m.idx = 0 "
                "WHERE p.shortid = ?",
                (shortid,),
            ).fetchone()
            quality_hint = _video_quality_hint(str(quality_row[1] or "")) if quality_row else 0
            needs_quality_upgrade = bool(quality_row and quality_row[0] == "video" and 0 < quality_hint < 1080)
            if needs_quality_upgrade:
                pending.append(shortid)
            else:
                _clear_failure(db, scope.username, shortid, saved=scope.saved)
        else:
            pending.append(shortid)
    return pending


async def _wait_scan_grid(tab) -> None:
    for _ in range(20):
        layout = await _wait_grid_layout(tab, attempts=1, delay=0)
        shortids = await tab.evaluate(_SCAN_SHORTIDS_JS) or []
        if layout or shortids:
            return
        await asyncio.sleep(0.5)
    print("scan: profile grid did not render, hard refreshing once", flush=True)
    await _hard_refresh_profile_page(tab)
    for _ in range(30):
        layout = await _wait_grid_layout(tab, attempts=1, delay=0)
        shortids = await tab.evaluate(_SCAN_SHORTIDS_JS) or []
        if layout or shortids:
            return
        await asyncio.sleep(0.5)
    text = await tab.evaluate(_PAGE_TEXT_JS)
    if "Something went wrong" in text:
        raise RuntimeError("scan: Instagram rendered its error page instead of the profile grid; retry later")
    raise RuntimeError("scan: profile grid did not render")


async def _wait_saved_scan_grid(tab) -> None:
    for _ in range(20):
        layout = await _wait_grid_layout(tab, attempts=1, delay=0)
        shortids = await tab.evaluate(_SCAN_SHORTIDS_JS) or []
        if layout or shortids:
            return
        await asyncio.sleep(0.5)
    print("scan: saved grid did not render, hard refreshing once", flush=True)
    await _hard_refresh_profile_page(tab)
    for _ in range(30):
        layout = await _wait_grid_layout(tab, attempts=1, delay=0)
        shortids = await tab.evaluate(_SCAN_SHORTIDS_JS) or []
        if layout or shortids:
            return
        await asyncio.sleep(0.5)
    text = await tab.evaluate(_PAGE_TEXT_JS)
    if "Something went wrong" in text:
        raise RuntimeError("scan: Instagram rendered its error page instead of the saved grid; retry later")
    raise RuntimeError("scan: saved grid did not render")


_PLACEHOLDER_URL_RE = re.compile(r"(?:rsrc\.php|/null\.(?:jpg|png|gif|webp))(?:$|[?#])")


def _stored_post_metadata(db: sqlite3.Connection, shortid: str) -> ScrapedPost | None:
    row = db.execute(
        "SELECT username, caption, post_type FROM post WHERE shortid = ?",
        (shortid,),
    ).fetchone()
    if not row or row[2] not in {"image", "carousel", "video"}:
        return None
    media_urls = [
        media_row[0]
        for media_row in db.execute(
            "SELECT url FROM media WHERE shortid = ? ORDER BY idx",
            (shortid,),
        ).fetchall()
        if media_row[0]
    ]
    if not media_urls or any(_PLACEHOLDER_URL_RE.search(u) for u in media_urls):
        return None
    return ScrapedPost(shortid, row[2], media_urls, username=row[0], caption=row[1])


async def _download_stored_post_metadata(
    tab,
    db: sqlite3.Connection,
    scope: ProfileScope,
    post: ScrapedPost,
    *,
    media_dir: str | Path,
    reporter=None,
    db_lock: asyncio.Lock | None = None,
) -> None:
    username = post.username or scope.username
    ext = ".mp4" if post.post_type == "video" else ".jpg"
    if post.post_type == "video":
        await save_video_post_media(
            tab,
            db,
            post,
            username=username,
            username_hint=scope.username,
            seg_capture=SegmentCapture(),
            bases_before=set(),
            hq_video=True,
            media_dir=media_dir,
            profile_saved="saved" if scope.saved else "profile",
            hq_rate_limit_error=lambda exc: RateLimitError(
                post.shortid,
                retry_after=exc.retry_after,
                url=exc.url,
            ),
            resolve_captured_audio=False,
            reporter=reporter,
            db_lock=db_lock,
        )
        return
    await save_post(
        db,
        post.shortid,
        username,
        post.caption,
        post.post_type,
        post.media_urls,
        ext,
        skip_existing_files=True,
        browser_fetch=lambda url: fetch_bytes(tab, url),
        media_dir=media_dir,
        profile_username=scope.username,
        profile_saved="saved" if scope.saved else "profile",
        reporter=reporter,
        db_lock=db_lock,
    )


async def _download_direct_post(
    browser,
    shortid: str,
    db: sqlite3.Connection,
    scope: ProfileScope,
    *,
    pace: Pace,
    media_dir: str | Path,
    reporter,
    db_lock: asyncio.Lock,
) -> None:
    seg_capture = SegmentCapture()
    rate_limit = RateLimitTracker()
    direct_tab = await new_tab(
        browser,
        on_media_segment=seg_capture.add,
        on_response_status=rate_limit.on_response_status,
    )
    try:
        await direct_tab.activate()
        await scrape_post_in_tab(
            direct_tab,
            shortid,
            db,
            pace=pace,
            username_hint=scope.username,
            seg_capture=seg_capture,
            rate_limit=rate_limit,
            media_dir=media_dir,
            profile_saved="saved" if scope.saved else "profile",
            reporter=reporter,
            db_lock=db_lock,
        )
    finally:
        try:
            await direct_tab.close()
        except Exception:
            pass


async def _scan_profile_timeline(
    tab,
    username: str,
    db: sqlite3.Connection,
    *,
    max_pages: int | None,
    stop_at_known: bool,
    known_shortids: set[str],
) -> bool:
    session = await open_profile_timeline(tab, username)
    seen: set[str] = set()
    total_posts = 0
    inserted_total = 0
    last_reported = 0
    after: str | None = None
    page_index = 0
    stopped_by_limit = False
    stopped_at_known = False

    while True:
        if max_pages is not None and page_index >= max_pages:
            stopped_by_limit = True
            break
        page = await fetch_profile_posts_page(session, after=after)
        page_index += 1
        if not page.posts:
            break

        unique_posts: list[ScrapedPost] = []
        saw_known = False
        saw_known_without_metadata = False
        new_on_page = 0
        for post in page.posts:
            if post.shortid in known_shortids:
                saw_known = True
                if _stored_post_metadata(db, post.shortid) is None:
                    saw_known_without_metadata = True
            elif post.shortid not in seen:
                new_on_page += 1
            if post.shortid not in seen:
                seen.add(post.shortid)
                unique_posts.append(post)

        inserted_total += save_profile_posts_metadata(db, username, unique_posts)
        total_posts += len(unique_posts)
        if total_posts >= last_reported + 50:
            print(f"scan: progress {total_posts} posts", flush=True)
            last_reported = total_posts

        if stop_at_known and saw_known and new_on_page == 0 and not saw_known_without_metadata:
            stopped_at_known = True
            break
        if not page.has_next_page or not page.end_cursor:
            break
        after = page.end_cursor

    if stopped_by_limit:
        print(f"scan: stopped after {max_pages} timeline pages", file=sys.stderr)
    elif stopped_at_known:
        print("scan: reached an already-known post", flush=True)
    print(f"scan: recorded metadata for {total_posts} posts for @{username} ({inserted_total} new)", flush=True)
    return True


async def scan_profile(
    username: str,
    db: sqlite3.Connection,
    *,
    live: bool = False,
    max_scrolls: int | None = None,
    stop_at_known: bool = True,
    saved: bool = False,
    extensions: list[str] | None = None,
    ublock: bool = True,
) -> None:
    init_profile_scan(db)
    async with browser_session(headless=not live, extensions=extensions, ublock=ublock) as (_browser, tab):
        await _scan_profile_in_tab(
            tab,
            username,
            db,
            max_scrolls=max_scrolls,
            stop_at_known=stop_at_known,
            saved=saved,
        )


async def scan_profiles(
    usernames: list[str],
    db: sqlite3.Connection,
    *,
    live: bool = False,
    max_scrolls: int | None = None,
    stop_at_known: bool = True,
    saved: bool = False,
    extensions: list[str] | None = None,
    ublock: bool = True,
) -> None:
    """Scan profiles in one browser session, keeping later profiles best-effort."""
    init_profile_scan(db)
    async with browser_session(headless=not live, extensions=extensions, ublock=ublock) as (_browser, tab):
        for username in usernames:
            try:
                await _scan_profile_in_tab(
                    tab,
                    username,
                    db,
                    max_scrolls=max_scrolls,
                    stop_at_known=stop_at_known,
                    saved=saved,
                )
            except Exception as exc:
                db.rollback()
                print(
                    f"scan: failed to scan @{username}; continuing with remaining profiles ({_format_failure(exc)})",
                    file=sys.stderr,
                    flush=True,
                )


async def _scan_profile_in_tab(
    tab,
    username: str,
    db: sqlite3.Connection,
    *,
    max_scrolls: int | None,
    stop_at_known: bool,
    saved: bool,
) -> None:
    known_shortids = set(scanned_profile_shortids(db, username, saved=saved)) if stop_at_known else set()
    # Instagram no longer serves /<username>/saved/all-posts/ (it redirects to the
    # bare profile); the saved collection lives at the global /saved/all-posts/ URL.
    profile_url = "https://www.instagram.com/saved/all-posts/" if saved else f"https://www.instagram.com/{username}/"
    label = "saved profile" if saved else "profile"
    print(f"scanning {label}: {profile_url}", flush=True)
    await tab.activate()
    if not saved:
        try:
            await _scan_profile_timeline(
                tab,
                username,
                db,
                max_pages=max_scrolls,
                stop_at_known=stop_at_known,
                known_shortids=known_shortids,
            )
            return
        except ProfileTimelineUnavailableError as exc:
            print(
                f"scan: profile timeline unavailable ({exc}); falling back to grid scroll",
                file=sys.stderr,
            )
    await navigate_to(tab, profile_url)
    await wait_ready(tab)
    raise_if_login(await tab.evaluate("window.location.href"))
    if saved:
        await _wait_saved_scan_grid(tab)
    else:
        await _wait_scan_grid(tab)

    seen: list[str] = []
    seen_set: set[str] = set()
    stalls = 0
    last_reported = 0

    scrolls = 0
    stopped_by_limit = False
    stopped_at_known = False
    while True:
        if max_scrolls is not None and scrolls >= max_scrolls:
            stopped_by_limit = True
            break
        scrolls += 1
        shortids = await tab.evaluate(_SCAN_SHORTIDS_JS) or []
        before = len(seen_set)
        saw_known = False
        for shortid in shortids:
            if shortid in known_shortids:
                saw_known = True
                continue
            if shortid not in seen_set:
                seen_set.add(shortid)
                seen.append(shortid)
        added = len(seen_set) - before

        if len(seen) >= last_reported + 50:
            print(f"scan: progress {len(seen)} posts", flush=True)
            last_reported = len(seen)

        scroll_h = float(await tab.evaluate("document.documentElement.scrollHeight"))
        await tab.evaluate(f"window.scrollTo(0, {scroll_h:.0f})")
        new_h = await _wait_scroll_height_growth(tab, scroll_h, timeout=3.0)
        if len(seen_set) == before and new_h <= scroll_h + 10:
            stalls += 1
        else:
            stalls = 0
        if stop_at_known and scrolls > 1 and saw_known and added == 0 and new_h <= scroll_h + 10:
            stopped_at_known = True
            break
        if stalls >= 5:
            break
    if stopped_by_limit:
        print(f"scan: stopped after {max_scrolls} scrolls", file=sys.stderr)
    elif stopped_at_known:
        print("scan: reached an already-known post", flush=True)

    inserted = save_profile_shortids(db, username, seen, saved=saved)
    saved_label = " saved" if saved else ""
    print(f"scan: recorded {len(seen)}{saved_label} posts for @{username} ({inserted} new stubs)", flush=True)


async def _download_scope_posts(
    browser,
    bootstrap_tab,
    scope: ProfileScope,
    db: sqlite3.Connection,
    pending: list[str],
    *,
    pace: Pace,
    media_dir: str | Path = "site/images",
    jobs: int = 1,
) -> None:
    """Download pending posts for one scope using an already-open browser."""
    if not pending:
        return

    worker_count = min(jobs, len(pending))
    if worker_count > 1:
        print(f"scan: using {worker_count} download jobs", flush=True)

    queue: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
    for index, shortid in enumerate(pending, 1):
        queue.put_nowait((index, shortid))

    db_lock = asyncio.Lock()
    output_lock = asyncio.Lock()
    progress_lock = threading.Lock()
    shutdown_event = asyncio.Event()

    async def worker(worker_index: int) -> None:
        metadata_tab = bootstrap_tab if worker_index == 0 else None
        close_metadata_tab = False
        metadata_tab_ready = False
        try:
            while True:
                if shutdown_event.is_set():
                    return
                try:
                    index, shortid = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return

                output = _PostOutput(
                    f"[{index}/{len(pending)}] {shortid}",
                    progress_lock,
                )
                output.progress(f"started scanned{scope.saved_label} post")
                try:
                    stored_post = _stored_post_metadata(db, shortid)
                    if stored_post:
                        try:
                            if metadata_tab is None:
                                output.progress("opening metadata tab")
                                metadata_tab = await new_tab(browser)
                                close_metadata_tab = True
                            if not metadata_tab_ready:
                                output.progress("loading profile metadata page")
                                await metadata_tab.activate()
                                await navigate_to(metadata_tab, f"https://www.instagram.com/{scope.username}/")
                                await wait_ready(metadata_tab)
                                raise_if_login(await metadata_tab.evaluate("window.location.href"))
                                metadata_tab_ready = True
                                output.progress("profile metadata page ready")
                            output.progress("downloading from stored profile metadata")
                            await _download_stored_post_metadata(
                                metadata_tab,
                                db,
                                scope,
                                stored_post,
                                media_dir=media_dir,
                                reporter=output,
                                db_lock=db_lock,
                            )
                        except Exception as exc:
                            async with db_lock:
                                db.rollback()
                                completed = is_done(
                                    db,
                                    shortid,
                                    scope.username,
                                    media_dir=media_dir,
                                    saved=scope.saved,
                                )
                            if completed:
                                raise
                            output.progress(
                                f"stored profile metadata failed ({type(exc).__name__}); refreshing direct post"
                            )
                            if isinstance(exc, RateLimitError):
                                raise
                            await _download_direct_post(
                                browser,
                                shortid,
                                db,
                                scope,
                                pace=pace,
                                media_dir=media_dir,
                                reporter=output,
                                db_lock=db_lock,
                            )
                    else:
                        output.progress("opening direct post tab")
                        await _download_direct_post(
                            browser,
                            shortid,
                            db,
                            scope,
                            pace=pace,
                            media_dir=media_dir,
                            reporter=output,
                            db_lock=db_lock,
                        )
                except Exception as exc:
                    async with db_lock:
                        db.rollback()
                        completed = is_done(db, shortid, scope.username, media_dir=media_dir, saved=scope.saved)
                        if completed:
                            _clear_failure(db, scope.username, shortid, saved=scope.saved)
                            output.line(
                                f"scanned{scope.saved_label} post completed; "
                                f"post-processing failed: {_format_failure(exc)}",
                                file=sys.stderr,
                            )
                        else:
                            _record_failure(
                                db,
                                scope.username,
                                shortid,
                                "scan_direct",
                                _format_failure(exc),
                                saved=scope.saved,
                            )
                            output.line(
                                f"scanned{scope.saved_label} post failed, logged for retry: {_format_failure(exc)}",
                                file=sys.stderr,
                            )
                    if isinstance(exc, RateLimitError):
                        shutdown_event.set()
                        delay = exc.backoff_seconds()
                        output.line(
                            f"HTTP 429 detected; stopping active jobs (retry after {delay / 60:.1f} minutes)",
                            file=sys.stderr,
                        )
                        await output.flush(output_lock)
                        raise _ParallelRateLimitShutdownError(exc) from exc
                    await output.flush(output_lock)
                else:
                    async with db_lock:
                        _clear_failure(db, scope.username, shortid, saved=scope.saved)
                    output.progress("completed")
                    await output.flush(output_lock)
                finally:
                    queue.task_done()
                    if not queue.empty():
                        await asyncio.sleep(pace.inter_post[0])
        finally:
            if close_metadata_tab and metadata_tab is not None:
                try:
                    await metadata_tab.close()
                except Exception:
                    pass

    tasks = [asyncio.create_task(worker(worker_index)) for worker_index in range(worker_count)]
    try:
        done, pending_tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        rate_limited = None
        unexpected_exc = None
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if isinstance(exc, _ParallelRateLimitShutdownError):
                rate_limited = exc
                break
            if exc is not None:
                unexpected_exc = exc
                break
        if rate_limited is not None:
            shutdown_event.set()
            for task in pending_tasks:
                task.cancel()
            await asyncio.gather(*pending_tasks, return_exceptions=True)
            return
        if unexpected_exc is not None:
            for task in pending_tasks:
                task.cancel()
            await asyncio.gather(*pending_tasks, return_exceptions=True)
            raise unexpected_exc
        await asyncio.gather(*pending_tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def download_scanned_profile(
    username: str,
    db: sqlite3.Connection,
    *,
    live: bool = False,
    pace: Pace | None = None,
    media_dir: str | Path = "site/images",
    saved: bool = False,
    jobs: int = 1,
    extensions: list[str] | None = None,
    ublock: bool = True,
) -> None:
    if pace is None:
        pace = Pace.fast()
    if jobs < 1:
        raise ValueError("jobs must be at least 1")
    scope = ProfileScope(username, saved)
    shortids = scanned_profile_shortids(db, scope.username, saved=scope.saved)
    if not shortids:
        print(
            f"no scanned{scope.saved_label} posts for @{scope.username}; scanning first",
            flush=True,
        )
        await scan_profile(scope.username, db, live=live, saved=scope.saved, extensions=extensions, ublock=ublock)
        shortids = scanned_profile_shortids(db, scope.username, saved=scope.saved)
        if not shortids:
            print(f"scan found no posts for @{scope.username}", flush=True)
            return

    pending = _pending_profile_shortids(db, scope, shortids, media_dir=media_dir)
    print(
        f"scan: {len(shortids)} known{scope.saved_label} posts, {len(pending)} pending downloads for @{scope.username}",
        flush=True,
    )
    if not pending:
        return

    async with browser_session(headless=not live, extensions=extensions, ublock=ublock) as (browser, bootstrap_tab):
        await _download_scope_posts(
            browser,
            bootstrap_tab,
            scope,
            db,
            pending,
            pace=pace,
            media_dir=media_dir,
            jobs=jobs,
        )


async def download_scanned_profiles(
    scopes: list[tuple[str, bool]],
    db: sqlite3.Connection,
    *,
    live: bool = False,
    pace: Pace | None = None,
    media_dir: str | Path = "site/images",
    jobs: int = 1,
    extensions: list[str] | None = None,
    ublock: bool = True,
) -> None:
    """Download pending posts for multiple scopes, reusing one browser session."""
    if pace is None:
        pace = Pace.fast()
    if jobs < 1:
        raise ValueError("jobs must be at least 1")

    scope_pending: list[tuple[ProfileScope, list[str]]] = []
    for username, saved in scopes:
        scope = ProfileScope(username, saved)
        shortids = scanned_profile_shortids(db, scope.username, saved=scope.saved)
        if not shortids:
            print(
                f"no scanned{scope.saved_label} posts for @{scope.username}; scanning first",
                flush=True,
            )
            await scan_profile(scope.username, db, live=live, saved=scope.saved, extensions=extensions, ublock=ublock)
            shortids = scanned_profile_shortids(db, scope.username, saved=scope.saved)
            if not shortids:
                print(f"scan found no posts for @{scope.username}", flush=True)
                continue
        pending = _pending_profile_shortids(db, scope, shortids, media_dir=media_dir)
        print(
            f"scan: {len(shortids)} known{scope.saved_label} posts, "
            f"{len(pending)} pending downloads for @{scope.username}",
            flush=True,
        )
        if pending:
            scope_pending.append((scope, pending))

    if not scope_pending:
        return

    async with browser_session(headless=not live, extensions=extensions, ublock=ublock) as (browser, bootstrap_tab):
        for scope, pending in scope_pending:
            await _download_scope_posts(
                browser,
                bootstrap_tab,
                scope,
                db,
                pending,
                pace=pace,
                media_dir=media_dir,
                jobs=jobs,
            )
