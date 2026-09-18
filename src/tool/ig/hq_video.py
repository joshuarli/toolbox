from __future__ import annotations

import asyncio
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree

from tool.ig.browser_types import TabLike
from tool.ig.errors import InstagramHttpError
from tool.ig.http_retry import http_request_json

INSTAGRAM_APP_ID = "936619743392459"
INSTAGRAM_WEB_ASBD_ID = "359341"  # modern web app value used by the current Instagram web client
INSTAGRAM_CHROMIUM_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
INSTAGRAM_WEB_USER_AGENT = INSTAGRAM_CHROMIUM_USER_AGENT
INSTAGRAM_MEDIA_API_USER_AGENT = INSTAGRAM_CHROMIUM_USER_AGENT


def instagram_api_user_agent() -> str:
    """Return the API UA matching the driven Chromium browser."""
    return INSTAGRAM_CHROMIUM_USER_AGENT


# Base-64 alphabet for Instagram media ids (shortcode <-> numeric pk).
_ENCODING_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def shortcode_to_media_id(shortcode: str) -> str:
    """Convert an Instagram shortcode to its numeric media id (base-64 decode).

    Same algorithm as yt-dlp's `_id_to_pk`. Long pseudo-shortcodes (graph-API
    media ids embedded in URLs) carry a 28-char suffix that is dropped first.
    """
    if len(shortcode) > 28:
        shortcode = shortcode[:-28]
    values = {char: index for index, char in enumerate(_ENCODING_CHARS)}
    result = 0
    base = len(_ENCODING_CHARS)
    for char in shortcode:
        if char not in values:
            raise HqVideoFetchError(f"invalid shortcode for media id lookup: {shortcode!r}")
        result = result * base + values[char]
    return str(result)


@dataclass(frozen=True)
class HqVideoTracks:
    video_url: str
    audio_url: str
    width: int
    height: int
    video_bitrate: int
    audio_bitrate: int


class HqVideoFetchError(InstagramHttpError):
    pass


def _local_name(el: ElementTree.Element) -> str:
    return str(el.tag).rsplit("}", 1)[-1]


def _child_text(el: ElementTree.Element, name: str) -> str | None:
    for child in el:
        if _local_name(child) == name and child.text:
            return child.text.strip()
    return None


def _int_attr(el: ElementTree.Element, name: str) -> int:
    try:
        return int(float(el.attrib.get(name) or 0))
    except TypeError, ValueError:
        return 0


def _parse_hq_video_tracks(dash_manifest: str) -> HqVideoTracks | None:
    try:
        root = ElementTree.fromstring(dash_manifest)
    except ElementTree.ParseError:
        return None

    videos: list[dict[str, Any]] = []
    audios: list[dict[str, Any]] = []
    for adaptation in root.iter():
        if _local_name(adaptation) != "AdaptationSet":
            continue
        content_type = str(adaptation.attrib.get("contentType") or "").lower()
        mime_type = str(adaptation.attrib.get("mimeType") or "").lower()
        adaptation_codecs = str(adaptation.attrib.get("codecs") or "")
        adaptation_base = _child_text(adaptation, "BaseURL")
        for rep in adaptation:
            if _local_name(rep) != "Representation":
                continue
            url = _child_text(rep, "BaseURL") or adaptation_base
            if not url or not str(url).startswith(("http://", "https://")):
                continue
            codecs = str(rep.attrib.get("codecs") or adaptation_codecs)
            width = _int_attr(rep, "width") or _int_attr(adaptation, "width")
            height = _int_attr(rep, "height") or _int_attr(adaptation, "height")
            bandwidth = _int_attr(rep, "bandwidth")
            fmt = {"url": url, "width": width, "height": height, "bandwidth": bandwidth, "codecs": codecs}
            is_audio = content_type == "audio" or mime_type.startswith("audio/") or codecs.startswith("mp4a")
            is_video = content_type == "video" or mime_type.startswith("video/") or width or height
            if is_audio:
                audios.append(fmt)
            elif is_video:
                videos.append(fmt)

    if not videos or not audios:
        return None
    video = max(videos, key=lambda f: (f["width"] * f["height"], f["height"], f["bandwidth"]))
    audio = max(audios, key=lambda f: f["bandwidth"])
    return HqVideoTracks(
        video_url=video["url"],
        audio_url=audio["url"],
        width=video["width"],
        height=video["height"],
        video_bitrate=video["bandwidth"],
        audio_bitrate=audio["bandwidth"],
    )


