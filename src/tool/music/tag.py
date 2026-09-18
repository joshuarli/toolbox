"""Fingerprint an audio file, look up its MusicBrainz metadata via AcoustID,
and write selected tags to the file with mutagen.

Also supports a --read mode to inspect existing tags without any network call.
"""

import argparse
import os
import re
import sys
import unicodedata
from typing import Any

from tool.music.api.acoustid import extract_metadata, fetch_acoustid_metadata, get_audio_fingerprint
from tool.music.api.musicbrainz import fetch_recording
from tool.music.api.musicbrainz import search as mb_search
from tool.music.tags import TAG_FIELDS, read_tags, write_tags
from tool.music.ui import bold, colored, dim, select_interactive

_YT_ID_RE = re.compile(r"\s*\[[A-Za-z0-9_-]{11}\]$")
_TRACK_NUM_RE = re.compile(r"^\d{1,3}\s*[-–—]\s*")


def _normalize_filename(filepath: str) -> str:
    """Extract a plausible song title from a filename.

    Removes the YouTube ID + extension suffix, leading track number, and
    replaces fullwidth punctuation with ASCII equivalents.
    """
    name = os.path.splitext(os.path.basename(filepath))[0]

    # Remove [youtube-id] suffix if present (before the extension)
    name = _YT_ID_RE.sub("", name)

    # Remove leading track number (e.g. "09 - ")
    name = _TRACK_NUM_RE.sub("", name)

    # Normalize fullwidth / confusable punctuation to ASCII
    name = unicodedata.normalize("NFKC", name)

    # Collapse whitespace
    name = " ".join(name.split())

    return name.strip()


def _fallback_search(filepath: str, current_tags: dict[str, str]) -> list[dict[str, Any]]:
    """Fallback when AcoustID fingerprint lookup returns no matches.

    Strategy:
    1. Search MusicBrainz using file metadata (title, artist).
    2. Search MusicBrainz using a normalized filename as the song title.
    """
    # Strategy 1: metadata from file tags.
    # Only recording + artist — the album name from file tags rarely matches
    # MusicBrainz canonical release titles, so including it blocks valid hits.
    query_parts = []
    if current_tags.get("title"):
        query_parts.append(f"recording:({current_tags['title']})")
    if current_tags.get("artist"):
        query_parts.append(f"artist:({current_tags['artist']})")

    if query_parts:
        query = " AND ".join(query_parts)
        print("  No fingerprint match. Searching MusicBrainz with file metadata...")
        results = mb_search(query)
        if results:
            print(f"  Found {len(results)} result(s) via metadata search.")
            return results

    # Strategy 2: normalized filename as title (plain query, not phrase-locked)
    title = _normalize_filename(filepath)
    if title:
        print(f"  Searching MusicBrainz for: {title}")
        results = mb_search(title)
        if results:
            print(f"  Found {len(results)} result(s) via filename search.")
            return results

    return []


def format_match_line(idx: int, result: dict[str, Any]) -> str:
    """Format one match as a single summary line."""
    score = result.get("score", 0) * 100
    meta = extract_metadata(result)
    title = meta.get("title", "?")
    artist = meta.get("artist", "?")
    album = meta.get("album", "")
    year = meta.get("date", "")

    album_str = f" [{album}]" if album else ""
    year_str = f" ({year})" if year else ""

    return f"  #{idx}  {score:3.0f}%  {title} — {artist}{album_str}{year_str}"


def _print_tags(filepath: str) -> None:
    """Pretty-print all tags from *filepath*."""
    tags = read_tags(filepath)
    if not tags:
        print("  (no tags)")
        return

    key_w = max(len(k) for k in tags)
    for key in TAG_FIELDS:
        val = tags.get(key)
        if val:
            print(f"  {bold(key):<{key_w + 11}s} {val}")


def format_diff(current: dict[str, str], new: dict[str, str]) -> list[str]:
    """Return formatted diff lines for fields that differ between current and new tags.

    Each changed field produces two lines — the old value (red) on the first,
    then the new value (green) on the next at the same column.

        key      old_val
               → new_val
    """
    lines: list[str] = []
    all_keys = sorted(set(current.keys()) | set(new.keys()))
    if not all_keys:
        return lines

    key_w = max(len(k) for k in all_keys)
    # indent for the arrow line: 4 spaces + key + 2 spaces = same column as old value
    indent = " " * (4 + key_w + 2)

    for key in all_keys:
        cur_val = current.get(key)
        new_val = new.get(key)
        if cur_val == new_val:
            continue
        cur_display = cur_val or "(none)"
        new_display = new_val or "(none)"
        arrow = "+" if cur_val is None else "→"
        lines.append(f"    {key:<{key_w}s}  {colored(cur_display, 1)}")
        lines.append(f"{indent}{arrow} {colored(new_display, 2)}")

    return lines


