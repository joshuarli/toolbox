"""ig get-post: download a single post by shortid (debug/direct path)."""

from __future__ import annotations

import sys
from pathlib import Path

from tool.ig.db import init_db
from tool.ig.post import RateLimitError, scrape_post
from tool.ig.scraper import extract_shortid
from tool.ig.types import Pace


def shortid_arg(value: str) -> str:
    """Accept a bare shortid or a /p/<shortid>/ or /reel/<shortid>/ URL."""
    try:
        return extract_shortid(value)
    except ValueError as exc:
        raise ValueError("get-post expects a post shortid or /p/<shortid>/ URL") from exc


async def _get_post(
    target: str,
    *,
    live: bool = False,
    pace: str = "fast",
    media_dir: str | Path = "site/images",
    extensions: list[str] | None = None,
    ublock: bool = True,
) -> None:
    """Scrape and download one post via the direct /p/<shortid>/ path."""
    shortid = shortid_arg(target)
    pace_obj = getattr(Pace, pace)()
    db = init_db()
    try:
        await scrape_post(
            shortid,
            db,
            live=live,
            pace=pace_obj,
            media_dir=media_dir,
            extensions=extensions,
            ublock=ublock,
        )
    except RateLimitError as exc:
        print(
            f"rate-limited fetching {exc.shortid} (retry after {exc.backoff_seconds() / 60:.1f} minutes)",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    finally:
        db.close()
