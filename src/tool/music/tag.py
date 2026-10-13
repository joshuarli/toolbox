"""Fingerprint an audio file, look up its MusicBrainz metadata via AcoustID,
and write selected tags to the file with mutagen.

Also supports a --read mode to inspect existing tags without any network call.
"""

import argparse
import os
import re
import sys
import unicodedata
from collections import Counter

from tool.music.api.acoustid import candidates, fetch_acoustid_metadata, get_audio_fingerprint
from tool.music.api.musicbrainz import MusicBrainzRequestError, enrich
from tool.music.api.musicbrainz import search as mb_search
from tool.music.metadata import Candidate, Metadata, show
from tool.music.tags import is_writable, read_tags, write_tags
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


def _fallback_search(filepath: str, current: Metadata) -> list[Candidate]:
    """Fallback when AcoustID fingerprint lookup returns no matches.

    Strategy:
    1. Search MusicBrainz using file metadata (title, artist).
    2. Search MusicBrainz using a normalized filename as the song title.
    """
    # Strategy 1: metadata from file tags.
    # Only recording + artist — the album name from file tags rarely matches
    # MusicBrainz canonical release titles, so including it blocks valid hits.
    query_parts = []
    if current.title:
        query_parts.append(f"recording:({current.title})")
    if current.artist:
        query_parts.append(f"artist:({current.artist})")

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


def _rank(cands: list[Candidate], current: Metadata) -> list[Candidate]:
    """Best first: score, then release title matching the file's album, then plain
    (non-compilation/live/...) release groups, then earliest date."""
    album = (current.album or "").casefold()
    return sorted(
        cands,
        key=lambda c: (
            -round(c.score, 2),
            bool(album) and (c.meta.album or "").casefold() != album,
            len(c.meta.releasetype) > 1,
            c.meta.date or "9999",
        ),
    )


def _edition(m: Metadata) -> str:
    """What distinguishes this release from others carrying the same recording."""
    if not m.album:
        return ""
    detail = [p for p in (m.date, m.releasecountry, m.media) if p]
    if m.discnumber and (int(m.totaldiscs or 0) > 1 or m.discnumber != "1"):
        detail.append(f"disc {m.discnumber}/{m.totaldiscs}" if m.totaldiscs else f"disc {m.discnumber}")
    if m.tracknumber:
        detail.append(f"#{m.tracknumber}")
    return f"[{m.album}" + (f" · {' · '.join(detail)}" if detail else "") + "]"


def format_match_line(idx: int, cand: Candidate, *, show_id: bool = False) -> str:
    """Format one match as a single summary line."""
    m = cand.meta
    line = f"  #{idx}  {cand.score * 100:3.0f}%  {m.title or '?'} — {m.artist or '?'}"
    if edition := _edition(m):
        line += f"  {edition}"
    if show_id and m.musicbrainz_albumid:
        line += dim(f" {m.musicbrainz_albumid[:8]}")
    return line


def _match_lines(cands: list[Candidate]) -> list[str]:
    """Summary lines for all candidates; the release id is appended where editions look identical."""
    keys = [(c.meta.title, c.meta.artist, _edition(c.meta)) for c in cands]
    counts = Counter(keys)
    return [format_match_line(i, c, show_id=counts[k] > 1) for i, (c, k) in enumerate(zip(cands, keys, strict=True), 1)]


def _print_tags(filepath: str) -> None:
    """Pretty-print all tags from *filepath*."""
    values = read_tags(filepath).to_values()
    if not values:
        print("  (no tags)")
        return

    key_w = max(len(k) for k in values)
    for key, vals in values.items():
        print(f"  {bold(key):<{key_w + 11}s} {show(key, vals)}")


def format_diff(current: Metadata, new: Metadata) -> list[str]:
    """Return formatted diff lines for the fields *new* would change.

    Each changed field produces two lines — the old value (red) on the first,
    then the new value (green) on the next at the same column.

        key      old_val
               → new_val
    """
    cur, upd = current.to_values(), new.to_values()
    changed = [k for k, v in upd.items() if cur.get(k) != v]
    if not changed:
        return []

    key_w = max(len(k) for k in changed)
    # indent for the arrow line: 4 spaces + key + 2 spaces = same column as old value
    indent = " " * (4 + key_w + 2)

    lines: list[str] = []
    for key in changed:
        cur_display = show(key, cur[key]) if key in cur else "(none)"
        arrow = "→" if key in cur else "+"
        lines.append(f"    {key:<{key_w}s}  {colored(cur_display, 1)}")
        lines.append(f"{indent}{arrow} {colored(show(key, upd[key]), 2)}")

    return lines


_VISIBLE = 12  # candidate rows shown at once; the list scrolls with the cursor


