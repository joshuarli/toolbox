"""Optional ``browser`` extra (shadowdriver) access."""

from __future__ import annotations

try:
    import shadowdriver
except ModuleNotFoundError as exc:
    if exc.name != "shadowdriver":
        raise
    shadowdriver = None  # type: ignore[no-redefinition]


class BrowserExtraMissingError(RuntimeError):
    """Raised when a browser command runs without the ``browser`` extra installed."""


def require_shadowdriver():
    """Return the shadowdriver module, or raise with the install hint."""
    if shadowdriver is None:
        raise BrowserExtraMissingError(
            "browser support requires the 'browser' extra (shadowdriver is not installed). "
            "Run `uv sync --extra browser` to install it."
        )
    return shadowdriver
