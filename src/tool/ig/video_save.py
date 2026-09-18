from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tool.ig.browser import ProtocolError
from tool.ig.browser_types import TabLike
from tool.ig.hq_video import HqVideoFetchError, resolve_hq_video_tracks
from tool.ig.media import profile_key
from tool.ig.scraper import (
    SegmentCapture,
    _ranked_video_urls,
    dest_path,
    download_bytes,
    mux_av,
    resolve_audio_url,
    save_post,
    strip_byte_range,
)
from tool.ig.types import SavedScope, ScrapedPost


def _progress(reporter: Any, message: str) -> None:
    progress = getattr(reporter, "progress", None)
    if progress is None:
        reporter.info(message)
    else:
        progress(message)


def captured_video_base_for_url(
    seg_capture: SegmentCapture,
    url: str,
    *,
    exclude: set[str] | None = None,
) -> str | None:
    selected_base = strip_byte_range(url)
    for base in seg_capture.get_video_bases(exclude=exclude):
        if strip_byte_range(base) == selected_base:
            return base
    return None


async def download_and_mux_av(video_url: str, audio_url: str, dest: Path, *, reporter: Any | None = None) -> None:
    if reporter is None:
        reporter = logging.getLogger(__name__)
    _progress(reporter, "CDN mux download started")
    video_bytes, audio_bytes = await asyncio.gather(
        asyncio.to_thread(download_bytes, video_url),
        asyncio.to_thread(download_bytes, audio_url),
    )
    _progress(reporter, "CDN mux download finished")
    reporter.info(f"  muxing CDN tracks ({len(video_bytes) // 1024}KB video + {len(audio_bytes) // 1024}KB audio)")
    _progress(reporter, "muxing started")
    await mux_av(video_bytes, audio_bytes, dest)
    _progress(reporter, "muxing finished")


def _assembled_bytes(seg_capture: SegmentCapture, base: str) -> bytes:
    body = seg_capture.assemble(base)
    if body is None:
        raise RuntimeError(f"captured segment disappeared before assembly: {base}")
    return body


async def _fallback_cdn_save(
    post: ScrapedPost,
    username: str,
    ext: str,
    *,
    db: sqlite3.Connection,
    username_hint: str | None,
    media_dir: str | Path,
    profile_saved: SavedScope,
    reporter: Any,
    db_lock: asyncio.Lock | None = None,
    captured_bodies: dict[str, bytes] | None = None,
) -> None:
    reporter.info("  segments incomplete, downloading from CDN")
    _progress(reporter, "CDN fallback save started")
    await save_post(
        db,
        post.shortid,
        username,
        post.caption,
        post.post_type,
        post.media_urls,
        ext,
        skip_existing_files=False,
        captured_bodies=captured_bodies,
        media_dir=media_dir,
        profile_username=username_hint,
        profile_saved=profile_saved,
        reporter=reporter,
        db_lock=db_lock,
    )
    _progress(reporter, "CDN fallback save finished")


def _best_video_url_from_captures(
    seg_capture: SegmentCapture,
    selected_url: str,
    exclude: set[str] | None = None,
) -> str:
    """Return the best video URL from captured bases, falling back to selected_url."""
    complete = [base for base in seg_capture.get_video_bases(exclude=exclude) if seg_capture.is_complete(base)]
    ranked = _ranked_video_urls(complete)
    if ranked:
        return ranked[0]
    return selected_url


