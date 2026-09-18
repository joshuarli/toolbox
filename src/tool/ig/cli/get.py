from __future__ import annotations

import sqlite3
from pathlib import Path

from tool.ig.cli._profile_arg import profile_arg
from tool.ig.db import init_db, is_done
from tool.ig.profile_scan import download_scanned_profile, download_scanned_profiles
from tool.ig.types import Pace


def _incomplete_scopes(
    db: sqlite3.Connection,
    media_dir: str | Path,
    *,
    saved_only: bool = False,
) -> list[tuple[str, bool]]:
    """Return (username, is_saved) pairs that have at least one incomplete post."""
    rows = db.execute(
        """
        SELECT DISTINCT pp.username, pp.saved
        FROM profile_post pp
        ORDER BY pp.username, pp.saved
        """
    ).fetchall()

    incomplete: list[tuple[str, bool]] = []
    for username, saved_int in rows:
        is_saved = bool(saved_int)
        if saved_only and not is_saved:
            continue
        shortids = db.execute(
            "SELECT shortid FROM profile_post WHERE username = ? AND saved = ? ORDER BY seq",
            (username, saved_int),
        ).fetchall()
        for (shortid,) in shortids:
            if not is_done(db, shortid, username, media_dir=media_dir, saved=is_saved):
                incomplete.append((username, is_saved))
                break
    return incomplete


async def _get(
    urls: list[str],
    *,
    live: bool = False,
    pace: str = "fast",
    media_dir: str = "site/images",
    saved: bool = False,
    jobs: int = 1,
    extensions: list[str] | None = None,
    ublock: bool = True,
) -> None:
    if jobs < 1:
        raise ValueError("jobs must be at least 1")
    pace_obj = getattr(Pace, pace)()

    db = init_db()
    try:
        if urls:
            for url in urls:
                identifier = profile_arg(url, command="get")
                await download_scanned_profile(
                    identifier,
                    db,
                    live=live,
                    pace=pace_obj,
                    media_dir=media_dir,
                    saved=saved,
                    jobs=jobs,
                    extensions=extensions,
                    ublock=ublock,
                )
        else:
            scopes = _incomplete_scopes(db, media_dir, saved_only=saved)
            if not scopes:
                label = "saved " if saved else ""
                print(f"all {label}posts are complete — nothing to download", flush=True)
                return
            print(
                f"downloading incomplete posts across {len(scopes)} profile{'s' if len(scopes) > 1 else ''}",
                flush=True,
            )
            await download_scanned_profiles(
                scopes,
                db,
                live=live,
                pace=pace_obj,
                media_dir=media_dir,
                jobs=jobs,
                extensions=extensions,
                ublock=ublock,
            )
    finally:
        db.close()
