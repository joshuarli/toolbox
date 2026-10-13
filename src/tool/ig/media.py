from __future__ import annotations

import urllib.parse
from pathlib import Path

MEDIA_DIR = Path("site") / "images"


def media_ext(post_type: str | None) -> str:
    return ".mp4" if post_type == "video" else ".jpg"


def media_url_ext(post_type: str | None, url: str | None = None) -> str:
    """Extension for a media file, refining the post-type default with the URL path."""
    if post_type == "video":
        return ".mp4"
    if url:
        suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
        if suffix in {".mp4", ".mov", ".m4v", ".webm"}:
            return ".mp4"
    return media_ext(post_type)


def resolve_media_destination(media_dir: str | Path, dest: str | Path) -> Path:
    """Resolve a media destination and reject paths outside the configured root."""
    root = Path(media_dir).resolve()
    resolved = Path(dest).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError(f"media destination is outside the media directory: {dest}") from None
    return resolved


def dest_path(
    shortid: str,
    idx: int,
    total: int,
    ext: str,
    media_dir: str | Path = MEDIA_DIR,
    profile: str | None = None,
) -> Path:
    name = f"{shortid}{ext}" if total == 1 else f"{shortid}_{idx + 1}{ext}"
    base = Path(media_dir)
    if profile:
        dest = base / "by-profile" / profile / name
    else:
        dest = base / shortid[-2:].lower() / name
    resolve_media_destination(base, dest)
    return dest


def media_path(
    shortid: str,
    post_type: str | None,
    idx: int,
    total: int,
    media_dir: str | Path = MEDIA_DIR,
    url: str | None = None,
    profile: str | None = None,
) -> Path:
    return dest_path(shortid, idx, total, media_url_ext(post_type, url), media_dir=media_dir, profile=profile)


def profile_key(username: str, saved: bool | str = False) -> str:
    """Archive key for a profile's media directory (e.g. 'alice' or 'alice-saved')."""
    is_saved = saved == "saved" if isinstance(saved, str) else bool(saved)
    return f"{username}-saved" if is_saved else username
