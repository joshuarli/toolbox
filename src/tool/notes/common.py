"""Shared paths and errors for the ``notes`` tool."""

from __future__ import annotations

import shutil
from pathlib import Path

# Hardcoded vault: the Obsidian vault synced via iCloud.
VAULT = Path.home() / "iCloud~md~obsidian/Documents/Notes"
IMAGE_FOLDER = "z-images"
# Matches the webp-paste plugin default (`~/d/obsidian-webp-paste`): pasted
# and Loupe-imported images are encoded at quality 85.
WEBP_QUALITY = 85
# Same browser-like headers the webp-paste plugin sends for image downloads.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
IMAGE_ACCEPT = "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"


class NotesError(Exception):
    """Fatal ``notes`` error; the CLI reports it as exit code 2."""


class ContextTooLargeError(NotesError):
    """A single chunk exceeds the Foundation Models context window."""


def resolve_vault(override: Path | None) -> Path:
    """Return the vault directory, raising :class:`NotesError` if it is missing."""
    vault = Path(override) if override is not None else VAULT
    if not vault.is_dir():
        raise NotesError(f"notes vault not found: {vault}")
    return vault


def images_path(vault: Path) -> Path:
    """Return the vault-relative ``z-images/`` directory without creating it."""
    return vault / IMAGE_FOLDER


def require_tool(name: str, hint: str) -> str:
    """Return the path to *name* on ``PATH``, or raise :class:`NotesError`."""
    path = shutil.which(name)
    if path is None:
        raise NotesError(f"{name} not found. {hint}")
    return path
