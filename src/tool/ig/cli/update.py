from __future__ import annotations

import sys

from tool.ig.cli.get import _get
from tool.ig.cli.scan import _scan


async def _update(
    *,
    live: bool = False,
    full: bool = False,
    pace: str = "fast",
    media_dir: str = "site/images",
    jobs: int = 1,
    extensions: list[str] | None = None,
    ublock: bool = True,
) -> None:
    """Rescan every profile and the saved collection already in the DB, then download new/incomplete posts."""
    for saved in (False, True):
        try:
            await _scan([], live=live, full=full, saved=saved, rescan=True, extensions=extensions, ublock=ublock)
        except Exception as exc:
            label = "saved-profile" if saved else "profile"
            detail = str(exc).strip()
            reason = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
            print(
                f"update: {label} scan phase failed; continuing to downloads ({reason})",
                file=sys.stderr,
                flush=True,
            )
    await _get(
        [],
        live=live,
        pace=pace,
        media_dir=media_dir,
        saved=False,
        jobs=jobs,
        extensions=extensions,
        ublock=ublock,
    )
