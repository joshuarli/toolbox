"""Profile timeline metadata through Instagram's authenticated web v1 API.

The previous approach captured the app's `PolarisProfilePostsQuery` request body
and replayed `/graphql/query` changing only the pagination cursor.  Instagram
retires those GraphQL queries; the current path is the authenticated web v1
`feed/user/<username>/username/` endpoint.  Resolving the ID first through
`web_profile_info` is deliberately avoided because Instagram now rate-limits
that endpoint even when the username feed is available.  The feed returns the
same per-item JSON shape (`code`, `media_type`, `caption`, `image_versions2`,
`video_versions`, `carousel_media`, `user`) that `post_from_timeline_node`
already understands.
"""

from __future__ import annotations

import asyncio
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

from tool.ig.browser_types import TabLike
from tool.ig.hq_video import instagram_cookie_values
from tool.ig.types import ScrapedPost
from tool.ig.web_api import WebApiError, request_json

FEED_PAGE_SIZE = 12

# The feed endpoint can transiently answer with a server error while the
# browser session is settling. `request_json` already retries these statuses
# for its normal transient policy; the extra bounded attempts here keep a
# temporary response from immediately selecting the much slower grid-scroll
# fallback.
_TIMELINE_API_ATTEMPTS = 4
_TIMELINE_RETRY_DELAY = 0.5
_TIMELINE_RETRYABLE_STATUSES = frozenset({400, 408, 425, 500, 502, 503, 504})

# Instagram can return placeholder URLs for unavailable/missing media.
# These look like http://static.cdninstagram.com/rsrc.php/null.jpg and do not
# contain the real CDN path pattern (/t\d+\.\d+-(?:15|6)/).
_PLACEHOLDER_URL_RE = re.compile(r"(?:rsrc\.php|/null\.(?:jpg|png|gif|webp))(?:$|[?#])")


