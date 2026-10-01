from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    import shadowdriver

from tool.ig.browser import ProtocolError, human_click, human_scroll, navigate, think
from tool.ig.db import add_profile_post, clear_media, upsert_media, upsert_post
from tool.ig.http_retry import http_request
from tool.ig.media import MEDIA_DIR, dest_path, media_url_ext, profile_key
from tool.ig.types import ModalBtnPos, Pace, PostType, SavedScope, ScrapedPost, ScrapeInfo

logger = logging.getLogger(__name__)


def _progress(reporter: Any, message: str) -> None:
    progress = getattr(reporter, "progress", None)
    if progress is None:
        reporter.info(message)
    else:
        progress(message)


_JS_DIR = Path(__file__).resolve().parent / "js"


def read_js(name: str) -> str:
    return (_JS_DIR / name).read_text()


def _ts() -> str:
    return time.strftime("%H:%M:%S")


# Instagram post image CDN path component. The type and middle identifier change
# over time (for example t51.82787-15, t51.75761-15, and t39.30808-6). Avatar
# paths such as t51.*-19 are intentionally excluded.
_IG_CDN_RE = re.compile(r"/t\d+\.\d+-(?:15|6)/")

# Filter for Instagram video CDN requests (DASH segments and direct MP4s).
_IG_VIDEO_URL_RE = re.compile(r"cdninstagram\.com|fbcdn\.net")


def is_ig_image_url(url: str) -> bool:
    return bool(_IG_VIDEO_URL_RE.search(url)) and bool(_IG_CDN_RE.search(url))


def strip_byte_range(url: str) -> str:
    """Remove bytestart/byteend from DASH segment URLs to get the full-file URL."""
    p = urllib.parse.urlparse(url)
    params = dict(urllib.parse.parse_qsl(p.query))
    params.pop("bytestart", None)
    params.pop("byteend", None)
    return p._replace(query=urllib.parse.urlencode(params)).geturl()


def _is_video_track(url: str) -> bool | None:
    """True if this DASH URL is the video track, False if audio-only, None if unknown.

    Decodes the efg param (base64 JSON) and checks for 'audio' in the encode tag.
    Returns None when the URL doesn't carry enough metadata to classify (avoids
    guessing that could put audio tracks in the video candidate pool).
    """
    try:
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        efg_raw = params.get("efg", "")
        if efg_raw:
            decoded = base64.urlsafe_b64decode(efg_raw + "==").decode("utf-8", errors="ignore")
            return "audio" not in decoded.lower()
    except ValueError:
        pass
    return None


class SegmentCapture:
    """Accumulates DASH video/audio segment bytes captured from browser responses."""

    def __init__(self) -> None:
        self._segs: dict[str, dict[int, bytes]] = {}  # base_url -> {bytestart: body}
        self._totals: dict[str, int] = {}  # base_url -> total file size from Content-Range
        self._track_type: dict[str, bool | None] = {}  # base_url -> True=video, False=audio, None=unknown

    def add(self, url: str, bytestart: int, body: bytes, total: int | None) -> None:
        base = strip_byte_range(url)
        if base not in self._segs:
            track = _is_video_track(url)
            if track is None:
                return  # cannot classify; exclude from both pools
            self._segs[base] = {}
            self._track_type[base] = track
        self._segs[base][bytestart] = body
        if total is not None:
            self._totals[base] = total

    def known_bases(self) -> set[str]:
        return set(self._segs.keys())

    def get_video_bases(self, exclude: set[str] | None = None) -> list[str]:
        return [base for base, is_v in self._track_type.items() if is_v and (exclude is None or base not in exclude)]

    def get_audio_base(self, exclude: set[str] | None = None) -> str | None:
        for base, is_v in self._track_type.items():
            if not is_v and (exclude is None or base not in exclude):
                return base
        return None

    def is_complete(self, base: str) -> bool:
        segs = self._segs.get(base, {})
        total = self._totals.get(base, 0)
        return bool(segs) and bool(total) and sum(len(b) for b in segs.values()) >= total

    def assemble(self, base: str) -> bytes | None:
        segs = self._segs.get(base)
        if not segs:
            return None
        return b"".join(body for _, body in sorted(segs.items()))


