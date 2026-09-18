from __future__ import annotations

import asyncio
import sys

from tool.ig.browser import browser_session
from tool.ig.db import init_db
from tool.ig.following import (
    FollowingFetchError,
    get_following_with_cookies,
    instagram_cookie_values,
    stored_following_usernames,
)


async def _do_fetch(username: str, db, cookies: dict[str, str]) -> None:
    try:
        result = await get_following_with_cookies(username, db, cookies)
    except FollowingFetchError as exc:
        if exc.status == 429:
            delay = exc.backoff_seconds()
            print(
                f"HTTP 429 for @{username}; backing off for {delay / 60:.1f} minutes",
                file=sys.stderr,
                flush=True,
            )
            await asyncio.sleep(delay)
            result = await get_following_with_cookies(username, db, cookies)
        else:
            raise
    print(
        f"@{result.username}: saved {result.saved} following ({result.new} new, {result.stale} stale)",
        file=sys.stderr,
        flush=True,
    )


def _all_profile_usernames(db) -> list[str]:
    rows = db.execute("SELECT DISTINCT username FROM profile_post WHERE saved = 0 ORDER BY username").fetchall()
    return [row[0] for row in rows]


async def _get_following(
    urls: list[str],
    *,
    live: bool = False,
    refresh: bool = False,
    refresh_all: bool = False,
    extensions: list[str] | None = None,
    ublock: bool = True,
) -> None:
    if refresh_all:
        if urls:
            raise ValueError("--refresh with no arguments fetches all profiles; do not supply @usernames")
        db = init_db()
        urls = [f"@{u}" for u in _all_profile_usernames(db)]
        db.close()
        if not urls:
            print("no profiles in database", file=sys.stderr)
            return

    db = init_db()
    try:
        fetch_usernames: list[str] = []
        for url in urls:
            if not url.startswith("@") or len(url) == 1:
                raise ValueError("get-following expects @profile")
            username = url[1:]
            if not refresh and stored_following_usernames(db, username):
                for name in stored_following_usernames(db, username):
                    print(name)
            else:
                fetch_usernames.append(username)

        if not fetch_usernames:
            return

        async with browser_session(headless=not live, extensions=extensions, ublock=ublock) as (_browser, tab):
            cookies = await instagram_cookie_values(tab)
            for username in fetch_usernames:
                await _do_fetch(username, db, cookies)
    finally:
        db.close()


def _dump_following(urls: list[str]) -> None:
    """Print stored following usernames without fetching (legacy compat)."""
    db = init_db()
    try:
        for url in urls:
            if not url.startswith("@") or len(url) == 1:
                raise ValueError("get-following expects @profile")
            for username in stored_following_usernames(db, url[1:]):
                print(username)
    finally:
        db.close()
