"""Rate-limited HTTP session pool using urllib3."""

import json
import threading
import time
from typing import Any
from urllib.parse import urlencode

from urllib3 import PoolManager


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


class Session:
    """urllib3 PoolManager with User-Agent, optional rate limiting, and JSON helpers."""

    def __init__(
        self,
        user_agent: str = "spotify-playlist/0.1.0 (joshuarli@icloud.com)",
        pool_size: int = 4,
        rate_limiter: RateLimiter | None = None,
    ):
        self.headers = {"User-Agent": user_agent}
        self.pool = PoolManager(num_pools=pool_size, maxsize=pool_size)
        self.rate_limiter = rate_limiter

    def _wait(self) -> None:
        if self.rate_limiter:
            self.rate_limiter.acquire()

    def get(self, url: str, params: dict[str, str | int] | None = None, headers: dict[str, str] | None = None) -> bytes:
        """GET *url* with optional query params. Returns raw body bytes."""
        self._wait()
        if params:
            url = f"{url}?{urlencode(params)}"
        merged = {**self.headers, **(headers or {})}
        resp = self.pool.request("GET", url, headers=merged)
        return resp.data

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
        self._wait()
        if params:
            url = f"{url}?{urlencode(params)}"
        merged = {**self.headers, **(headers or {})}
        if json_body is not None:
            merged.setdefault("Content-Type", "application/json")
            body = json.dumps(json_body).encode("utf-8")
        else:
            body = None
        resp = self.pool.request("POST", url, headers=merged, body=body)
        return resp.data

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
