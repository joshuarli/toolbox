from __future__ import annotations

import argparse
from collections.abc import Sequence
import sys

from . import sdqual


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tool",
        description="Focused personal command-line tools.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sdqual_parser = subparsers.add_parser(
        "sdqual",
        help="destructively qualify SD/removable media with fio",
    )
    sdqual.configure_parser(sdqual_parser)
    sdqual_parser.set_defaults(handler=sdqual.run)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = args.handler
    try:
        return int(handler(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except sdqual.SdqualError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
