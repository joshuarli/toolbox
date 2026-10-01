"""Unified ig entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from typing import cast

import uvloop

from tool.browser_require import BrowserExtraMissingError
from tool.ig.browser import (
    BrowserCrashedError,
    BrowserInUseError,
    set_network_verbose,
)
from tool.ig.cli.build import _build_site
from tool.ig.cli.doctor import _doctor
from tool.ig.cli.fingerprint import _fingerprint, _fingerprint_warnings, _host_fingerprint
from tool.ig.cli.follow import _follow
from tool.ig.cli.following import _get_following
from tool.ig.cli.get import _get
from tool.ig.cli.get_post import _get_post
from tool.ig.cli.import_db import ImportDbConflictError, ImportDbError, _import_db
from tool.ig.cli.info import _info, _info_usernames
from tool.ig.cli.kill import _kill
from tool.ig.cli.login import _login
from tool.ig.cli.remove import _remove
from tool.ig.cli.scan import _scan
from tool.ig.cli.update import _update

__all__ = [
    "_fingerprint",
    "_fingerprint_warnings",
    "_get",
    "_get_following",
    "_get_post",
    "_host_fingerprint",
    "build_parser",
    "configure_parser",
    "main",
    "run",
]


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _common_args(args: argparse.Namespace) -> tuple[list[str] | None, bool]:
    """Read the top-level ``--extension``/``--no-ublock``/``-v`` flags shared by every leaf command."""
    set_network_verbose(args.verbose)
    extension_arg = args.extension
    assert isinstance(extension_arg, list)
    extensions = cast("list[str]", extension_arg) or None
    ublock = args.ublock
    assert isinstance(ublock, bool)
    return extensions, cast(bool, ublock)


def cmd_login(args: argparse.Namespace) -> None:
    _extensions, ublock = _common_args(args)
    uvloop.run(_login(ublock=ublock))


def cmd_kill(_args: argparse.Namespace) -> None:
    uvloop.run(_kill())


def cmd_doctor(_args: argparse.Namespace) -> None:
    uvloop.run(_doctor())


def cmd_fingerprint(args: argparse.Namespace) -> None:
    extensions, ublock = _common_args(args)
    uvloop.run(_fingerprint(live=args.live, extensions=extensions, ublock=ublock))


def cmd_remove(args: argparse.Namespace) -> None:
    username = args.username.lstrip("@")
    _remove(username, dry_run=args.dry_run, media_dir=args.media_dir, saved=args.saved)


def cmd_info(args: argparse.Namespace) -> None:
    if args.usernames:
        _info_usernames()
    else:
        _info(media_dir=args.media_dir, images=args.images)


def cmd_import_db(args: argparse.Namespace) -> None:
    try:
        _import_db(args.source)
    except ImportDbConflictError as exc:
        args.parser.error("import-db preflight failed:\n  " + "\n  ".join(exc.conflicts))
    except ImportDbError as exc:
        args.parser.error(str(exc))


def cmd_scan(args: argparse.Namespace) -> None:
    extensions, ublock = _common_args(args)
    uvloop.run(_scan(args.url, live=args.live, full=args.full, saved=args.saved, extensions=extensions, ublock=ublock))


def cmd_get_post(args: argparse.Namespace) -> None:
    extensions, ublock = _common_args(args)
    uvloop.run(
        _get_post(
            args.target,
            live=args.live,
            pace=args.pace,
            media_dir=args.media_dir,
            extensions=extensions,
            ublock=ublock,
        )
    )


def cmd_follow(args: argparse.Namespace) -> None:
    extensions, ublock = _common_args(args)
    uvloop.run(_follow(args.urls, live=args.live, extensions=extensions, ublock=ublock))


def cmd_get_following(args: argparse.Namespace) -> None:
    extensions, ublock = _common_args(args)
    uvloop.run(
        _get_following(
            args.url,
            live=args.live,
            refresh=args.refresh,
            refresh_all=args.refresh and not args.url,
            extensions=extensions,
            ublock=ublock,
        )
    )


def cmd_build(args: argparse.Namespace) -> None:
    _build_site(media_dir=args.media_dir)


def cmd_update(args: argparse.Namespace) -> None:
    extensions, ublock = _common_args(args)
    uvloop.run(
        _update(
            live=args.live,
            full=args.full,
            pace=args.pace,
            media_dir=args.media_dir,
            jobs=args.jobs,
            extensions=extensions,
            ublock=ublock,
        )
    )


def cmd_get(args: argparse.Namespace) -> None:
    extensions, ublock = _common_args(args)
    uvloop.run(
        _get(
            args.url,
            live=args.live,
            pace=args.pace,
            media_dir=args.media_dir,
            saved=args.saved,
            jobs=args.jobs,
            extensions=extensions,
            ublock=ublock,
        )
    )


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.description = "Log in to, scan, and archive Instagram profiles and posts."
    parser.add_argument(
        "--extension",
        action="append",
        default=[],
        metavar="PATH",
        help="load an unpacked Chrome extension (repeatable, Chromium only)",
    )
    parser.add_argument(
        "--no-ublock",
        action="store_false",
        dest="ublock",
        default=True,
        help="disable uBlock Origin (Chromium only)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="show per-URL uBlock block notices and other network noise",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    login_cmd = sub.add_parser("login", help="open a browser window to log in to Instagram")
    login_cmd.set_defaults(handler=cmd_login)
    kill_cmd = sub.add_parser("kill", help="stop the background browser process")
    kill_cmd.set_defaults(handler=cmd_kill)
    doctor_cmd = sub.add_parser("doctor", help="print browser profile, process, port, and log diagnostics")
    doctor_cmd.set_defaults(handler=cmd_doctor)
    fingerprint_cmd = sub.add_parser("fingerprint", help="print the browser fingerprint used by the scraper")
    fingerprint_cmd.set_defaults(handler=cmd_fingerprint)
    fingerprint_cmd.add_argument("--live", action="store_true", help="show the browser while probing the fingerprint")
    get_cmd = sub.add_parser("get", help="download pending scanned profile posts")
    get_cmd.set_defaults(handler=cmd_get)
    get_post_cmd = sub.add_parser("get-post", help="download a single post by shortid (debug/direct path)")
    get_post_cmd.set_defaults(handler=cmd_get_post)
    get_post_cmd.add_argument("target", help="post shortid or /p/<shortid>/ or /reel/<shortid>/ URL")
    get_post_cmd.add_argument("--live", action="store_true", help="show the browser window while scraping")
    get_post_cmd.add_argument("--media-dir", default="site/images", help="directory for downloaded media files")
    get_post_cmd.add_argument(
        "--pace",
        choices=["fast"],
        default="fast",
        help="scraping aggressiveness (default: fast)",
    )
    following_cmd = sub.add_parser("get-following", help="fetch a profile's following list into SQLite")
    following_cmd.set_defaults(handler=cmd_get_following)
    follow_cmd = sub.add_parser("follow", help="follow one or more Instagram accounts")
    follow_cmd.add_argument("urls", nargs="+", help="@account(s) to follow")
    follow_cmd.add_argument("--live", action="store_true", help="show the browser window")
    follow_cmd.set_defaults(handler=cmd_follow)
    update_cmd = sub.add_parser(
        "update", help="rescan all known profiles and saved posts, then download new or incomplete posts"
    )
    update_cmd.set_defaults(handler=cmd_update)
    update_cmd.add_argument("--live", action="store_true", help="show the browser window while scraping")
    update_cmd.add_argument(
        "--full", action="store_true", help="rescan whole profiles instead of stopping at known posts"
    )
    update_cmd.add_argument("--media-dir", default="site/images", help="directory for downloaded media files")
    update_cmd.add_argument("-j", "--jobs", type=_positive_int, default=1, help="number of concurrent post downloads")
    update_cmd.add_argument(
        "--pace",
        choices=["fast"],
        default="fast",
        help="scraping aggressiveness (default: fast)",
    )
    scan_cmd = sub.add_parser("scan", help="scan a profile grid into post stubs")
    scan_cmd.set_defaults(handler=cmd_scan)
    info_cmd = sub.add_parser("info", help="print database summary of scanned profiles")
    info_cmd.set_defaults(handler=cmd_info)
    import_cmd = sub.add_parser("import-db", help="import profiles from another instagram.db")
    import_cmd.add_argument("source", help="path to the source instagram.db")
    import_cmd.set_defaults(handler=cmd_import_db, parser=import_cmd)
    info_cmd.add_argument("--media-dir", default="site/images", help="directory containing downloaded media files")
    info_cmd.add_argument(
        "-i", "--images", action="store_true", help="scan filesystem to check which posts have downloaded images"
    )
    info_cmd.add_argument(
        "--usernames", action="store_true", help="dump only scanned usernames (no saved accounts), space-delimited"
    )
    rm_cmd = sub.add_parser("remove", help="delete a profile and all its images from the DB")
    rm_cmd.set_defaults(handler=cmd_remove)
    rm_cmd.add_argument("username", help="username to remove (with or without @)")
    rm_cmd.add_argument("--dry-run", action="store_true", help="list what would be removed without deleting")
    rm_cmd.add_argument("--media-dir", default="site/images", help="directory containing downloaded media files")
    rm_cmd.add_argument("--saved", action="store_true", help="remove saved-post membership for this profile")
    get_cmd.add_argument("url", nargs="*", help="@profile")
    get_cmd.add_argument("--live", action="store_true", help="show the browser window while scraping")
    get_cmd.add_argument("--saved", action="store_true", help="download pending saved posts for a profile")
    get_cmd.add_argument("--media-dir", default="site/images", help="directory for downloaded media files")
    get_cmd.add_argument("-j", "--jobs", type=_positive_int, default=1, help="number of concurrent post downloads")
    get_cmd.add_argument(
        "--pace",
        choices=["fast"],
        default="fast",
        help="scraping aggressiveness (default: fast)",
    )
    following_cmd.add_argument(
        "url", nargs="*", default=[], help="@username (omit with --refresh to fetch all profiles)"
    )
    following_cmd.add_argument("--live", action="store_true", help="show the browser window while fetching cookies")
    following_cmd.add_argument("--refresh", action="store_true", help="force a fresh fetch from Instagram")
    scan_cmd.add_argument("url", nargs="*", help="@profile")
    scan_cmd.add_argument("--live", action="store_true", help="show the browser window while scanning")
    scan_cmd.add_argument("--saved", action="store_true", help="scan a profile's saved posts")
    scan_cmd.add_argument(
        "--full", action="store_true", help="rescan the whole profile instead of stopping at known posts"
    )
    build_cmd = sub.add_parser("build", help="build a static archive site from the database")
    build_cmd.add_argument("--media-dir", default="site/images", help="directory containing downloaded media files")
    build_cmd.set_defaults(handler=cmd_build)


def build_parser() -> argparse.ArgumentParser:
    """Standalone ``ig`` parser, for direct testing outside the ``tool`` CLI."""
    parser = argparse.ArgumentParser(prog="ig")
    configure_parser(parser)
    return parser


async def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    extensions = args.extension or None
    ublock = args.ublock
    set_network_verbose(args.verbose)
    if args.cmd == "login":
        await _login(ublock=ublock)
    elif args.cmd == "kill":
        await _kill()
    elif args.cmd == "doctor":
        await _doctor()
    elif args.cmd == "fingerprint":
        await _fingerprint(live=args.live, extensions=extensions, ublock=ublock)
    elif args.cmd == "remove":
        username = args.username.lstrip("@")
        _remove(username, dry_run=args.dry_run, media_dir=args.media_dir, saved=args.saved)
    elif args.cmd == "info":
        if args.usernames:
            _info_usernames()
        else:
            _info(media_dir=args.media_dir, images=args.images)
    elif args.cmd == "import-db":
        try:
            _import_db(args.source)
        except ImportDbConflictError as exc:
            parser.error("import-db preflight failed:\n  " + "\n  ".join(exc.conflicts))
        except ImportDbError as exc:
            parser.error(str(exc))
    elif args.cmd == "scan":
        await _scan(args.url, live=args.live, full=args.full, saved=args.saved, extensions=extensions, ublock=ublock)
    elif args.cmd == "get-post":
        await _get_post(
            args.target,
            live=args.live,
            pace=args.pace,
            media_dir=args.media_dir,
            extensions=extensions,
            ublock=ublock,
        )
    elif args.cmd == "follow":
        await _follow(
            args.urls,
            live=args.live,
            extensions=extensions,
            ublock=ublock,
        )
    elif args.cmd == "get-following":
        await _get_following(
            args.url,
            live=args.live,
            refresh=args.refresh,
            refresh_all=args.refresh and not args.url,
            extensions=extensions,
            ublock=ublock,
        )
    elif args.cmd == "build":
        _build_site(media_dir=args.media_dir)
    elif args.cmd == "update":
        await _update(
            live=args.live,
            full=args.full,
            pace=args.pace,
            media_dir=args.media_dir,
            jobs=args.jobs,
            extensions=extensions,
            ublock=ublock,
        )
    elif args.cmd == "get":
        await _get(
            args.url,
            live=args.live,
            pace=args.pace,
            media_dir=args.media_dir,
            saved=args.saved,
            jobs=args.jobs,
            extensions=extensions,
            ublock=ublock,
        )


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[logging.StreamHandler()])
    try:
        uvloop.run(main())
    except BrowserInUseError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    except BrowserCrashedError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    except BrowserExtraMissingError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except KeyboardInterrupt:
        pass
