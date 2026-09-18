from __future__ import annotations

from pathlib import Path

from tool.ig.db import init_db
from tool.ig.media import media_path, profile_key
from tool.ig.profile_scan import init_profile_table as _init_profile_table


def _remove(username: str, *, dry_run: bool = False, media_dir: str = "site/images", saved: bool = False) -> None:
    db = init_db()
    _init_profile_table(db)
    saved_int = int(saved)
    removed_shortids = {
        row[0]
        for row in db.execute(
            "SELECT shortid FROM profile_post WHERE username = ? AND saved = ?",
            (username, saved_int),
        )
    }
    rows = db.execute(
        """
        SELECT p.shortid, p.post_type, m.idx, m.url, COUNT(*) OVER (PARTITION BY p.shortid) AS total
        FROM media m
        JOIN post p ON m.shortid = p.shortid
        JOIN profile_post pp ON pp.shortid = p.shortid
        WHERE pp.username = ? AND pp.saved = ?
        ORDER BY p.shortid, m.idx
        """,
        (username, saved_int),
    ).fetchall()
    post_count = db.execute(
        "SELECT COUNT(*) FROM profile_post WHERE username = ? AND saved = ?",
        (username, saved_int),
    ).fetchone()[0]
    label = f"@{username} saved" if saved else f"@{username}"

    if post_count == 0:
        print(f"no posts found for {label}")
        db.close()
        return

    print(f"{label}: {post_count} posts, {len(rows)} media files")
    pkey = profile_key(username, saved)
    for shortid, post_type, idx, media_url, total in rows:
        print(f"  {media_path(shortid, post_type, idx, total, media_dir, media_url, profile=pkey)}")

    if dry_run:
        db.close()
        return

    db.execute("DELETE FROM profile_post WHERE username = ? AND saved = ?", (username, saved_int))
    if not saved:
        db.execute("DELETE FROM profile_scrape WHERE username = ?", (username,))
    db.execute("DELETE FROM profile_failure WHERE username = ? AND saved = ?", (username, saved_int))

    orphan_shortids = (
        {
            row[0]
            for row in db.execute(
                f"""
            SELECT p.shortid
            FROM post p
            WHERE p.shortid IN ({",".join("?" for _ in removed_shortids)})
              AND p.shortid NOT IN (SELECT shortid FROM profile_post)
            """,
                tuple(removed_shortids),
            )
        }
        if removed_shortids
        else set()
    )

    deleted = 0
    for shortid, post_type, idx, media_url, total in rows:
        # Delete the authoritative by-profile copy for this profile
        p = media_path(shortid, post_type, idx, total, media_dir, media_url, profile=pkey)
        if p.exists():
            p.unlink()
            deleted += 1
        # Also clean legacy sharded copy if orphaned and still present
        if shortid in orphan_shortids:
            legacy = media_path(shortid, post_type, idx, total, media_dir, media_url)
            if legacy.exists() and legacy != p:
                legacy.unlink()
                deleted += 1

    if orphan_shortids:
        placeholders = ",".join("?" for _ in orphan_shortids)
        db.execute(f"DELETE FROM media WHERE shortid IN ({placeholders})", tuple(orphan_shortids))
        db.execute(f"DELETE FROM post WHERE shortid IN ({placeholders})", tuple(orphan_shortids))
    # Clean up empty by-profile directory
    try:
        profile_dir = Path(media_dir) / "by-profile" / pkey
        if profile_dir.exists() and not any(profile_dir.iterdir()):
            profile_dir.rmdir()
    except OSError:
        pass
    db.commit()
    db.execute("PRAGMA wal_checkpoint(PASSIVE)")
    db.close()
    print(f"removed: {post_count} posts, {deleted} files deleted")
