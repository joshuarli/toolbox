"""Shared error types for Instagram HTTP failures."""

from __future__ import annotations

# Rate-limit backoff bounds shared across post scraping and following sync.
# When Instagram returns HTTP 429 with a Retry-After header that value is
# clamped into this range; when no header is present the default is used.
_RATE_LIMIT_DEFAULT_BACKOFF: float = 15 * 60
_RATE_LIMIT_MIN_BACKOFF: float = 60
_RATE_LIMIT_MAX_BACKOFF: float = 2 * 60 * 60


class InstagramHttpError(RuntimeError):
    """Base for Instagram HTTP errors carrying status, retry-after, and URL."""

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

    def backoff_seconds(self) -> float:
        """Clamp retry_after into [min, max] or return the default backoff.

        Used by post scraping and following sync so rate-limit policy stays
        in one place.
        """
        if self.retry_after is None:
            return _RATE_LIMIT_DEFAULT_BACKOFF
        return max(_RATE_LIMIT_MIN_BACKOFF, min(_RATE_LIMIT_MAX_BACKOFF, self.retry_after))
