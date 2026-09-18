from __future__ import annotations

import sqlite3

from tool.ig.cli._profile_arg import profile_arg
from tool.ig.db import init_db
from tool.ig.profile_scan import DEFAULT_SAVED_PROFILE, scan_profiles


def _scanned_usernames(db: sqlite3.Connection, *, saved: bool) -> list[str]:
    rows = db.execute(
        "SELECT DISTINCT username FROM profile_post WHERE saved = ? ORDER BY username",
        (int(saved),),
    ).fetchall()
    return [row[0] for row in rows]


async def _scan(
    urls: list[str],
    *,
    live: bool = False,
    full: bool = False,
    saved: bool = False,
    rescan: bool = False,
    extensions: list[str] | None = None,
    ublock: bool = True,
) -> None:
    db = init_db()
    try:
        if urls:
            identifiers = [profile_arg(url, command="scan") for url in urls]
        elif rescan:
            identifiers = _scanned_usernames(db, saved=saved)
            if not identifiers:
                return
            scope = "saved " if saved else ""
            label = f"rescanning {len(identifiers)} {scope}profile{'s' if len(identifiers) > 1 else ''}"
            print(label, flush=True)
        elif saved:
            identifiers = [DEFAULT_SAVED_PROFILE]
        else:
            identifiers = _scanned_usernames(db, saved=False)
            if not identifiers:
                print("no profiles scanned yet — nothing to rescan", flush=True)
                return
            label = f"rescanning {len(identifiers)} profile{'s' if len(identifiers) > 1 else ''}"
            print(label, flush=True)
        await scan_profiles(
            identifiers, db, live=live, stop_at_known=not full, saved=saved, extensions=extensions, ublock=ublock
        )
    finally:
        db.close()
