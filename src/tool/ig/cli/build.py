from __future__ import annotations

from tool.ig.site_builder import ThumbnailGenerationError, build


def _build_site(media_dir: str = "site/images") -> None:
    try:
        build(media_dir=media_dir)
    except ThumbnailGenerationError as exc:
        raise SystemExit(f"thumbnail generation failed: {exc}") from exc