async def save_video_post_media(
    tab: TabLike,
    db: sqlite3.Connection,
    post: ScrapedPost,
    *,
    username: str,
    username_hint: str | None,
    seg_capture: SegmentCapture,
    bases_before: set[str],
    hq_video: bool = True,
    media_dir: str | Path = "site/images",
    profile_saved: SavedScope = "profile",
    hq_rate_limit_error: Callable[[HqVideoFetchError], Exception],
    resolve_captured_audio: bool = True,
    reporter: Any | None = None,
    db_lock: asyncio.Lock | None = None,
) -> None:
    if reporter is None:
        reporter = logging.getLogger(__name__)

    ext = ".mp4"
    _pkey = profile_key(username_hint, profile_saved) if username_hint else username
    dest = dest_path(post.shortid, 0, 1, ext, media_dir=media_dir, profile=_pkey)
    v_base = None
    a_base = None
    selected_url = post.media_urls[0]
    audio_url = None
    hq_tracks_selected = False
    original_media_urls = list(post.media_urls)

    if hq_video:
        _progress(reporter, "HQ metadata lookup started")
        hq_failed = False
        hq_tracks = None
        try:
            hq_tracks = await resolve_hq_video_tracks(tab, post.shortid)
        except HqVideoFetchError as exc:
            if exc.status == 429:
                raise hq_rate_limit_error(exc) from exc
            reporter.warning(f"  hq video: {exc}; saving standard quality")
            _progress(reporter, "HQ metadata lookup failed; using standard quality")
            hq_failed = True
        except ProtocolError as exc:
            reporter.warning(f"  hq video: cookie read failed ({exc}); saving standard quality")
            _progress(reporter, "HQ metadata lookup failed; using standard quality")
            hq_failed = True
        if hq_tracks:
            _progress(reporter, "HQ metadata lookup finished")
            selected_url = hq_tracks.video_url
            audio_url = hq_tracks.audio_url
            post.media_urls = [selected_url]
            hq_tracks_selected = True
            reporter.info(
                "  hq video: "
                f"{hq_tracks.width}x{hq_tracks.height} "
                f"video={hq_tracks.video_bitrate // 1000}kbps "
                f"audio={hq_tracks.audio_bitrate // 1000}kbps"
            )
        elif not hq_failed:
            reporter.warning("  hq video: no DASH manifest, saving standard quality")
            _progress(reporter, "HQ metadata lookup finished without DASH manifest")

    if audio_url is None and resolve_captured_audio:
        _progress(reporter, "audio URL lookup started")
        audio_url = await resolve_audio_url(tab, seg_capture=seg_capture, known_bases=bases_before)
        _progress(reporter, "audio URL lookup finished" if audio_url else "audio URL unavailable")

    if hq_tracks_selected and audio_url:
        try:
            await download_and_mux_av(selected_url, audio_url, dest, reporter=reporter)
        except Exception:
            reporter.info("  hq mux failed, falling back to CDN download")
            post.media_urls = original_media_urls
            await _fallback_cdn_save(
                post,
                username,
                ext,
                db=db,
                username_hint=username_hint,
                media_dir=media_dir,
                profile_saved=profile_saved,
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
            fresh_files={dest},
            media_dir=media_dir,
            profile_username=username_hint,
            profile_saved=profile_saved,
            reporter=reporter,
            db_lock=db_lock,
        )
        return

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 15.0
    while not hq_tracks_selected and loop.time() < deadline:
        v_base = captured_video_base_for_url(seg_capture, selected_url, exclude=bases_before)
        a_base = seg_capture.get_audio_base(exclude=bases_before)
        if v_base and a_base and seg_capture.is_complete(v_base) and seg_capture.is_complete(a_base):
            break
        if v_base and not a_base and seg_capture.is_complete(v_base):
            break
        # No captured video bases at all — nothing to wait for.
        if v_base is None and not seg_capture.get_video_bases():
            break
        await asyncio.sleep(0.3)

    if (
        not hq_tracks_selected
        and v_base
        and a_base
        and seg_capture.is_complete(v_base)
        and seg_capture.is_complete(a_base)
    ):
        v_bytes = _assembled_bytes(seg_capture, v_base)
        a_bytes = _assembled_bytes(seg_capture, a_base)
        reporter.info(f"  muxing ({len(v_bytes) // 1024}KB video + {len(a_bytes) // 1024}KB audio)")
        try:
            _progress(reporter, "captured segment mux started")
            await mux_av(v_bytes, a_bytes, dest)
            _progress(reporter, "captured segment mux finished")
        except Exception:
            reporter.info("  captured segment mux failed, falling back to CDN download")
            await _fallback_cdn_save(
                post,
                username,
                ext,
                db=db,
                username_hint=username_hint,
                media_dir=media_dir,
                profile_saved=profile_saved,
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
            fresh_files={dest},
            media_dir=media_dir,
            profile_username=username_hint,
            profile_saved=profile_saved,
            reporter=reporter,
            db_lock=db_lock,
        )
    elif not hq_tracks_selected and v_base and not a_base and seg_capture.is_complete(v_base):
        v_bytes = _assembled_bytes(seg_capture, v_base)
        reporter.info(f"  saving captured video ({len(v_bytes) // 1024}KB)")
        _progress(reporter, "captured video save started")
        await save_post(
            db,
            post.shortid,
            username,
            post.caption,
            post.post_type,
            post.media_urls,
            ext,
            skip_existing_files=False,
            captured_bodies={selected_url: v_bytes},
            media_dir=media_dir,
            profile_username=username_hint,
            profile_saved=profile_saved,
            reporter=reporter,
            db_lock=db_lock,
        )
        _progress(reporter, "captured video save finished")
    elif not hq_tracks_selected and audio_url:
        video_url = _best_video_url_from_captures(seg_capture, selected_url, exclude=bases_before)
        try:
            await download_and_mux_av(video_url, audio_url, dest, reporter=reporter)
        except Exception:
            reporter.info("  CDN mux failed, falling back to plain CDN download")
            await _fallback_cdn_save(
                post,
                username,
                ext,
                db=db,
                username_hint=username_hint,
                media_dir=media_dir,
                profile_saved=profile_saved,
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
            fresh_files={dest},
            media_dir=media_dir,
            profile_username=username_hint,
            profile_saved=profile_saved,
            reporter=reporter,
            db_lock=db_lock,
        )
    elif not hq_tracks_selected:
        await _fallback_cdn_save(
            post,
            username,
            ext,
            db=db,
            username_hint=username_hint,
            media_dir=media_dir,
            profile_saved=profile_saved,
            reporter=reporter,
            db_lock=db_lock,
        )