def _download_hq_media_json(cookies: dict[str, str], shortid: str) -> dict[str, Any]:
    """Fetch HQ media metadata from Instagram's private web API.

    The legacy `graphql/query/?doc_id=...&variables={shortcode}` shortcode query
    (`xdt_shortcode_media`) is rejected by Instagram with HTTP 200 +
    {"data": null, "errors": [execution error]}.  The current authenticated path
    (also used by yt-dlp) is the web v1 API, keyed by the post's numeric media
    id, which returns `video_dash_manifest` and `video_versions`.
    """
    cookie_header = "; ".join(f"{name}={value}" for name, value in sorted(cookies.items()) if value)
    if "sessionid=" not in cookie_header or "csrftoken=" not in cookie_header:
        raise HqVideoFetchError("HQ video metadata requires authenticated browser cookies")

    media_id = shortcode_to_media_id(shortid)
    url = f"https://www.instagram.com/api/v1/media/{media_id}/info/"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "*/*",
            "Cookie": cookie_header,
            "Origin": "https://www.instagram.com",
            "Referer": f"https://www.instagram.com/p/{shortid}/",
            "User-Agent": instagram_api_user_agent(),
            "X-ASBD-ID": INSTAGRAM_WEB_ASBD_ID,
            "X-CSRFToken": cookies["csrftoken"],
            "X-IG-App-ID": INSTAGRAM_APP_ID,
            "X-IG-WWW-Claim": "0",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    try:
        return http_request_json(req)
    except urllib.error.HTTPError as exc:
        raise HqVideoFetchError(
            f"HQ video metadata HTTP {exc.code}",
            status=exc.code,
            retry_after=parse_retry_after(exc.headers.get("retry-after")),
            url=url,
        ) from exc
    except urllib.error.URLError as exc:
        raise HqVideoFetchError(f"HQ video metadata request failed: {exc.reason}", url=url) from exc


async def resolve_hq_video_tracks(tab: TabLike, shortid: str) -> HqVideoTracks | None:
    cookies = await instagram_cookie_values(tab)
    if not cookies.get("sessionid") or not cookies.get("csrftoken"):
        raise HqVideoFetchError("HQ video metadata requires authenticated browser cookies")

    data = await asyncio.to_thread(_download_hq_media_json, cookies, shortid)
    items = data.get("items")
    media = items[0] if isinstance(items, list) and items else None
    manifest = media.get("video_dash_manifest") if isinstance(media, dict) else None
    if not manifest:
        return None
    return _parse_hq_video_tracks(str(manifest))


def cookie_string_value(value) -> str:
    if isinstance(value, dict) and value.get("type") == "string":
        return str(value.get("value") or "")
    return str(value or "")


async def instagram_cookie_values(tab: TabLike) -> dict[str, str]:
    result = await tab.get_cookies()
    values: dict[str, str] = {}
    for cookie in result.get("cookies") or []:
        domain = str(cookie.get("domain") or "")
        if "instagram.com" not in domain:
            continue
        name = str(cookie.get("name") or "")
        if name:
            values[name] = cookie_string_value(cookie.get("value"))
    return values


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except TypeError, ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
    except TypeError, ValueError, IndexError, OverflowError:
        return None
    return max(0.0, dt.timestamp() - time.time())
