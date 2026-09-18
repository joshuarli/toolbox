from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from tool.ig.db import DB_PATH, init_db
from tool.ig.profile_scan import init_profile_table


class ImportDbError(RuntimeError):
    pass


class ImportDbConflictError(ImportDbError):
    def __init__(self, conflicts: list[str]) -> None:
        self.conflicts = conflicts
        super().__init__("source database contains collections already present in target")


@dataclass(frozen=True)
class ImportDbSummary:
    posts: int
    media: int
    profile_posts: int
    following: int
    profile_scrapes: int
    profile_failures: int


def _table_exists(db: sqlite3.Connection, schema: str, table: str) -> bool:
    return (
        db.execute(
            f"SELECT 1 FROM {schema}.sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _columns(db: sqlite3.Connection, schema: str, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA {schema}.table_info({table})")}


def _row_count(db: sqlite3.Connection, table: str) -> int:
    return int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _check_quick(db: sqlite3.Connection, schema: str, label: str) -> None:
    row = db.execute(f"PRAGMA {schema}.quick_check").fetchone()
    if not row or row[0] != "ok":
        raise ImportDbError(f"{label} database failed quick_check: {row[0] if row else 'no result'}")


def _check_foreign_keys(db: sqlite3.Connection, schema: str, label: str) -> None:
    rows = db.execute(f"PRAGMA {schema}.foreign_key_check").fetchall()
    if rows:
        table, rowid, parent, fkid = rows[0]
        raise ImportDbError(
            f"{label} database failed foreign_key_check: {table} rowid={rowid} parent={parent} fkid={fkid}"
        )


def _profile_collection_labels(rows: set[tuple[str, int]]) -> list[str]:
    return [f"@{username} saved" if saved else f"@{username}" for username, saved in sorted(rows)]


def _source_profile_collections(db: sqlite3.Connection, table: str) -> set[tuple[str, int]]:
    if not _table_exists(db, "src", table):
        return set()
    cols = _columns(db, "src", table)
    saved_expr = "saved" if "saved" in cols else "0"
    return {
        (username, int(saved))
        for username, saved in db.execute(f"SELECT DISTINCT username, {saved_expr} FROM src.{table}")
    }


def _main_profile_collections(db: sqlite3.Connection, table: str) -> set[tuple[str, int]]:
    if not _table_exists(db, "main", table):
        return set()
    return {(username, int(saved)) for username, saved in db.execute(f"SELECT DISTINCT username, saved FROM {table}")}