def interactive_select(results: list[dict[str, Any]], filepath: str) -> int:
    """Arrow-key navigable match selector with live diff.

    Returns the selected index into *results*.
    """
    current_tags = read_tags(filepath)

    def render(idx: int) -> list[str]:
        lines: list[str] = [""]
        for i, r in enumerate(results):
            line = format_match_line(i + 1, r)
            if i == idx:
                lines.append(bold(f"▶{line}"))
            else:
                lines.append(f"  {line}")
        lines.append("")

        meta = extract_metadata(results[idx])
        diff = format_diff(current_tags, meta)
        if diff:
            lines.append("  Changes (AcoustID):")
            lines.extend(diff)
        else:
            lines.append("  (no changes — tags already match)")
        lines.append("")
        lines.append(dim("  ↑/↓ navigate  Enter write  s skip  q quit"))
        return lines

    return select_interactive(render, len(results))


def _fallback_select(results: list[dict[str, Any]]) -> int:
    """Non-TTY fallback: print numbered list and prompt for input."""
    for i, r in enumerate(results, 1):
        print(format_match_line(i, r))

    print()
    try:
        choice = input(f"Select [1-{len(results)}, q=quit]: ").strip()
    except EOFError, KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(0)

    if choice.lower() == "q":
        print("Aborted.")
        sys.exit(0)

    try:
        idx = int(choice) - 1
        if 0 <= idx < len(results):
            return idx
    except ValueError:
        pass

    print(f"Invalid selection: {choice}")
    sys.exit(1)


def configure_parser(p: argparse.ArgumentParser) -> None:
    p.description = (
        "Fingerprint an audio file with fpcalc and look up its "
        "MusicBrainz metadata via the AcoustID API. "
        "Requires ACOUSTID_API_KEY in the environment."
    )
    p.add_argument("file", help="audio file to fingerprint")
    p.add_argument("-y", "--yes", action="store_true", help="skip write confirmation")
    p.add_argument("-f", "--force", action="store_true", help="re-tag even if metadata is already complete")
    p.add_argument("--read", action="store_true", help="print current tags and exit (no network)")


def run(args: argparse.Namespace) -> None:
    if args.read:
        print(f"Tags in {args.file}:")
        _print_tags(args.file)
        return

    current_tags = read_tags(args.file)
    if not args.force:
        missing = [k for k in TAG_FIELDS if k not in current_tags]
        if not missing:
            print(f"All tags already complete, skipping {args.file}. Use --force to override.")
            return

    print(f"Analyzing {args.file}...")
    duration, fingerprint = get_audio_fingerprint(args.file)

    if not (duration and fingerprint):
        return

    results = fetch_acoustid_metadata(duration, fingerprint) or []

    # Filter out results with no extractable metadata
    results = [r for r in results if extract_metadata(r)]

    if not results:
        results = _fallback_search(args.file, current_tags)
        if not results:
            print("No matches found for this audio fingerprint.")
            return

    if not sys.stdout.isatty():
        selected_idx = _fallback_select(results)
    else:
        selected_idx = interactive_select(results, args.file)

    selected = results[selected_idx]
    metadata = extract_metadata(selected)
    recordings = selected.get("recordings", [])
    if recordings:
        mbid = recordings[0].get("id")
        if mbid:
            metadata.update(fetch_recording(mbid))

    current_tags = read_tags(args.file)
    diff = format_diff(current_tags, metadata)

    print()
    if not diff:
        print("No changes to write (tags already match).")
        return

    if not sys.stdout.isatty() and not args.yes:
        response = input("Write these tags? [y/N]: ").strip().lower()
        if response not in ("y", "yes"):
            print("Aborted.")
            return

    actual_path = write_tags(args.file, metadata)
    if "title" in metadata:
        dirname = os.path.dirname(args.file)
        ext = os.path.splitext(args.file)[1]
        safe_title = metadata["title"].replace("/", "-").replace("\x00", "")
        new_path = os.path.join(dirname, safe_title + ext)
        if new_path != actual_path:
            if os.path.exists(new_path):
                print(f"Warning: not renaming — {os.path.basename(new_path)} already exists.")
            else:
                os.rename(actual_path, new_path)
                actual_path = new_path
    print(f"Wrote tags to {actual_path}.")


def main() -> None:
    parser = argparse.ArgumentParser()
    configure_parser(parser)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
