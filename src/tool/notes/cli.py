"""Operate on the hardcoded Obsidian notes vault."""

from __future__ import annotations

import argparse

from tool.notes import clean, localize


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.description = "Clean and localize images in the Obsidian notes vault."
    sub = parser.add_subparsers(dest="notes_cmd", required=True)

    p_clean = sub.add_parser(
        "clean",
        help="move unreferenced z-images/ files and duplicate notes to the Trash",
    )
    clean.configure_parser(p_clean)
    p_clean.set_defaults(handler=clean.run)

    p_localize = sub.add_parser(
        "localize",
        help="download remote images into z-images/ as WebP",
    )
    localize.configure_parser(p_localize)
    p_localize.set_defaults(handler=localize.run)


def build_parser() -> argparse.ArgumentParser:
    """Standalone ``notes`` parser, for direct testing outside the ``tool`` CLI."""
    parser = argparse.ArgumentParser(prog="notes")
    configure_parser(parser)
    return parser
