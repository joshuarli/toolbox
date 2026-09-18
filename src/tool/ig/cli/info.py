from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from tool.ig.db import init_db, is_done

_GREEN = "\033[32m"
_RESET = "\033[0m"


def _green_if_done(line: str, total: int, completed: int) -> str:
    if total > 0 and total == completed:
        return f"{_GREEN}{line}{_RESET}"
    return line


def _info_usernames() -> None:
    db = init_db()
    try:
        usernames = [
            row[0]
            for row in db.execute(
                "SELECT DISTINCT username FROM profile_post WHERE saved = 0 ORDER BY username"
            ).fetchall()
        ]
        print(" ".join(f"@{u}" for u in usernames))
    finally:
        db.close()


def _info(media_dir: str | Path = "site/images", *, images: bool = False) -> None:
    db = init_db()
    try:
        rows = db.execute(
            """
            SELECT pp.username, pp.saved, pp.shortid, p.post_type
            FROM profile_post pp
            LEFT JOIN post p ON p.shortid = pp.shortid
            ORDER BY pp.username, pp.saved, pp.seq, pp.shortid
            """
        ).fetchall()

        following_counts = dict(
            db.execute(
                "SELECT username, COUNT(*) FROM profile_following WHERE current = 1 GROUP BY username"
            ).fetchall()
        )

        if not rows:
            print("database is empty — no profiles scanned yet")
            return

        profiles: dict[tuple[str, int], dict[str, int]] = defaultdict(
            lambda: {"total": 0, "completed": 0, "images": 0, "carousels": 0, "videos": 0}
        )
        for username, saved, shortid, post_type in rows:
            stats = profiles[(username, saved)]
            stats["total"] += 1
            if images and is_done(db, shortid, username, media_dir=media_dir, saved=bool(saved)):
                stats["completed"] += 1
            if post_type == "image":
                stats["images"] += 1
            elif post_type == "carousel":
                stats["carousels"] += 1
            elif post_type == "video":
                stats["videos"] += 1

        done_header = "done" if images else "  -"
        print(f"{'profile':<24} {'posts':>6} {done_header:>6} {'follow':>7}  {'img':>5} {'car':>5} {'vid':>5}")
        print("-" * 72)

        grand_total = 0
        grand_completed = 0
        for (username, saved), stats in profiles.items():
            total = stats["total"]
            completed = stats["completed"]
            label = f"@{username} saved" if saved else f"@{username}"
            grand_total += total
            grand_completed += completed
            follow = following_counts.get(username, 0) if not saved else 0
            done_col = f"{completed:>6}" if images else "     -"
            line = (
                f"{label:<24} {total:>6} {done_col} {follow:>7}  "
                f"{stats['images']:>5} {stats['carousels']:>5} {stats['videos']:>5}"
            )
            print(_green_if_done(line, total, completed))

        print("-" * 72)
        if images:
            total_line = f"{'total':<24} {grand_total:>6} {grand_completed:>6}"
        else:
            total_line = f"{'total':<24} {grand_total:>6}       -"
        print(_green_if_done(total_line, grand_total, grand_completed))
    finally:
        db.close()
