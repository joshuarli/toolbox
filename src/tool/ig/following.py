"""Fetch and store profile following lists through Instagram's web v1 API."""

from __future__ import annotations

import asyncio
import sqlite3
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

from tool.ig.browser import browser_session
from tool.ig.errors import InstagramHttpError
from tool.ig.hq_video import instagram_cookie_values
from tool.ig.web_api import profile_id, request_json

FOLLOWING_PAGE_SIZE = 200


@dataclass(frozen=True)
class Followee:
    username: str
    user_id: str
    full_name: str
    is_private: bool
    profile_pic_url: str


@dataclass(frozen=True)
class FollowingSyncResult:
    username: str
    saved: int
    new: int
    stale: int


class FollowingFetchError(InstagramHttpError):
    pass


def init_profile_following(db: sqlite3.Connection) -> None:
    db.execute(
        """
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
        ) WITHOUT ROWID
        """
    )
    db.commit()


def _following_url(
    user_id: str,
    *,
    max_id: str | None = None,
    count: int = FOLLOWING_PAGE_SIZE,
) -> str:
    """Build the web v1 friendships page URL for one following page."""
    query = urllib.parse.urlencode({"count": count})
    if max_id:
        query = f"{query}&max_id={urllib.parse.quote(max_id)}"
    return f"https://www.instagram.com/api/v1/friendships/{user_id}/following/?{query}"


def _parse_following_page(data: dict[str, Any]) -> tuple[list[Followee], bool, str | None]:
    users = data.get("users")
    if not isinstance(users, list):
        raise FollowingFetchError("following response did not include users")
    nodes: list[Followee] = []
    for node in users:
        if not isinstance(node, dict):
            continue
        username = str(node.get("username") or "")
        if not username:
            continue
        nodes.append(
            Followee(
                username=username,
                user_id=str(node.get("pk") or node.get("id") or ""),
                full_name=str(node.get("full_name") or ""),
                is_private=bool(node.get("is_private")),
                profile_pic_url=str(node.get("profile_pic_url") or ""),
            )
        )
    has_next = bool(data.get("has_more"))
    next_max_id = data.get("next_max_id")
    return nodes, has_next, str(next_max_id) if next_max_id else None


def fetch_following(username: str, user_id: str, cookies: dict[str, str]) -> list[Followee]:
    followees: list[Followee] = []
    seen: set[str] = set()
    seen_cursors: set[str] = set()
    max_id: str | None = None
    while True:
        url = _following_url(user_id, max_id=max_id)
        data = request_json(
            url,
            cookies,
            f"https://www.instagram.com/{username}/",
            "following list",
            error_cls=FollowingFetchError,
        )
        page_nodes, has_next, next_max_id = _parse_following_page(data)
        for followee in page_nodes:
            if followee.username in seen:
                continue
            seen.add(followee.username)
            followees.append(followee)
        if not has_next:
            return followees
        if not next_max_id:
            raise FollowingFetchError("following response had has_more without next_max_id", url=url)
        if next_max_id in seen_cursors:
            raise FollowingFetchError(
                f"following response repeated next_max_id after {len(seen_cursors) + 1} pages", url=url
            )
        seen_cursors.add(next_max_id)
        max_id = next_max_id


def save_following(db: sqlite3.Connection, username: str, followees: list[Followee]) -> FollowingSyncResult:
    init_profile_following(db)
    now = int(time.time())
    existing = {
        row[0]: row[1]
        for row in db.execute(
            "SELECT followee_username, first_seen_at FROM profile_following WHERE username = ?",
            (username,),
        )
    }
    db.execute("UPDATE profile_following SET current = 0 WHERE username = ?", (username,))
    for seq, followee in enumerate(followees):
        first_seen_at = existing.get(followee.username, now)
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(username, followee_username) DO UPDATE SET
                followee_id = excluded.followee_id,
                full_name = excluded.full_name,
                is_private = excluded.is_private,
                profile_pic_url = excluded.profile_pic_url,
                first_seen_at = profile_following.first_seen_at,
                last_seen_at = excluded.last_seen_at,
                seq = excluded.seq,
                current = 1
            """,
            (
                username,
                followee.username,
                followee.user_id,
                followee.full_name,
                int(followee.is_private),
                followee.profile_pic_url,
                first_seen_at,
                now,
                seq,
            ),
        )
    db.commit()
    stale = db.execute(
        "SELECT COUNT(*) FROM profile_following WHERE username = ? AND current = 0",
        (username,),
    ).fetchone()[0]
    new = sum(1 for followee in followees if followee.username not in existing)
    return FollowingSyncResult(username=username, saved=len(followees), new=new, stale=int(stale))


def stored_following_usernames(db: sqlite3.Connection, username: str, *, current_only: bool = True) -> list[str]:
    init_profile_following(db)
    where = "WHERE username = ? AND current = 1" if current_only else "WHERE username = ?"
    rows = db.execute(
        f"""
        SELECT followee_username
        FROM profile_following
        {where}
        ORDER BY seq, followee_username
        """,
        (username,),
    ).fetchall()
    return [row[0] for row in rows]


async def get_following_with_cookies(
    username: str, db: sqlite3.Connection, cookies: dict[str, str]
) -> FollowingSyncResult:
    user_id = await asyncio.to_thread(profile_id, username, cookies, error_cls=FollowingFetchError)
    followees = await asyncio.to_thread(fetch_following, username, user_id, cookies)
    return save_following(db, username, followees)


async def get_following(
    username: str,
    db: sqlite3.Connection,
    *,
    live: bool = False,
    extensions: list[str] | None = None,
    ublock: bool = True,
) -> FollowingSyncResult:
    async with browser_session(headless=not live, extensions=extensions, ublock=ublock) as (_browser, tab):
        cookies = await instagram_cookie_values(tab)
        return await get_following_with_cookies(username, db, cookies)
