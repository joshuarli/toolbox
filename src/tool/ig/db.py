from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from tool.ig.media import MEDIA_DIR, media_path, profile_key
from tool.ig.types import SavedScope

DB_PATH = "instagram.db"


def _profile_post_needs_saved_migration(db: sqlite3.Connection) -> bool:
    old_table = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'profile_post_old'"
    ).fetchone()
    if old_table:
        return True
    cols = db.execute("PRAGMA table_info(profile_post)").fetchall()
    if not cols:
        return True
    names = {row[1] for row in cols}
    pk_cols = [row[1] for row in sorted((row for row in cols if row[5]), key=lambda row: row[5])]
    return "saved" not in names or pk_cols != ["username", "saved", "shortid"]


def _migrate_profile_post_saved(db: sqlite3.Connection) -> None:
    if not _profile_post_needs_saved_migration(db):
        return
    tables = [
        name
        for name in ("profile_post", "profile_post_old")
        if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
    ]

    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("""
            CREATE TABLE profile_post_new (
                username TEXT NOT NULL,
                saved    INTEGER NOT NULL DEFAULT 0,
                shortid  TEXT NOT NULL REFERENCES post(shortid),
                seen_at  INTEGER NOT NULL,
                seq      INTEGER NOT NULL,
                PRIMARY KEY(username, saved, shortid)
            ) WITHOUT ROWID
        """)
        for table in tables:
            cols = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            required = {"username", "shortid", "seen_at", "seq"}
            if not required.issubset(cols):
                missing = ", ".join(sorted(required - cols))
                raise RuntimeError(f"cannot migrate {table}: missing columns {missing}")
            saved_expr = "saved" if "saved" in cols else "0"
            ordering = "username, saved, seq, shortid" if "saved" in cols else "username, seq, shortid"
            db.execute(
                f"""
                INSERT INTO profile_post_new (username, saved, shortid, seen_at, seq)
                SELECT username, {saved_expr}, shortid, seen_at, seq
                FROM {table}
                WHERE 1
                ORDER BY {ordering}
                ON CONFLICT(username, saved, shortid) DO NOTHING
                """
            )

        for table in tables:
            db.execute(f"DROP TABLE {table}")
        db.execute("ALTER TABLE profile_post_new RENAME TO profile_post")
        db.commit()
    except BaseException:
        db.rollback()
        raise


def init_db(path: str = DB_PATH) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS post (
                shortid    TEXT PRIMARY KEY,
                username   TEXT,
                caption    TEXT,
                post_type  TEXT,
                scraped_at INTEGER
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS profile_post (
                username TEXT NOT NULL,
                saved    INTEGER NOT NULL DEFAULT 0,
                shortid  TEXT NOT NULL REFERENCES post(shortid),
                seen_at  INTEGER NOT NULL,
                seq      INTEGER NOT NULL,
                PRIMARY KEY(username, saved, shortid)
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS profile_following (
                username          TEXT NOT NULL,
                followee_username TEXT NOT NULL,
                followee_id       TEXT,
                full_name         TEXT,
                is_private        INTEGER NOT NULL DEFAULT 0,
                profile_pic_url   TEXT,
                first_seen_at     INTEGER NOT NULL,
                last_seen_at      INTEGER NOT NULL,
                seq               INTEGER NOT NULL,
                current           INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(username, followee_username)
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS media (
                shortid TEXT NOT NULL REFERENCES post(shortid),
                idx     INTEGER NOT NULL,
                url     TEXT,
                PRIMARY KEY(shortid, idx)
            ) WITHOUT ROWID;
        """)
        _migrate_profile_post_saved(db)
        db.commit()
        return db
    except BaseException:
        db.close()
        raise


def is_done(
    db: sqlite3.Connection,
    shortid: str,
    username: str | None = None,
    media_dir: str | Path = MEDIA_DIR,
    saved: bool = False,
) -> bool:
    """True if post is in the DB for the requested scope with all media files present."""
    if username:
        row = db.execute(
            """
            SELECT p.post_type
            FROM post p
            JOIN profile_post pp ON pp.shortid = p.shortid
            WHERE p.shortid = ? AND pp.username = ? AND pp.saved = ?
            """,
            (shortid, username, int(saved)),
        ).fetchone()
    else:
        row = db.execute("SELECT post_type FROM post WHERE shortid = ?", (shortid,)).fetchone()
    if not row or row[0] is None:
        return False
    media = db.execute("SELECT idx, url FROM media WHERE shortid = ? ORDER BY idx", (shortid,)).fetchall()
    total = len(media)
    if not media:
        return False
    if username:
        pkey = profile_key(username, saved)
        for idx, url in media:
            new_path = media_path(shortid, row[0], idx, total, media_dir, url, profile=pkey)
            if new_path.exists() and new_path.stat().st_size > 0:
                continue
            # Fallback to legacy sharded location for migration / tests
            old_path = media_path(shortid, row[0], idx, total, media_dir, url)
            if old_path.exists() and old_path.stat().st_size > 0:
                continue
            return False
        return True
    return bool(media) and all(
        (path := media_path(shortid, row[0], idx, total, media_dir, url)).exists() and path.stat().st_size > 0
        for idx, url in media
    )


def upsert_post(
    db: sqlite3.Connection, shortid: str, username: str | None, caption: str | None, post_type: str
) -> None:
    db.execute(
        """
        INSERT INTO post (shortid, username, caption, post_type, scraped_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(shortid) DO UPDATE SET
            username   = excluded.username,
            caption    = excluded.caption,
            post_type  = excluded.post_type,
            scraped_at = excluded.scraped_at
        """,
        (shortid, username, caption, post_type, int(time.time())),
    )


def clear_media(db: sqlite3.Connection, shortid: str, total: int) -> None:
    db.execute("DELETE FROM media WHERE shortid = ? AND idx >= ?", (shortid, total))


def upsert_media(db: sqlite3.Connection, shortid: str, idx: int, url: str) -> None:
    db.execute(
        """
        INSERT INTO media (shortid, idx, url)
        VALUES (?, ?, ?)
        ON CONFLICT(shortid, idx) DO UPDATE SET
            url = excluded.url
        """,
        (shortid, idx, url),
    )


def add_profile_post(db: sqlite3.Connection, username: str, saved: SavedScope, shortid: str) -> None:
    saved_int = 1 if saved == "saved" else 0
    seq = db.execute(
        "SELECT COALESCE(MAX(seq) + 1, 0) FROM profile_post WHERE username = ? AND saved = ?",
        (username, saved_int),
    ).fetchone()[0]
    db.execute(
        """
        INSERT OR IGNORE INTO profile_post (username, saved, shortid, seen_at, seq)
        VALUES (?, ?, ?, ?, ?)
        """,
        (username, saved_int, shortid, int(time.time()), seq),
    )
