from __future__ import annotations

import shadowdriver


async def _login(ublock: bool = True) -> None:
    profile_dir = await shadowdriver.default_profile_dir()
    print(f"Profile: {profile_dir}", flush=True)
    print("Log in to Instagram, then close the window.", flush=True)

    extra_args: list[str] = []
    if ublock:
        try:
            ublock_dir = await shadowdriver.ensure_ublock_origin()
            extra_args.append(f"--load-extension={ublock_dir}")
        except shadowdriver.UblockError as exc:
            print(f"uBlock Origin setup failed: {exc}", flush=True)

    try:
        # LaunchServices makes the window foreground so it receives
        # keyboard input. shadowdriver writes a launch marker and
        # distinguishes a crash from a manual close on exit.
        await shadowdriver.launch_browser_window(
            profile_dir=profile_dir,
            url="https://www.instagram.com/",
            extra_args=extra_args,
        )
        await shadowdriver.wait_browser_window_exit(
            profile_dir=profile_dir,
        )
    finally:
        await shadowdriver.kill()