def _source_usernames(db: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(db, "src", table):
        return set()
    return {row[0] for row in db.execute(f"SELECT DISTINCT username FROM src.{table}")}


def _main_usernames(db: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(db, "main", table):
        return set()
    return {row[0] for row in db.execute(f"SELECT DISTINCT username FROM {table}")}


def _preflight_import(db: sqlite3.Connection) -> None:
    conflicts: list[str] = []

    profile_conflicts = _source_profile_collections(db, "profile_post") & _main_profile_collections(db, "profile_post")
    if profile_conflicts:
        conflicts.append("profile collections: " + ", ".join(_profile_collection_labels(profile_conflicts)))

    failure_conflicts = _source_profile_collections(db, "profile_failure") & _main_profile_collections(
        db, "profile_post"
    )
    if failure_conflicts:
        conflicts.append(
            "profile failures for existing collections: " + ", ".join(_profile_collection_labels(failure_conflicts))
        )

    following_conflicts = _source_usernames(db, "profile_following") & _main_usernames(db, "profile_following")
    if following_conflicts:
        conflicts.append("following lists: " + ", ".join(f"@{username}" for username in sorted(following_conflicts)))

    scrape_conflicts = _source_usernames(db, "profile_scrape") & _main_usernames(db, "profile_scrape")
    if scrape_conflicts:
        conflicts.append("legacy scan state: " + ", ".join(f"@{username}" for username in sorted(scrape_conflicts)))

    if conflicts:
        raise ImportDbConflictError(conflicts)


def _import_posts(db: sqlite3.Connection) -> None:
    if not _table_exists(db, "src", "post"):
        return
    db.execute(
        """
        INSERT INTO post (shortid, username, caption, post_type, scraped_at)
        SELECT shortid, username, caption, post_type, scraped_at
        FROM src.post
        WHERE true
        ON CONFLICT(shortid) DO UPDATE SET
            username = COALESCE(post.username, excluded.username),
            caption = COALESCE(post.caption, excluded.caption),
            post_type = COALESCE(post.post_type, excluded.post_type),
            scraped_at = COALESCE(post.scraped_at, excluded.scraped_at)
        """
    )


def _import_profile_post_stubs(db: sqlite3.Connection) -> None:
    if not _table_exists(db, "src", "profile_post"):
        return
    db.execute(
        """
        INSERT OR IGNORE INTO post (shortid, username, caption, post_type, scraped_at)
        SELECT DISTINCT shortid, NULL, NULL, NULL, NULL
        FROM src.profile_post
        """
    )


def _import_media(db: sqlite3.Connection) -> None:
    if not _table_exists(db, "src", "media"):
        return
    db.execute(
        """
        INSERT INTO media (shortid, idx, url)
        SELECT shortid, idx, url
        FROM src.media
        WHERE true
        ON CONFLICT(shortid, idx) DO UPDATE SET
            url = COALESCE(media.url, excluded.url)
        """
    )


def _import_profile_posts(db: sqlite3.Connection) -> None:
    if not _table_exists(db, "src", "profile_post"):
        return
    cols = _columns(db, "src", "profile_post")
    saved_expr = "saved" if "saved" in cols else "0"
    db.execute(
        f"""
        INSERT INTO profile_post (username, saved, shortid, seen_at, seq)
        SELECT username, {saved_expr}, shortid, seen_at, seq
        FROM src.profile_post
        WHERE true
        ON CONFLICT(username, saved, shortid) DO NOTHING
        """
    )


def _import_following(db: sqlite3.Connection) -> None:
    if not _table_exists(db, "src", "profile_following"):
        return
    db.execute(
        """
        INSERT INTO profile_following (
            username,
            followee_username,
            followee_id,
            full_name,
            is_private,
            profile_pic_url,
            first_seen_at,
            last_seen_at,
            seq,
            current
        )
        SELECT
            username,
            followee_username,
            followee_id,
            full_name,
            is_private,
            profile_pic_url,
            first_seen_at,
            last_seen_at,
            seq,
            current
        FROM src.profile_following
        WHERE true
        ON CONFLICT(username, followee_username) DO UPDATE SET
            followee_id = COALESCE(profile_following.followee_id, excluded.followee_id),
            full_name = COALESCE(profile_following.full_name, excluded.full_name),
            is_private = profile_following.is_private OR excluded.is_private,
            profile_pic_url = COALESCE(profile_following.profile_pic_url, excluded.profile_pic_url),
            first_seen_at = MIN(profile_following.first_seen_at, excluded.first_seen_at),
            last_seen_at = MAX(profile_following.last_seen_at, excluded.last_seen_at),
            seq = profile_following.seq,
            current = profile_following.current OR excluded.current
        """
    )


def _import_profile_scrape(db: sqlite3.Connection) -> None:
    if not _table_exists(db, "src", "profile_scrape"):
        return
    db.execute(
        """
        INSERT INTO profile_scrape (
            username,
            last_shortid,
            updated_at,
            current_shortid,
            current_updated_at
        )
        SELECT
            username,
            last_shortid,
            updated_at,
            current_shortid,
            current_updated_at
        FROM src.profile_scrape
        WHERE true
        ON CONFLICT(username) DO NOTHING
        """
    )


def _import_profile_failures(db: sqlite3.Connection) -> None:
    if not _table_exists(db, "src", "profile_failure"):
        return
    cols = _columns(db, "src", "profile_failure")
    saved_expr = "saved" if "saved" in cols else "0"
    db.execute(
        f"""
        INSERT INTO profile_failure (username, saved, shortid, stage, reason, failed_at, resolved_at)
        SELECT username, {saved_expr}, shortid, stage, reason, failed_at, resolved_at
        FROM src.profile_failure
        WHERE true
        ON CONFLICT(username, saved, shortid, stage) WHERE resolved_at IS NULL DO UPDATE SET
            reason = excluded.reason,
            failed_at = excluded.failed_at
        """
    )


def import_db(source_path: str | Path, *, target_path: str | Path = DB_PATH) -> ImportDbSummary:
    source = Path(source_path).expanduser()
    target = Path(target_path).expanduser()
    if not source.exists():
        raise ImportDbError(f"source database does not exist: {source}")
    if not source.is_file():
        raise ImportDbError(f"source database is not a file: {source}")
    if source.resolve() == target.resolve():
        raise ImportDbError("source and target database are the same file")

    db = init_db(str(target))
    init_profile_table(db)
    try:
        db.execute("ATTACH DATABASE ? AS src", (str(source),))
        try:
            _check_quick(db, "main", "target")
            _check_foreign_keys(db, "main", "target")
            _check_quick(db, "src", "source")

            _preflight_import(db)

            before = {
                "post": _row_count(db, "post"),
                "media": _row_count(db, "media"),
                "profile_post": _row_count(db, "profile_post"),
                "profile_following": _row_count(db, "profile_following"),
                "profile_scrape": _row_count(db, "profile_scrape"),
                "profile_failure": _row_count(db, "profile_failure"),
            }

            db.execute("BEGIN")
            _import_posts(db)
            _import_profile_post_stubs(db)
            _import_media(db)
            _import_profile_posts(db)
            _import_following(db)
            _import_profile_scrape(db)
            _import_profile_failures(db)
            _check_quick(db, "main", "target")
            _check_foreign_keys(db, "main", "target")
            db.commit()
            _check_quick(db, "main", "target")
            _check_foreign_keys(db, "main", "target")

            after = {
                "post": _row_count(db, "post"),
                "media": _row_count(db, "media"),
                "profile_post": _row_count(db, "profile_post"),
                "profile_following": _row_count(db, "profile_following"),
                "profile_scrape": _row_count(db, "profile_scrape"),
                "profile_failure": _row_count(db, "profile_failure"),
            }
            return ImportDbSummary(
                posts=after["post"] - before["post"],
                media=after["media"] - before["media"],
                profile_posts=after["profile_post"] - before["profile_post"],
                following=after["profile_following"] - before["profile_following"],
                profile_scrapes=after["profile_scrape"] - before["profile_scrape"],
                profile_failures=after["profile_failure"] - before["profile_failure"],
            )
        except Exception:
            db.rollback()
            raise
        finally:
            db.execute("DETACH DATABASE src")
    finally:
        db.close()


def _import_db(source_path: str | Path) -> None:
    summary = import_db(source_path)
    print(f"imported from {Path(source_path)}")
    print(f"  posts:            {summary.posts}")
    print(f"  media:            {summary.media}")
    print(f"  profile posts:    {summary.profile_posts}")
    print(f"  following rows:   {summary.following}")
    print(f"  profile scrapes:  {summary.profile_scrapes}")
    print(f"  profile failures: {summary.profile_failures}")