def interactive_select(cands: list[Candidate], filepath: str) -> int:
    """Arrow-key navigable match selector with live diff.

    Returns the selected index into *cands*.
    """
    current = read_tags(filepath)
    match_lines = _match_lines(cands)

    def render(idx: int) -> list[str]:
        start = min(max(0, idx - _VISIBLE // 2), max(0, len(cands) - _VISIBLE))
        end = min(len(cands), start + _VISIBLE)
        lines: list[str] = [""]
        if start:
            lines.append(dim(f"    ↑ {start} more"))
        lines.extend(bold(f"▶{match_lines[i]}") if i == idx else f"  {match_lines[i]}" for i in range(start, end))
        if end < len(cands):
            lines.append(dim(f"    ↓ {len(cands) - end} more"))
        lines.append("")

        diff = format_diff(current, cands[idx].meta)
        if diff:
            lines.append("  Changes (before MusicBrainz enrichment):")
            lines.extend(diff)
        else:
            lines.append("  (no changes — tags already match)")
        lines.append("")
        lines.append(dim("  ↑/↓ navigate  Enter write  s skip  q quit"))
        return lines

    return select_interactive(render, len(cands))


def _fallback_select(cands: list[Candidate]) -> int:
    """Non-TTY fallback: print numbered list and prompt for input."""
    for line in _match_lines(cands):
        print(line)

    print()
    try:
        choice = input(f"Select [1-{len(cands)}, q=quit]: ").strip()
    except EOFError, KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(0)

    if choice.lower() == "q":
        print("Aborted.")
        sys.exit(0)

    try:
        idx = int(choice) - 1
        if 0 <= idx < len(cands):
            return idx
    except ValueError:
        pass

    print(f"Invalid selection: {choice}")
    sys.exit(1)


AUTOTAG_MIN_SCORE = 0.9


def autotag(filepath: str) -> Metadata | None:
    """Tag *filepath* without prompting, but only where the fingerprint match is unambiguous.

    Every AcoustID match scoring at least `AUTOTAG_MIN_SCORE` must be the same recording.  If
    that recording is on exactly one release, and the file has no album or one of the same
    title, the full release identity is written; otherwise only recording-level identity is,
    leaving the album and the release choice to an interactive `tag` run.
    Returns what was written, or None if the file was left alone.  Does not rename the file.
    """
    if not is_writable(filepath):
        return None
    duration, fingerprint = get_audio_fingerprint(filepath)
    if not (duration and fingerprint):
        return None
    strong = [
        c for c in candidates(fetch_acoustid_metadata(duration, fingerprint) or []) if c.score >= AUTOTAG_MIN_SCORE
    ]
    if len({c.meta.musicbrainz_recordingid for c in strong}) != 1:
        return None

    best = max(strong, key=lambda c: c.score).meta
    album = read_tags(filepath).album
    sole_release = len({c.meta.musicbrainz_albumid for c in strong}) == 1 and best.musicbrainz_albumid
    if sole_release and (not album or album.casefold() == (best.album or "").casefold()):
        selection = best
    else:
        selection = Metadata(musicbrainz_recordingid=best.musicbrainz_recordingid, acoustid_id=best.acoustid_id)

    metadata = enrich(selection)
    if not metadata.musicbrainz_artistid:  # MusicBrainz lookup failed; nothing durable to write
        return None
    metadata.acoustid_fingerprint = fingerprint
    write_tags(filepath, metadata)
    return metadata


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

    current = read_tags(args.file)
    if not args.force and current.is_complete():
        print(f"Already identified in MusicBrainz, skipping {args.file}. Use --force to override.")
        return

    print(f"Analyzing {args.file}...")
    duration, fingerprint = get_audio_fingerprint(args.file)

    if not (duration and fingerprint):
        return

    cands = candidates(fetch_acoustid_metadata(duration, fingerprint) or [])

    if not cands:
        try:
            cands = _fallback_search(args.file, current)
        except MusicBrainzRequestError as exc:
            cause = f": {exc.__cause__}" if exc.__cause__ else ""
            print(f"error: {exc}{cause}", file=sys.stderr)
            sys.exit(1)
        if not cands:
            print("No matches found for this audio fingerprint.")
            return
    cands = _rank(cands, current)

    if not sys.stdout.isatty():
        selected_idx = _fallback_select(cands)
    else:
        selected_idx = interactive_select(cands, args.file)

    metadata = enrich(cands[selected_idx].meta)
    metadata.acoustid_fingerprint = fingerprint
    diff = format_diff(current, metadata)

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
    if metadata.title:
        dirname = os.path.dirname(args.file)
        ext = os.path.splitext(args.file)[1]
        safe_title = metadata.title.replace("/", "-").replace("\x00", "")
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
