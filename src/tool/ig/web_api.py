"""Shared primitives for Instagram's authenticated web v1 API.

The current Instagram web app serves timelines, following lists and media info
through xdt/private-web endpoints under `https://www.instagram.com/api/v1/*`,
authenticated with the browser's session cookies.  These helpers centralise the
auth header construction, JSON GETs with transient-error retries and 429
surfacing, and username -> numeric user-id resolution used by `following.py`.

The legacy `/graphql/query` document queries (the shortcode `doc_id` and
`query_hash` variants) are retired by Instagram and must not be reintroduced.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from tool.ig.errors import InstagramHttpError
from tool.ig.hq_video import (
    INSTAGRAM_APP_ID,
    INSTAGRAM_WEB_ASBD_ID,
    instagram_api_user_agent,
    parse_retry_after,
)
from tool.ig.http_retry import http_request_json


class WebApiError(InstagramHttpError):
    """Authenticated Instagram web v1 API request failure."""


_PROFILE_INFO_FALLBACK_STATUSES = frozenset({400, 429})


def require_auth(
    cookies: dict[str, str],
    *,
    error_cls: type[InstagramHttpError] = WebApiError,
) -> None:
    if not cookies.get("sessionid") or not cookies.get("csrftoken"):
        raise error_cls("Instagram web API requires authenticated browser cookies")


def auth_headers(
    cookies: dict[str, str],
    referer: str,
    *,
    error_cls: type[InstagramHttpError] = WebApiError,
) -> dict[str, str]:
    require_auth(cookies, error_cls=error_cls)
    cookie_header = "; ".join(f"{name}={value}" for name, value in sorted(cookies.items()) if value)
    return {
        "Accept": "*/*",
        "Cookie": cookie_header,
        "Origin": "https://www.instagram.com",
        "Referer": referer,
        "User-Agent": instagram_api_user_agent(),
        "X-ASBD-ID": INSTAGRAM_WEB_ASBD_ID,
        "X-CSRFToken": cookies["csrftoken"],
        "X-IG-App-ID": INSTAGRAM_APP_ID,
        "X-IG-WWW-Claim": "0",
        "X-Requested-With": "XMLHttpRequest",
    }


def request_json(
    url: str,
    cookies: dict[str, str],
    referer: str,
    label: str,
    *,
    error_cls: type[InstagramHttpError] = WebApiError,
) -> dict[str, Any]:
    """GET `url` with browser cookies and return the parsed JSON.

    Transient HTTP errors are retried by `http_request_json`; rate limits are
    not retried and surface as `error_cls` with status 429 and the parsed
    Retry-After so orchestration can convert them to backoff policy.
    """
    req = urllib.request.Request(url, headers=auth_headers(cookies, referer, error_cls=error_cls))
    try:
        data = http_request_json(req)
    except urllib.error.HTTPError as exc:
        raise error_cls(
            f"{label} HTTP {exc.code}",
            status=exc.code,
            retry_after=parse_retry_after(exc.headers.get("retry-after")),
            url=url,
        ) from exc
    except urllib.error.URLError as exc:
        raise error_cls(f"{label} request failed: {exc.reason}", url=url) from exc
    return data


def profile_id(
    username: str,
    cookies: dict[str, str],
    *,
    error_cls: type[InstagramHttpError] = WebApiError,
) -> str:
    """Resolve a username to an Instagram numeric user ID.

    ``web_profile_info`` is still the preferred resolver, but Instagram now
    returns account-specific 400s and broad 429s for that endpoint. The feed
    endpoint exposes the same ID in its top-level ``user`` object, so use it as
    a bounded fallback for those statuses (not for unrelated request errors).
    """
    query = urllib.parse.urlencode({"username": username})
    url = f"https://www.instagram.com/api/v1/users/web_profile_info/?{query}"
    try:
        data = request_json(
            url,
            cookies,
            f"https://www.instagram.com/{username}/",
            "profile info",
            error_cls=error_cls,
        )
    except InstagramHttpError as exc:
        if exc.status not in _PROFILE_INFO_FALLBACK_STATUSES:
            raise
        return _profile_id_from_username_feed(username, cookies, error_cls=error_cls)
    user = data.get("data", {}).get("user")
    if not isinstance(user, dict):
        raise error_cls(f"profile info did not include @{username}")
    user_id = user.get("id")
    if not user_id:
        raise error_cls(f"profile info did not include an id for @{username}")
    return str(user_id)


def _profile_id_from_username_feed(
    username: str,
    cookies: dict[str, str],
    *,
    error_cls: type[InstagramHttpError],
) -> str:
    """Resolve a profile ID from the username feed used by profile timelines."""
    encoded_username = urllib.parse.quote(username, safe="")
    url = f"https://www.instagram.com/api/v1/feed/user/{encoded_username}/username/?count=1"
    data = request_json(
        url,
        cookies,
        f"https://www.instagram.com/{username}/",
        "profile feed",
        error_cls=error_cls,
    )

    candidates: list[Any] = [data.get("user")]
    items = data.get("items")
    if isinstance(items, list):
        candidates.extend(item.get("user") for item in items if isinstance(item, dict))
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        user_id = candidate.get("pk") or candidate.get("id")
        if user_id:
            return str(user_id)
    raise error_cls(f"profile feed did not include an id for @{username}", url=url)
