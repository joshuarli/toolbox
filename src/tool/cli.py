from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from tool import sd
from tool.browser_require import BrowserExtraMissingError
from tool.ig import cli as ig_cli
from tool.ig.browser import BrowserCrashedError, BrowserInUseError
from tool.movies import cli as movies_cli
from tool.music import cli as music_cli
from tool.notes import cli as notes_cli
from tool.notes.common import NotesError
from tool.sd import preflight


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tool",
        description="Focused personal command-line tools.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sd_parser = subparsers.add_parser(
        "sd",
        help="destructively qualify SD/removable media with fio",
    )
    sd.configure_parser(sd_parser)
    sd_parser.set_defaults(handler=sd.run, needs_preflight=True)

    movies_parser = subparsers.add_parser(
        "movies",
        help="encode/audit video files and manage a Letterboxd-backed movie library",
    )
    movies_cli.configure_parser(movies_parser)

    music_parser = subparsers.add_parser(
        "music",
        help="scan/tag/transcode a local audio library",
    )
    music_cli.configure_parser(music_parser)

    ig_parser = subparsers.add_parser(
        "ig",
        help="log in to, scan, and archive Instagram profiles and posts",
    )
    ig_cli.configure_parser(ig_parser)

    notes_parser = subparsers.add_parser(
        "notes",
        help="clean and localize the Obsidian notes vault",
    )
    notes_cli.configure_parser(notes_parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = args.handler
    try:
        if getattr(args, "needs_preflight", False):
            args.preflight = preflight.run_preflight()
        result = handler(args)
        return int(result) if result is not None else 0
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except (BrowserInUseError, BrowserCrashedError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (preflight.PreflightError, sd.SdError, NotesError, BrowserExtraMissingError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
