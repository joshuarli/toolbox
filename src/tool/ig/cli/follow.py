from __future__ import annotations

import asyncio
import re

from tool.ig.browser import browser_session, navigate

_FOLLOW_JS = """
(() => {
    const candidates = document.querySelectorAll('button, div[role="button"], a[role="button"]');
    for (const el of candidates) {
        const text = el.textContent.trim();
        if (text === 'Follow' || text === 'Follow back') {
            el.click();
            return 'followed';
        }
        if (text === 'Following' || text === 'Requested') {
            return 'already_following';
        }
    }
    return 'not_found';
})()
"""


async def _follow(
    urls: list[str],
    *,
    live: bool = False,
    extensions: list[str] | None = None,
    ublock: bool = True,
) -> None:
    usernames: list[str] = []
    for url in urls:
        if not url.startswith("@") or len(url) == 1:
            raise ValueError("follow expects @account")
        username = url[1:]
        if not re.fullmatch(r"[A-Za-z0-9._]+", username):
            raise ValueError(f"invalid username: {username}")
        usernames.append(username)

    if not usernames:
        print("no accounts specified", flush=True)
        return

    async with browser_session(headless=not live, extensions=extensions, ublock=ublock) as (_browser, tab):
        for username in usernames:
            profile_url = f"https://www.instagram.com/{username}/"
            print(f"follow: @{username}", flush=True)
            await tab.activate()
            await navigate(tab, profile_url, timeout=20.0)
            await asyncio.sleep(2.0)

            result = await tab.evaluate(_FOLLOW_JS)
            if result == "followed":
                print(f"  -> now following @{username}", flush=True)
            elif result == "already_following":
                print(f"  -> already following @{username}", flush=True)
            else:
                print(f"  -> follow button not found for @{username}", flush=True)
            await asyncio.sleep(1.0)
