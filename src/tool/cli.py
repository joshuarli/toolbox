from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import preflight, sd


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
    sd_parser.set_defaults(handler=sd.run)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = args.handler
    try:
        args.preflight = preflight.run_preflight()
        return int(handler(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except (preflight.PreflightError, sd.SdError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