class ProfileTimelineUnavailableError(RuntimeError):
    """Profile timeline API could not serve metadata; callers may fall back."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        retry_after: float | None = None,
        url: str = "",
    ) -> None:
        self.status = status
        self.retry_after = retry_after
        self.url = url
        super().__init__(message)


@dataclass(frozen=True)
class ProfileTimelineSession:
    username: str
    # Kept for callers that construct sessions from the older ID-based API.
    # Sessions opened by this module use Instagram's username feed endpoint and
    # therefore leave the numeric ID unset.
    user_id: str | None
    cookies: dict[str, str]


@dataclass(frozen=True)
class ProfilePostsPage:
    posts: list[ScrapedPost]
    end_cursor: str | None
    has_next_page: bool


async def open_profile_timeline(tab: TabLike, username: str) -> ProfileTimelineSession:
    """Snapshot authenticated cookies for the username-based timeline API.

    Do not resolve the username through ``web_profile_info`` here. Instagram
    currently rate-limits that endpoint independently of the profile feed, so
    doing the ID lookup first turns an otherwise available timeline into the
    slow grid-scroll fallback.
    """
    try:
        cookies = await instagram_cookie_values(tab)
    except Exception as exc:
        raise ProfileTimelineUnavailableError(f"could not read browser cookies: {exc}") from exc
    return ProfileTimelineSession(username=username, user_id=None, cookies=cookies)


def _feed_page_url(
    user_id: str,
    *,
    after: str | None = None,
    count: int = FEED_PAGE_SIZE,
) -> str:
    """Build the legacy ID-based feed URL for one timeline page."""
    query = urllib.parse.urlencode({"count": count})
    if after:
        query = f"{query}&max_id={urllib.parse.quote(after)}"
    return f"https://www.instagram.com/api/v1/feed/user/{user_id}/?{query}"


def _feed_username_page_url(
    username: str,
    *,
    after: str | None = None,
    count: int = FEED_PAGE_SIZE,
) -> str:
    """Build the web v1 feed URL that resolves a profile by username."""
    query = urllib.parse.urlencode({"count": count})
    if after:
        query = f"{query}&max_id={urllib.parse.quote(after)}"
    encoded_username = urllib.parse.quote(username, safe="")
    return f"https://www.instagram.com/api/v1/feed/user/{encoded_username}/username/?{query}"


async def fetch_profile_posts_page(
    session: ProfileTimelineSession,
    *,
    after: str | None = None,
) -> ProfilePostsPage:
    url = (
        _feed_page_url(session.user_id, after=after)
        if session.user_id
        else _feed_username_page_url(session.username, after=after)
    )
    data: Any | None = None
    for attempt in range(_TIMELINE_API_ATTEMPTS):
        try:
            data = await asyncio.to_thread(
                request_json,
                url,
                session.cookies,
                f"https://www.instagram.com/{session.username}/",
                "profile timeline",
            )
            break
        except WebApiError as exc:
            if exc.status not in _TIMELINE_RETRYABLE_STATUSES or attempt == _TIMELINE_API_ATTEMPTS - 1:
                raise ProfileTimelineUnavailableError(
                    str(exc), status=exc.status, retry_after=exc.retry_after, url=exc.url
                ) from exc
            await asyncio.sleep(_TIMELINE_RETRY_DELAY * (2**attempt))

    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ProfileTimelineUnavailableError("profile timeline response did not include items")

    posts: list[ScrapedPost] = []
    for item in items:
        if isinstance(item, dict):
            post = post_from_timeline_node(item, username_hint=session.username)
            if post:
                posts.append(post)

    next_max_id = data.get("next_max_id") if isinstance(data, dict) else None
    return ProfilePostsPage(
        posts=posts,
        end_cursor=str(next_max_id) if next_max_id else None,
        has_next_page=bool(data.get("more_available")) if isinstance(data, dict) else False,
    )


def post_from_timeline_node(node: dict[str, Any], *, username_hint: str | None = None) -> ScrapedPost | None:
    shortid = node.get("code")
    if not isinstance(shortid, str) or not shortid:
        return None

    caption = _caption_text(node)
    owner_raw = node.get("user")
    owner = owner_raw if isinstance(owner_raw, dict) else {}
    username = owner.get("username") if isinstance(owner.get("username"), str) else username_hint
    media_type = node.get("media_type")
    carousel_raw = node.get("carousel_media")
    carousel = carousel_raw if isinstance(carousel_raw, list) else []

    if media_type == 8 or carousel:
        urls: list[str] = []
        for child in carousel:
            if not isinstance(child, dict):
                continue
            url = _best_video_url(child) if child.get("media_type") == 2 else _best_image_url(child)
            if url:
                urls.append(url)
        return ScrapedPost(shortid, "carousel", urls, username=username, caption=caption) if urls else None

    if media_type == 2 or node.get("product_type") in {"clips", "igtv"}:
        url = _best_video_url(node)
        return ScrapedPost(shortid, "video", [url], username=username, caption=caption) if url else None

    url = _best_image_url(node)
    return ScrapedPost(shortid, "image", [url], username=username, caption=caption) if url else None


def _caption_text(node: dict[str, Any]) -> str | None:
    caption = node.get("caption")
    if isinstance(caption, dict):
        text = caption.get("text")
        if isinstance(text, str):
            return text
    edges = node.get("edge_media_to_caption")
    if isinstance(edges, dict):
        items = edges.get("edges")
        if isinstance(items, list) and items:
            first = items[0].get("node") if isinstance(items[0], dict) else None
            if isinstance(first, dict):
                text = first.get("text")
                if isinstance(text, str):
                    return text
    return None


def _best_image_url(node: dict[str, Any]) -> str | None:
    versions = node.get("image_versions2")
    candidates = versions.get("candidates") if isinstance(versions, dict) else None
    if not isinstance(candidates, list):
        return None
    best = max(
        (
            candidate
            for candidate in candidates
            if isinstance(candidate, dict)
            and isinstance(candidate.get("url"), str)
            and not _PLACEHOLDER_URL_RE.search(str(candidate["url"]))
        ),
        key=lambda candidate: int(candidate.get("width") or 0) * int(candidate.get("height") or 0),
        default=None,
    )
    url = best.get("url") if best else None
    return url if isinstance(url, str) else None


def _best_video_url(node: dict[str, Any]) -> str | None:
    versions = node.get("video_versions")
    if not isinstance(versions, list):
        return None
    best = max(
        (
            candidate
            for candidate in versions
            if isinstance(candidate, dict)
            and isinstance(candidate.get("url"), str)
            and not _PLACEHOLDER_URL_RE.search(str(candidate["url"]))
        ),
        key=lambda candidate: int(candidate.get("width") or 0) * int(candidate.get("height") or 0),
        default=None,
    )
    url = best.get("url") if best else None
    return url if isinstance(url, str) else None
