"""Rate-limited HTTP session using urllib.request."""

import json
import ssl
import threading
import time
from http.client import HTTPException
from typing import IO, Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, Request, build_opener

_TIMEOUT = 30.0
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_RETRY_BACKOFF = 0.5


class RateLimiter:
    """Token bucket rate limiter — 1 token/sec, max burst 1."""

    def __init__(self, rate: float = 1.0):
        self._rate = rate
        self._tokens = 1.0
        self._max_tokens = 1.0
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until a token is available, then consume it."""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._max_tokens, self._tokens + elapsed * self._rate)
            self._last_refill = now
            wait = max(0.0, (1.0 - self._tokens) / self._rate)
            self._tokens -= 1.0
        if wait > 0:
            time.sleep(wait)


def _read(response: IO[bytes], max_bytes: int | None) -> bytes:
    data: bytes = response.read() if max_bytes is None else response.read(max_bytes + 1)
    return data


class Session:
    """urllib.request opener with User-Agent, optional rate limiting, and JSON helpers."""

    def __init__(
        self,
        user_agent: str = "spotify-playlist/0.1.0 (joshuarli@icloud.com)",
        rate_limiter: RateLimiter | None = None,
    ):
        self.headers = {"User-Agent": user_agent}
        self.opener = build_opener(HTTPSHandler(context=ssl.create_default_context()))
        self.rate_limiter = rate_limiter

    def _wait(self) -> None:
        if self.rate_limiter:
            self.rate_limiter.acquire()

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        retries: int = 0,
        max_bytes: int | None = None,
    ) -> tuple[int, bytes]:
        """Send a request and return (status, body); HTTP error statuses are returned, not raised.

        Retries on connection failures and 429/5xx with exponential backoff. When *max_bytes*
        is set, at most *max_bytes* + 1 bytes of the body are read.
        """
        merged = {**self.headers, **(headers or {})}
        for attempt in range(retries + 1):
            self._wait()
            try:
                try:
                    with self.opener.open(
                        Request(url, data=body, headers=merged, method=method), timeout=_TIMEOUT
                    ) as response:
                        status, data = int(response.status), _read(response, max_bytes)
                except HTTPError as error:
                    with error:
                        status, data = error.code, _read(error, max_bytes)
            except (OSError, HTTPException):
                if attempt == retries:
                    raise
            else:
                if status not in _RETRY_STATUSES or attempt == retries:
                    return status, data
            time.sleep(_RETRY_BACKOFF * 2**attempt)
        raise AssertionError("unreachable")

    def get(self, url: str, params: dict[str, str | int] | None = None, headers: dict[str, str] | None = None) -> bytes:
        """GET *url* with optional query params. Returns raw body bytes."""
        if params is not None and params:
            url = f"{url}?{urlencode(params)}"
        return self.request("GET", url, headers=headers)[1]

    def get_json(
        self, url: str, params: dict[str, str | int] | None = None, headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """GET *url* and parse JSON response."""
        data = self.get(url, params=params, headers=headers)
        return json.loads(data.decode("utf-8"))

    def get_text(
        self, url: str, params: dict[str, str | int] | None = None, headers: dict[str, str] | None = None
    ) -> str:
        """GET *url* and return response as string."""
        data = self.get(url, params=params, headers=headers)
        return data.decode("utf-8")

    def post(
        self,
        url: str,
        params: dict[str, str | int] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        """POST *url* with optional query params and JSON body. Returns raw bytes."""
        if params is not None and params:
            url = f"{url}?{urlencode(params)}"
        merged = dict(headers or {})
        if json_body is not None:
            merged.setdefault("Content-Type", "application/json")
            body = json.dumps(json_body).encode("utf-8")
        else:
            body = None
        return self.request("POST", url, headers=merged, body=body)[1]

    def post_json(
        self,
        url: str,
        params: dict[str, str | int] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """POST *url* and parse JSON response."""
        data = self.post(url, params=params, json_body=json_body, headers=headers)
        return json.loads(data.decode("utf-8"))