async def mux_av(video_bytes: bytes, audio_bytes: bytes, dest: Path) -> None:
    """Mux separate DASH video and audio tracks into one MP4 via ffmpeg."""
    import subprocess

    def _do() -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=f".{dest.name}.",
            suffix=dest.suffix,
            dir=dest.parent,
            delete=False,
        ) as tmp_out:
            tmp_out_path = Path(tmp_out.name)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                v = Path(tmp) / "v.mp4"
                a = Path(tmp) / "a.mp4"
                v.write_bytes(video_bytes)
                a.write_bytes(audio_bytes)
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-i", str(v), "-i", str(a), "-c", "copy", str(tmp_out_path)],
                    check=True,
                    timeout=120,
                )
                tmp_out_path.replace(dest)
        except Exception:
            tmp_out_path.unlink(missing_ok=True)
            raise

    await asyncio.to_thread(_do)


def extract_shortid(url: str) -> str:
    m = re.search(r"/(?:p|reel)/([A-Za-z0-9_-]+)", url)
    if m:
        group = m.group(1)
        assert isinstance(group, str)
        return group
    if re.match(r"^[A-Za-z0-9_-]+$", url):
        return url
    raise ValueError(f"cannot extract shortid from: {url}")


def raise_if_login(href: str) -> None:
    if "/accounts/login" in href:
        raise RuntimeError("Redirected to login; run `uv run ig login` first")


_SCRAPE_JS = read_js("scrape.js")

_SIGNAL_JS = read_js("signal.js")

# Extracts full CDN URLs for all carousel slides from the server-rendered inline
# script that carries the Relay/GraphQL post data (PolarisPostRootQuery).
# Instagram embeds all carousel slide candidates in this script on page load —
# even when slides are served from browser cache and never fire network events.
# The related-media grid script is excluded by keying on 'PolarisPostRootQuery'.
# URL format in the raw script text uses JSON \/  escaping; replace \/ with /.
_SCRIPT_SLIDE_URLS_JS = read_js("script_slide_urls.js")

_SCRIPT_VIDEO_URLS_JS = read_js("script_video_urls.js")

_PERFORMANCE_VIDEO_URLS_JS = read_js("performance_video_urls.js")

_MODAL_BTN_DOC_POS_JS = read_js("modal_btn_doc_pos.js")

# Returns the best-resolution CDN URLs for the currently visible post image.
# Carousel <ul>s may contain low-res placeholders for offscreen slides, so hidden
# slides are intentionally ignored and collected after clicking Next.
_MODAL_IMGS_JS = read_js("modal_imgs.js")


