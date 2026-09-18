"""Shared HTTP retry loop for Instagram CDN and API calls."""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from typing import Any, TypeVar

_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF = 0.5
_RETRYABLE_STATUSES = frozenset({408, 425, 500, 502, 503, 504})

T = TypeVar("T")


def _retryable_status(code: int) -> bool:
    return code in _RETRYABLE_STATUSES


def http_request(
    req: urllib.request.Request,
    *,
    attempts: int = _RETRY_ATTEMPTS,
    backoff: float = _RETRY_BACKOFF,
    timeout: float = 30,
) -> bytes:
    """Execute a urllib Request with retries on transient errors.

    Retries on URLError and retryable HTTP status codes (5xx, 408, 425).
    Does not retry 429 — rate limits must be handled by the caller.
    Returns the response body bytes on success.
    """
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return bytes(r.read())
        except urllib.error.HTTPError as exc:
            if not _retryable_status(exc.code) or attempt == attempts - 1:
                raise
        except urllib.error.URLError:
            if attempt == attempts - 1:
                raise
        time.sleep(backoff * (2**attempt))
    raise RuntimeError("unreachable http retry state")


def http_request_json(
    req: urllib.request.Request,
    *,
    attempts: int = _RETRY_ATTEMPTS,
    backoff: float = _RETRY_BACKOFF,
    timeout: float = 30,
) -> Any:
    """Like http_request but decodes JSON and returns parsed value."""
    body = http_request(req, attempts=attempts, backoff=backoff, timeout=timeout)
    return __import__("json").loads(body.decode("utf-8"))
