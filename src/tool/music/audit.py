"""Report how much of a music library still needs tagging.

Read-only: reads tags through the same adapter as `scan` and `tag`, makes no network
requests, and writes nothing.  Each file gets a state based on the identity fields
`Metadata.is_complete` requires, so the summary answers how much work `music tag`
has left and where it is.
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

from tool.music.constants import BLUE, GOLD, GREEN, GREY, RED
from tool.music.metadata import Metadata
from tool.music.scan import collect_files
from tool.music.tags import is_writable, read_tags
from tool.music.ui import bold, colored


class State(StrEnum):
    COMPLETE = "complete"
    MISSING_IDENTITY = "missing-identity"
    MISSING_ALBUM = "missing-album"
    UNTAGGED = "untagged"
    PARTIAL = "partial"


STATE_ORDER = [
    State.COMPLETE,
    State.MISSING_IDENTITY,
    State.MISSING_ALBUM,
    State.UNTAGGED,
    State.PARTIAL,
]

STATE_COLOR = {
    State.COMPLETE: GREEN,
    State.MISSING_IDENTITY: GOLD,
    State.MISSING_ALBUM: BLUE,
    State.UNTAGGED: RED,
    State.PARTIAL: RED,
}


@dataclass(frozen=True)
class FileAudit:
    path: str
    group: str
    format: str
    state: State
    writable: bool
    title: bool
    artist: bool
    album: bool
    recording_id: bool
    release_id: bool


def classify(meta: Metadata) -> State:
    """Bucket a file by the identity it carries.

    `missing-album` means the recording is identified but the claimed album is not;
    a recording with no album needs no release identity and is complete.
    """
    if meta.is_complete():
        return State.COMPLETE
    if not bool(meta.title or meta.artist or meta.album):
        return State.UNTAGGED
    if not bool(meta.title and meta.artist):
        return State.PARTIAL
    if bool(meta.musicbrainz_recordingid):
        return State.MISSING_ALBUM
    return State.MISSING_IDENTITY


def audit_file(filepath: Path, display_path: str) -> FileAudit:
    meta = read_tags(str(filepath))
    parts = Path(display_path).parts
    return FileAudit(
        path=display_path,
        group=parts[0] if len(parts) > 1 else ".",
        format=Path(display_path).suffix.lower().lstrip(".") or "?",
        state=classify(meta),
        writable=is_writable(str(filepath)),
        title=bool(meta.title),
        artist=bool(meta.artist),
        album=bool(meta.album),
        recording_id=bool(meta.musicbrainz_recordingid),
        release_id=bool(meta.musicbrainz_albumid),
    )


def _summary_rows(state_counts: Counter[State], total: int) -> list[str]:
    label_w = max(len(s.value) for s in STATE_ORDER)
    lines = [colored(f"  {'STATE':<{label_w}s}  {'COUNT':>6s}  SHARE", 15, bold=True)]
    for state in STATE_ORDER:
        count = state_counts[state]
        share = f"{count / total:6.1%}" if total else "     -"
        lines.append(f"  {colored(f'{state.value:<{label_w}s}', STATE_COLOR[state], bold=True)}  {count:6d}  {share}")
    return lines


def _format_rows(rows: list[FileAudit]) -> list[str]:
    counts = Counter(r.format for r in rows)
    writable = defaultdict(bool)
    for r in rows:
        writable[r.format] = writable[r.format] or r.writable
    format_w = max(len(f) for f in counts)
    lines = [colored(f"  {'FORMAT':<{format_w}s}  {'COUNT':>6s}  TAGGABLE", 15, bold=True)]
    for fmt, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        taggable = "yes" if writable[fmt] else colored("no", RED, bold=True)
        lines.append(f"  {fmt:<{format_w}s}  {count:6d}  {taggable}")
    return lines


def _group_rows(rows: list[FileAudit]) -> list[str]:
    totals: Counter[str] = Counter(r.group for r in rows)
    by_state: defaultdict[str, Counter[State]] = defaultdict(Counter)
    for r in rows:
        by_state[r.group][r.state] += 1

    group_w = max(len(g) for g in totals)
    counts = [str(totals[g]) for g in totals]
    count_w = max(len(c) for c in counts)
    state_w = max(len(s.value) for s in STATE_ORDER)

    header = f"  {'GROUP':<{group_w}s}  {'FILES':>{count_w}s}"
    for state in STATE_ORDER:
        header += f"  {state.value.upper():>{state_w}s}"
    lines = [colored(header, 15, bold=True)]
    for group in sorted(totals, key=lambda g: (-totals[g], g)):
        line = f"  {group:<{group_w}s}  {totals[group]:>{count_w}d}"
        for state in STATE_ORDER:
            line += f"  {by_state[group][state]:>{state_w}d}"
        lines.append(line)
    return lines


def _file_rows(rows: list[FileAudit]) -> list[str]:
    state_w = max(len(s.value) for s in STATE_ORDER)
    lines = []
    for state in STATE_ORDER:
        if state is State.COMPLETE:
            continue
        for r in sorted((r for r in rows if r.state is state), key=lambda r: r.path):
            note = "" if r.writable else colored(f"  (convert: {r.format} is not taggable in place)", GREY)
            lines.append(f"  {colored(f'{state.value:<{state_w}s}', STATE_COLOR[state])}  {r.path}{note}")
    return lines


def report(rows: list[FileAudit], roots: list[str]) -> None:
    total = len(rows)
    state_counts = Counter(r.state for r in rows)
    complete = state_counts[State.COMPLETE]
    unwritable = sum(1 for r in rows if not r.writable)

    heading = "AUDIT  " + ", ".join(roots)
    print(bold(heading))
    summary = f"  {total} files — {complete} complete ({complete / total:.1%}), {total - complete} need work"
    if unwritable:
        summary += f"; {unwritable} not taggable in place"
    print(colored(summary, GREY))

    print()
    print("\n".join(_summary_rows(state_counts, total)))

    print()
    print("\n".join(_format_rows(rows)))

    print()
    print("\n".join(_group_rows(rows)))

    if complete == total:
        return
    print()
    print(bold(f"  NEEDS WORK ({total - complete})"))
    print("\n".join(_file_rows(rows)))


def configure_parser(p: argparse.ArgumentParser) -> None:
    p.description = (
        "Report tag completeness for a music library: how many files carry durable "
        "MusicBrainz identity, which need a lookup, and where. Read-only."
    )
    p.add_argument("paths", nargs="+", help="directories or files to audit")
    p.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4, help="number of parallel workers")
    p.add_argument("--json", action="store_true", help="print machine-readable JSON instead of the report")


def run(args: argparse.Namespace) -> None:
    files = collect_files(args.paths)
    if not files:
        print("No audio files found.")
        return

    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        rows = list(ex.map(lambda f: audit_file(*f), files))

    if args.json:
        json.dump(
            {
                "counts": {state.value: sum(1 for r in rows if r.state is state) for state in STATE_ORDER},
                "files": [asdict(r) for r in rows],
            },
            sys.stdout,
            indent=1,
        )
        print()
        return

    report(rows, args.paths)


def main() -> None:
    parser = argparse.ArgumentParser()
    configure_parser(parser)
    run(parser.parse_args())