def _downloadable_video_urls(urls: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for url in urls or []:
        if not url or url.startswith("blob:") or ".mp4" not in url:
            continue
        url = strip_byte_range(url)
        if url not in seen:
            seen.add(url)
            cleaned.append(url)
    return cleaned


def _video_metadata(url: str) -> dict[str, Any]:
    try:
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        efg_raw = params.get("efg", "")
        if efg_raw:
            raw = urllib.parse.unquote(efg_raw)
            return json.loads(base64.urlsafe_b64decode(raw + "==").decode("utf-8", errors="ignore"))
    except ValueError:
        pass
    return {}


def _video_quality_hint(url: str) -> int:
    """Return the strongest resolution hint encoded in an Instagram video URL.

    Instagram's ``efg.vencode_tag`` usually carries either a height suffix
    (``..._1080p``) or a width marker (``...C3.988...``).  The latter is what
    appears on the lower-resolution H.264 stream for some posts.  A quality
    hint is deliberately conservative: unknown URLs return zero and continue
    to be ranked by duration/bitrate.
    """
    meta = _video_metadata(url)
    tag = str(meta.get("vencode_tag") or "").lower()
    values: list[int] = []
    for key in ("width", "height"):
        try:
            value = int(float(meta.get(key) or 0))
            if value > 0:
                values.append(value)
        except TypeError, ValueError:
            pass
    values.extend(int(value) for value in re.findall(r"(?:^|[._-])(?:c\d+\.)?(\d{3,4})p(?:$|[._-])", tag))
    values.extend(int(value) for value in re.findall(r"(?:^|[._-])c\d+\.(\d{3,4})(?:$|[._-])", tag))
    if not values and "dash_baseline" in tag:
        # Baseline tags omit dimensions altogether.  Treat them as a low
        # quality hint so profile downloads get one chance to resolve DASH.
        return 1
    return max(values, default=0)


def _video_url_score(url: str) -> tuple[int, int, int, int, int, int]:
    meta = _video_metadata(url)
    tag = str(meta.get("vencode_tag") or "").lower()
    if "audio" in tag:
        return (-1, 0, 0, 0, 0, 0)
    duration = int(float(meta.get("duration_s") or 0))
    bitrate = int(float(meta.get("bitrate") or 0))
    quality = _video_quality_hint(url)
    # Prefer a modern high-resolution encode over a lower-resolution
    # progressive preview even when the preview's nominal bitrate is higher.
    # VP9/AV1 are also preferred at equal resolution because Instagram often
    # gives them better quality per bit than the H.264 fallback.
    codec_quality = 2 if "vp9" in tag or "av1" in tag else 1
    container_quality = 0
    if "progressive" in tag:
        container_quality += 1
    if "dash_baseline" in tag:
        container_quality += 1
    return (1, duration, quality, codec_quality, bitrate, container_quality)


def _video_selection_reason(url: str) -> str:
    meta = _video_metadata(url)
    tag = str(meta.get("vencode_tag") or "unknown")
    duration = meta.get("duration_s", "?")
    bitrate = meta.get("bitrate", "?")
    return f"duration={duration}s bitrate={bitrate} tag={tag}"


def _audio_url_score(url: str) -> tuple[int, int, int]:
    meta = _video_metadata(url)
    tag = str(meta.get("vencode_tag") or "").lower()
    if "audio" not in tag:
        return (-1, 0, 0)
    duration = int(float(meta.get("duration_s") or 0))
    bitrate = int(float(meta.get("bitrate") or 0))
    return (1, duration, bitrate)


def _best_video_url(urls: list[str]) -> str | None:
    urls = _ranked_video_urls(urls)
    if not urls:
        return None
    return urls[0]


def _ranked_video_urls(urls: list[str]) -> list[str]:
    return sorted(
        [url for url in _downloadable_video_urls(urls) if _video_url_score(url)[0] > 0],
        key=_video_url_score,
        reverse=True,
    )


def _best_audio_url(urls: list[str]) -> str | None:
    urls = _downloadable_video_urls(urls)
    audio_urls = [url for url in urls if _audio_url_score(url)[0] > 0]
    if not audio_urls:
        return None
    return max(audio_urls, key=_audio_url_score)


def _add_video_candidates(candidates: list[str], seen: set[str], urls: list[str] | None) -> None:
    for url in _downloadable_video_urls(urls):
        if url in seen:
            continue
        seen.add(url)
        candidates.append(url)


def _media_filename(url: str) -> str:
    return url.split("?")[0].split("/")[-1]


def _image_quality_score(url: str) -> int:
    # Search the URL path and stp= parameter for p/s dimension markers.
    # Other query parameter values (oh=, _nc_oc=, etc.) are hash tokens that
    # can coincidentally contain pNNNxNNN patterns — exclude them.
    path = url.split("?", 1)[0]
    stp_val = ""
    stp_m = re.search(r"[?&]stp=([^&]*)", url)
    if stp_m:
        stp_val = stp_m.group(1)
    search_text = f"{path}?{stp_val}"
    matches = re.findall(r"[ps](\d{2,5})x(\d{2,5})", search_text)
    if matches:
        return max(int(w) * int(h) for w, h in matches)
    crop_matches = re.findall(r"[?&]stp=[^&]*c\d+(?:\.\d+)?\.\d+(?:\.\d+)?\.(\d{2,5})\.(\d{2,5})a(?:_|%5F)", url)
    if crop_matches:
        return max(int(w) * int(h) for w, h in crop_matches)
    if re.search(r"[?&]stp=(?:cp\d+_)?dst-(?:jpg|jpegr)(?:_e\d+)?(?:_tt\d+)?(?:&|$)", url):
        return 10_000_000
    return 0


def _best_image_variant(url: str, captured_urls: list[str] | None) -> str:
    if not captured_urls:
        return url
    name = _media_filename(url)
    candidates = [url]
    candidates.extend(u for u in captured_urls if _media_filename(u) == name and is_ig_image_url(u))
    return max(candidates, key=_image_quality_score)


def _image_quality_label(url: str) -> str:
    if not is_ig_image_url(url):
        return "external"
    path = url.split("?", 1)[0]
    stp_val = ""
    stp_m = re.search(r"[?&]stp=([^&]*)", url)
    if stp_m:
        stp_val = stp_m.group(1)
    search_text = f"{path}?{stp_val}"
    matches = re.findall(r"([ps]\d{2,5}x\d{2,5})", search_text)
    if matches:
        return max(matches, key=lambda m: int(m[1:].split("x", 1)[0]) * int(m.split("x", 1)[1]))
    crop_matches = re.findall(r"[?&]stp=[^&]*c\d+(?:\.\d+)?\.\d+(?:\.\d+)?\.(\d{2,5})\.(\d{2,5})a(?:_|%5F)", url)
    if crop_matches:
        w, h = max(crop_matches, key=lambda item: int(item[0]) * int(item[1]))
        return f"c{w}x{h}"
    if _image_quality_score(url) >= 1_000_000:
        return "unconstrained"
    return "unknown"


def _is_low_quality_image_url(url: str) -> bool:
    if not is_ig_image_url(url):
        return False
    score = _image_quality_score(url)
    return 0 < score < 1_000_000


def _classify_scraped_post(
    declared_type: str,
    script_urls: list[str],
    video_urls: list[str],
    has_video_element: bool,
    has_carousel_button: bool,
) -> tuple[PostType, bool]:
    """Determine final post type and whether it contains video from scrape signals.

    Reclassifies declared "image" posts as "video" when video tracks or DOM
    video elements are present. Treats posts with multiple script slides or a
    carousel Next button as carousels. Mixed video+image carousels are detected
    as carousels with video.

    Returns (final_post_type, has_video) — has_video is True when video tracks
    or a video element exist alongside image/carousel evidence.
    """
    is_carousel = declared_type == "carousel" or len(script_urls) > 1 or has_carousel_button

    has_video = bool(video_urls or has_video_element)

    if declared_type == "video":
        return ("video", False)

    if not is_carousel and has_video:
        return ("video", False)

    if is_carousel:
        return ("carousel", has_video)

    return ("image", False)


def _validate_media_urls(shortid: str, post_type: PostType, media_urls: list[str]) -> None:
    if not media_urls:
        raise ValueError(f"{shortid}: scrape produced no media URLs")
    if post_type == "video" and len(media_urls) != 1:
        raise ValueError(f"{shortid}: video scrape produced {len(media_urls)} media URLs; expected exactly 1")
    if post_type in {"image", "carousel"}:
        low = [(idx, url) for idx, url in enumerate(media_urls) if _is_low_quality_image_url(url)]
        if low:
            details = ", ".join(f"#{idx + 1} {_image_quality_label(url)}" for idx, url in low[:5])
            raise ValueError(f"{shortid}: low-quality Instagram image URL(s) selected: {details}")


def _has_better_image_variant(url: str, captured_urls: list[str] | None) -> bool:
    return _image_quality_score(_best_image_variant(url, captured_urls)) > _image_quality_score(url)


async def _wait_for_best_image_variant(url: str, captured_urls: list[str] | None, *, timeout: float = 1.5) -> str:
    best = _best_image_variant(url, captured_urls)
    if not captured_urls or _image_quality_score(best) >= 1_000_000:
        return best
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if _has_better_image_variant(best, captured_urls):
            best = _best_image_variant(best, captured_urls)
            if _image_quality_score(best) >= 1_000_000:
                break
        await asyncio.sleep(0.1)
    return best


async def _resolve_video_urls(
    tab,
    media_urls: list[str] | None,
    *,
    seg_capture: SegmentCapture | None = None,
    known_bases: set[str] | None = None,
    select_best: bool = True,
    reporter: Any | None = None,
) -> list[str]:
    if reporter is None:
        reporter = logger
    urls: list[str] = []
    seen: set[str] = set()
    _add_video_candidates(urls, seen, media_urls)
    _add_video_candidates(urls, seen, await tab.evaluate(_SCRIPT_VIDEO_URLS_JS) or [])
    if seg_capture is not None:
        _add_video_candidates(urls, seen, seg_capture.get_video_bases(exclude=known_bases))
    if not urls and seg_capture is not None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while loop.time() < deadline:
            _add_video_candidates(urls, seen, seg_capture.get_video_bases(exclude=known_bases))
            if urls:
                break
            await asyncio.sleep(0.2)
    _add_video_candidates(urls, seen, await tab.evaluate(_PERFORMANCE_VIDEO_URLS_JS) or [])
    ranked = _ranked_video_urls(urls)
    if not ranked:
        return []
    if select_best:
        best = ranked[0]
        if len(urls) > 1:
            reporter.info(f"  video: selected 1/{len(urls)} candidates ({_video_selection_reason(best)})")
        return [best]
    return ranked


async def resolve_audio_url(
    tab,
    *,
    seg_capture: SegmentCapture | None = None,
    known_bases: set[str] | None = None,
) -> str | None:
    urls: list[str] = []
    seen: set[str] = set()
    _add_video_candidates(urls, seen, await tab.evaluate(_SCRIPT_VIDEO_URLS_JS) or [])
    if seg_capture is not None:
        audio_base = seg_capture.get_audio_base(exclude=known_bases)
        if audio_base is not None:
            _add_video_candidates(urls, seen, [audio_base])
    _add_video_candidates(urls, seen, await tab.evaluate(_PERFORMANCE_VIDEO_URLS_JS) or [])
    return _best_audio_url(urls)


async def scrape_direct(
    tab: shadowdriver.Tab,
    shortid: str,
    *,
    pace: Pace | None = None,
    seg_capture: SegmentCapture | None = None,
    known_bases: set[str] | None = None,
    reporter: Any | None = None,
) -> ScrapedPost | None:
    """Scrape a post from a directly-navigated /p/<shortid>/ page.

    Uses _SIGNAL_JS (JSON-LD / carousel button / og:image) to detect readiness
    instead of _modal_ready_js, because the timestamp link checked by the modal
    ready test may not be present or may use a different path on direct pages.
    Falls back to _collect_modal_images for images/carousels since the article
    DOM structure is identical to the SPA modal.
    """
    if pace is None:
        pace = Pace.fast()
    if reporter is None:
        reporter = logger

    # Wait for page signal (JSON-LD, carousel button, or og:image).
    loop = asyncio.get_running_loop()
    end = loop.time() + 20.0
    signal = None
    while loop.time() < end:
        href_raw = await tab.evaluate("window.location.href")
        href = href_raw if isinstance(href_raw, str) else ""
        if href.startswith("chrome-error://"):
            err_raw = await tab.evaluate("document.body ? document.body.innerText : ''")
            err = err_raw if isinstance(err_raw, str) else ""
            code_m = re.search(r"HTTP ERROR (\d+)", err)
            code = int(code_m.group(1)) if code_m else 0
            reporter.error(f"  {_ts()} {shortid}: HTTP {code} error")
            return None
        signal = await tab.evaluate(_SIGNAL_JS)
        if signal:
            break
        await asyncio.sleep(0.5)

    if not signal:
        reporter.error(f"  {_ts()} {shortid}: page did not signal ready")
        return None

    await think(min_s=pace.modal_think[0], max_s=pace.modal_think[1])

    info_raw = await tab.evaluate(_SCRAPE_JS)
    info: ScrapeInfo | None = cast("ScrapeInfo", info_raw) if isinstance(info_raw, dict) else None
    if not info:
        reporter.error(f"  {_ts()} {shortid}: _SCRAPE_JS returned no data")
        return None

    declared_type: str = info.get("type", "image")
    script_urls_raw = await tab.evaluate(_SCRIPT_SLIDE_URLS_JS)
    script_urls: list[str] = (
        [url for url in script_urls_raw if isinstance(url, str)] if isinstance(script_urls_raw, list) else []
    )
    script_video_urls_raw = await tab.evaluate(_SCRIPT_VIDEO_URLS_JS)
    script_video_urls: list[str] = _downloadable_video_urls(
        [url for url in script_video_urls_raw if isinstance(url, str)]
        if isinstance(script_video_urls_raw, list)
        else []
    )
    has_video_element: bool = bool(await tab.evaluate('!!document.querySelector("article video, main video")'))
    has_carousel_button: bool = bool(
        await tab.evaluate("!!document.querySelector('article button[aria-label=\"Next\"]')")
    )

    post_type, has_video = _classify_scraped_post(
        declared_type, script_urls, script_video_urls, has_video_element, has_carousel_button
    )

    if post_type == "video":
        media_urls = await _resolve_video_urls(
            tab,
            info.get("mediaUrls") or [],
            seg_capture=seg_capture,
            known_bases=known_bases,
            reporter=reporter,
        )
        if not media_urls:
            reporter.error(f"  {_ts()} {shortid}: video URL not captured")
            return None
    elif has_video:
        video_media_urls = await _resolve_video_urls(
            tab,
            script_video_urls,
            seg_capture=seg_capture,
            known_bases=known_bases,
            select_best=False,
            reporter=reporter,
        )
        if not video_media_urls:
            reporter.error(f"  {_ts()} {shortid}: carousel video URL not captured")
            return None
        if script_urls:
            media_urls = [*video_media_urls, *script_urls]
        else:
            image_urls = await _collect_modal_images(tab, shortid, is_carousel=True, pace=pace)
            media_urls = [*video_media_urls, *image_urls]
        if not media_urls:
            reporter.error(f"  {_ts()} {shortid}: no carousel media found")
            return None
    else:
        if script_urls:
            media_urls = script_urls
        else:
            media_urls = await _collect_modal_images(tab, shortid, is_carousel=post_type == "carousel", pace=pace)
        if not media_urls:
            reporter.error(f"  {_ts()} {shortid}: no images found")
            return None

    return ScrapedPost(
        shortid=shortid,
        post_type=post_type,
        media_urls=media_urls,
        username=info.get("username"),
        caption=info.get("caption"),
    )


async def _collect_modal_images(
    tab: shadowdriver.Tab,
    shortid: str,
    *,
    is_carousel: bool,
    pace: Pace | None = None,
    captured_image_urls: list[str] | None = None,
) -> list[str]:
    """Read image URLs from the modal DOM, clicking through carousel slides as needed."""
    if pace is None:
        pace = Pace.fast()
    loop = asyncio.get_running_loop()
    by_name: dict[str, str] = {}
    ordered_names: list[str] = []
    ordered: list[str] = []

    async def _absorb(urls_raw: object) -> bool:
        urls: list[str] = cast("list[str]", urls_raw) if isinstance(urls_raw, list) else []
        changed = False
        for u in urls:
            if not u:
                continue
            u = await _wait_for_best_image_variant(u, captured_image_urls)
            name = _media_filename(u)
            old = by_name.get(name)
            if old is None:
                by_name[name] = u
                ordered_names.append(name)
                ordered.append(u)
                changed = True
            elif _image_quality_score(u) > _image_quality_score(old):
                by_name[name] = u
                ordered[ordered_names.index(name)] = u
                changed = True
        return changed

    await _absorb(await tab.evaluate(_MODAL_IMGS_JS))

    if not is_carousel:
        return ordered

    img_index_js = "parseInt(new URLSearchParams(window.location.search).get('img_index') || '1', 10)"
    has_next_js = "!!document.querySelector('article button[aria-label=\"Next\"]')"
    stagnant_clicks = 0

    for _ in range(20):
        if not await tab.evaluate(has_next_js):
            break

        prev_idx = await tab.evaluate(img_index_js)
        doc_pos_raw = await tab.evaluate(_MODAL_BTN_DOC_POS_JS)
        doc_pos: ModalBtnPos | None = cast("ModalBtnPos", doc_pos_raw) if isinstance(doc_pos_raw, dict) else None
        if not doc_pos:
            break

        doc_x = float(doc_pos["docX"])
        doc_y = float(doc_pos["docY"])
        view_h = float(doc_pos["viewH"])
        await human_scroll(tab, max(0.0, doc_y - view_h / 2))
        try:
            await human_click(tab, doc_x, doc_y - max(0.0, doc_y - view_h / 2))
        except ProtocolError as exc:
            if "out of bounds" not in str(exc):
                raise
            await tab.evaluate("document.querySelector('article button[aria-label=\"Next\"]').click()")

        # The profile modal can carry a stale img_index from the previous
        # carousel after ArrowRight navigation. Treat it as a weak signal and
        # prefer newly rendered images or disappearance of the article Next button.
        added = False
        has_next_after = True
        idx_after = prev_idx
        deadline = loop.time() + 3.0
        while loop.time() < deadline:
            added = await _absorb(await tab.evaluate(_MODAL_IMGS_JS))
            has_next_after = bool(await tab.evaluate(has_next_js))
            idx_after = await tab.evaluate(img_index_js)
            if added or not has_next_after or idx_after != prev_idx:
                break
            await asyncio.sleep(0.1)

        await asyncio.sleep(pace.slide_render)
        added = await _absorb(await tab.evaluate(_MODAL_IMGS_JS)) or added
        if not added and has_next_after and idx_after == prev_idx:
            stagnant_clicks += 1
            if stagnant_clicks >= 2:
                break
            continue
        stagnant_clicks = 0

    return ordered


async def navigate_to(tab, url: str, timeout: float = 15.0) -> None:
    """Navigate, ignoring timeouts (page load continues in background)."""
    await navigate(tab, url, timeout=timeout)


async def wait_ready(tab, timeout: float = 20.0) -> None:
    """Poll until document.readyState == 'complete'."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if await tab.evaluate("document.readyState") == "complete":
            return
        await asyncio.sleep(0.3)


def download_bytes(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            # Chrome UA used intentionally: Instagram CDN sometimes serves
            # degraded assets or triggers additional challenges for
            # non-Chrome UAs on direct urllib fetches.
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.instagram.com/",
        },
    )
    return http_request(req)


def _write_bytes_atomic(dest: Path, body: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(body)
        tmp_path.replace(dest)
    except Exception:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise


def _download(url: str, dest: Path) -> None:
    _write_bytes_atomic(dest, download_bytes(url))


def _pending_path(dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=f".{dest.name}.", suffix=".pending", dir=dest.parent, delete=False) as tmp:
        pending = Path(tmp.name)
    pending.unlink(missing_ok=True)
    return pending


def _validate_downloaded_file(path: Path, dest: Path) -> None:
    """Check file headers so corrupt responses (HTML error pages, redirects)
    aren't permanently treated as valid media by is_done()."""
    suffix = dest.suffix.lower()
    if suffix == ".jpg":
        header = path.read_bytes()[:12]
        # JPEG magic: \xFF\xD8\xFF
        if header[:2] == b"\xff\xd8":
            return
        # WebP magic: RIFF....WEBP (Instagram may serve WebP through .jpg URLs)
        if header[:4] == b"RIFF" and len(header) >= 12 and header[8:12] == b"WEBP":
            return
        raise ValueError(f"{dest.name}: does not look like a JPEG (starts with {header[:4]!r})")
    if suffix == ".mp4":
        # ISO BMFF / MP4: 'ftyp' at offset 4
        header = path.read_bytes()[:12]
        if len(header) < 12 or header[4:8] != b"ftyp":
            raise ValueError(f"{dest.name}: does not look like an MP4 (starts with {header[:12]!r})")


def _commit_post_metadata(
    db: sqlite3.Connection,
    shortid: str,
    username: str | None,
    caption: str | None,
    post_type: PostType,
    media_entries: list[tuple[int, str]],
    *,
    profile_username: str | None = None,
    profile_saved: SavedScope = "profile",
) -> None:
    """Commit post + media rows to the database in a single transaction.

    Called after downloads succeed and before files are promoted to their
    final paths.  Committing first means a crash between commit and file
    promotion leaves the DB consistent — is_done() sees missing files and
    the post will be retried on the next run.
    """
    total = len(media_entries)
    upsert_post(db, shortid, username, caption, post_type)
    clear_media(db, shortid, total)
    for idx, url in media_entries:
        upsert_media(db, shortid, idx, url)
    if profile_username:
        add_profile_post(db, profile_username, profile_saved, shortid)
    db.commit()
    # Checkpoint WAL so instagram.db mtime updates (for `make site` dependency tracking).
    db.execute("PRAGMA wal_checkpoint(PASSIVE)")


async def save_post(
    db: sqlite3.Connection,
    shortid: str,
    username: str | None,
    caption: str | None,
    post_type: PostType,
    media_urls: list[str],
    ext: str,
    *,
    skip_existing_files: bool = True,
    fresh_files: set[Path] | None = None,
    captured_bodies: dict[str, bytes] | None = None,
    browser_fetch: Callable[[str], Awaitable[bytes]] | None = None,
    media_dir: str | Path = MEDIA_DIR,
    profile_username: str | None = None,
    profile_saved: SavedScope = "profile",
    reporter: Any | None = None,
    db_lock: asyncio.Lock | None = None,
) -> None:
    """Upsert post + media rows and download files (carousel files in parallel).

    Downloads are staged to temp paths, then the DB transaction commits,
    and only then are files promoted to their final destinations.  This
    ordering means a crash between commit and promotion is safe — is_done()
    sees the files are missing and the post is retried on the next run.

    ``fresh_files`` identifies destinations produced by an earlier step in
    the same save operation (for example, a muxed video).  They are retained
    without a second download, but are reported as newly created rather than
    misleadingly labelled ``cached``.
    """
    _validate_media_urls(shortid, post_type, media_urls)

    if reporter is None:
        reporter = logger

    total = len(media_urls)
    fresh_files = fresh_files or set()
    _progress(reporter, f"download phase started ({total} file{'s' if total != 1 else ''})")

    async def _dl_one(idx: int, url: str) -> tuple[int, str, Path | None, Path, Exception | None]:
        item_ext = media_url_ext(post_type, url) if post_type == "carousel" else ext
        if profile_username:
            pkey = profile_key(profile_username, profile_saved)
        elif username:
            pkey = username
        else:
            pkey = None
        dest = dest_path(shortid, idx, total, item_ext, media_dir=media_dir, profile=pkey)
        label = f"[{idx + 1}/{total}]"
        if skip_existing_files and dest.exists():
            if dest in fresh_files:
                reporter.info(f"  {label} {dest} (created)")
                _progress(reporter, f"download kept created file {idx + 1}/{total}")
            else:
                reporter.info(f"  {label} {dest} (cached)")
                _progress(reporter, f"download skipped cached file {idx + 1}/{total}")
            return idx, url, None, dest, None
        pending = _pending_path(dest)
        try:
            if captured_bodies and url in captured_bodies:
                _write_bytes_atomic(pending, captured_bodies[url])
            elif browser_fetch is not None:
                try:
                    reporter.info(f"  {label} browser fetch...")
                    body = await browser_fetch(url)
                    _write_bytes_atomic(pending, body)
                except Exception as exc:
                    reporter.debug(f"  {label} browser fetch failed ({type(exc).__name__}): {exc}")
                    reporter.info(f"  {label} browser fetch unavailable, downloading...")
                    await asyncio.to_thread(_download, url, pending)
            else:
                reporter.info(f"  {label} downloading...")
                await asyncio.to_thread(_download, url, pending)
        except Exception as exc:
            pending.unlink(missing_ok=True)
            return idx, url, None, dest, exc
        try:
            _validate_downloaded_file(pending, dest)
        except Exception as exc:
            pending.unlink(missing_ok=True)
            return idx, url, None, dest, exc
        _progress(reporter, f"download finished file {idx + 1}/{total}")
        return idx, url, pending, dest, None

    coros = [_dl_one(idx, url) for idx, url in enumerate(media_urls)]
    raw_results = await asyncio.gather(*coros, return_exceptions=True)

    results: list[tuple[int, str, Path | None, Path, Exception | None]] = []
    errors: list[BaseException] = []
    for raw in raw_results:
        if isinstance(raw, BaseException):
            errors.append(raw)
        else:
            idx, _url, pending, dest, error = raw
            results.append(raw)
            if error is not None:
                errors.append(error)

    if errors:
        for _idx, _url, pending, _dest, _error in results:
            if pending is not None:
                pending.unlink(missing_ok=True)
        detail = "; ".join(str(e) for e in errors)
        raise RuntimeError(f"{shortid}: {len(errors)} download(s) failed: {detail}") from errors[0]

    # Phase 2: commit DB metadata before files are visible on disk.
    if db_lock is None:
        _progress(reporter, "database write started")
        _commit_post_metadata(
            db,
            shortid,
            username,
            caption,
            post_type,
            [(idx, url) for idx, url, _pending, _dest, _error in results],
            profile_username=profile_username,
            profile_saved=profile_saved,
        )
        _progress(reporter, "database write finished")
    else:
        _progress(reporter, "database write waiting")
        async with db_lock:
            _progress(reporter, "database write started")
            _commit_post_metadata(
                db,
                shortid,
                username,
                caption,
                post_type,
                [(idx, url) for idx, url, _pending, _dest, _error in results],
                profile_username=profile_username,
                profile_saved=profile_saved,
            )
        _progress(reporter, "database write finished")

    # Phase 3: promote staged files to final destinations.
    _progress(reporter, "file promotion started")
    for idx, _url, pending, dest, _error in results:
        if pending is not None:
            pending.replace(dest)
            reporter.info(f"  [{idx + 1}/{total}] {dest}")
    _progress(reporter, "file promotion finished")
