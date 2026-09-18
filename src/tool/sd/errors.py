"""Shared user-facing error types."""

from __future__ import annotations


class SdError(RuntimeError):
    """Expected user-facing sd error."""


class SafetyError(SdError):
    """The target could not be proven safe to overwrite."""


class UnsupportedPlatformError(SdError):
    """A platform-gated helper was called on the wrong OS."""
