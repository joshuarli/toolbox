"""Burn down a library's tagging queue.

Walks a directory, skips files that already carry durable identity, remuxes
non-taggable containers losslessly when their codec allows it, and autotags the
rest.  Ambiguous matches are reported for `music tag` rather than guessed.
"""

import argparse
import os
import shutil
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from tool.music.api.acoustid import (
    ACOUSTID_KEY_HINT,
    AcoustIDError,
    ensure_acoustid_key,
)
from tool.music.api.musicbrainz import MusicBrainzRequestError
from tool.music.constants import GOLD, GREEN, GREY, RED
from tool.music.scan import collect_files
from tool.music.tag import autotag_match
from tool.music.tags import is_writable, read_tags
from tool.music.transcode import (
    probe_audio,
    remux_extension,
    transcode,
    transcode_destination,
)
from tool.music.ui import bold, colored


@dataclass(frozen=True)
class Outcome:
    status: str
    path: str
    target: str | None = None
    artist: str | None = None
    title: str | None = None
    remuxed: bool = False
    method: str | None = None
    duration_delta: float | None = None


STATUS_COLOR = {
    "tagged-release": GREEN,
    "tagged-recording": GOLD,
    "needs-manual": GREY,
    "needs-conversion": RED,
    "failed": RED,
}
STATUS_TEXT = {
    "tagged-release": "tagged",
    "tagged-recording": "tagged recording",
    "needs-manual": "needs manual",
    "needs-conversion": "needs conversion",
    "failed": "failed",
}


def _remux(src: str) -> str | None:
    """Use an existing conversion, or create one, and return the taggable path."""
    dst = transcode_destination(src)
    if os.path.lexists(dst):
        return dst
    return transcode(src)


def process(src: str, *, delete_source: bool) -> Outcome:
    """Tag one file, converting its container first when that is lossless.

    A non-taggable source with no lossless container is reported instead of being
    re-encoded, so the queue never silently loses audio quality.
    """
    if read_tags(src).is_complete():
        return Outcome("already-complete", src)

    target = src
    remuxed = False
    if not is_writable(src):
        if remux_extension(src) is None:
            return Outcome("needs-conversion", src)
        converted = _remux(src)
        if converted is None:
            return Outcome("failed", src)
        target = converted
        remuxed = True
        if delete_source:
            if probe_audio(target) is None:
                return Outcome("failed", src)
            os.remove(src)

    converted_target = target if remuxed else None
    if remuxed and read_tags(target).is_complete():
        return Outcome("already-complete", src, target=converted_target, remuxed=remuxed)

    try:
        written = autotag_match(target)
    except SystemExit:
        return Outcome("failed", src, target=converted_target, remuxed=remuxed)

    if written is None:
        return Outcome("needs-manual", src, target=converted_target, remuxed=remuxed)
    meta = written.meta
    status = "tagged-release" if bool(meta.musicbrainz_albumid) else "tagged-recording"
    return Outcome(
        status,
        src,
        target=converted_target,
        artist=meta.artist,
        title=meta.title,
        remuxed=remuxed,
        method=written.method,
        duration_delta=written.duration_delta,
    )


def _print_outcome(outcome: Outcome) -> None:
    if outcome.status == "already-complete":
        return
    color = STATUS_COLOR[outcome.status]
    label = STATUS_TEXT[outcome.status]
    line = f"  {colored(f'{label:<17s}', color)} {outcome.path}"
    if outcome.remuxed and outcome.target is not None:
        line += f" → {outcome.target}"
    if outcome.status.startswith("tagged"):
        line += f"  {outcome.artist} — {outcome.title}"
        if bool(outcome.method) and outcome.method != "fingerprint":
            evidence = outcome.method.replace("fingerprint+", "fingerprint + ")
            if outcome.duration_delta is not None:
                evidence += f", Δ{outcome.duration_delta:.1f}s"
            line += colored(f"  ({evidence})", GREY)
        if outcome.status == "tagged-recording":
            line += colored("  (release needs `tool music tag`)", GREY)
    elif outcome.status == "needs-conversion":
        line += colored("  (`tool music transcode` first)", GREY)
    elif outcome.status == "needs-manual":
        line += colored(f"  (`tool music tag {outcome.target or outcome.path}`)", GREY)
    print(line, flush=True)


def _derived_duplicate(src: str, input_paths: set[str]) -> bool:
    """Whether *src* is a non-taggable source whose remux output is also in the queue."""
    if is_writable(src):
        return False
    dst = transcode_destination(src)
    if os.path.abspath(dst) == os.path.abspath(src):
        return False
    return os.path.lexists(dst) and str(Path(dst).resolve()) in input_paths


def _dry_run(files: list[tuple[Path, str]]) -> None:
    input_paths = {str(fp.resolve()) for fp, _ in files}
    queued = 0
    for fp, display in files:
        if _derived_duplicate(str(fp), input_paths):
            continue
        if read_tags(str(fp)).is_complete():
            continue
        queued += 1
        if not is_writable(str(fp)):
            ext = remux_extension(str(fp))
            if ext is None:
                label = colored(f"{'needs conversion':<25s}", RED)
                print(f"  {label} {display}")
                continue
            label = colored(f"{'remux + tag':<25s}", GOLD)
            print(f"  {label} {display} → {Path(display).with_suffix(ext)}")
        else:
            label = colored(f"{'tag':<25s}", GREEN)
            print(f"  {label} {display}")
    if queued == 0:
        print("Nothing to do — every file already has durable identity.")
    else:
        print(f"\n  {queued} files would be processed.")


def configure_parser(p: argparse.ArgumentParser) -> None:
    p.description = (
        "Burn down a tagging queue: skip files that already carry MusicBrainz identity, "
        "remux non-taggable containers losslessly, and autotag unambiguous matches. "
        "Ambiguous files are reported for `music tag`."
    )
    p.add_argument("paths", nargs="+", help="directories or files to tag")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be processed without network access or writes",
    )
    p.add_argument(
        "--delete-source",
        action="store_true",
        help="remove the original after a lossless remux verifies",
    )


def run(args: argparse.Namespace) -> None:
    files = collect_files(args.paths)
    if not files:
        print("No audio files found.")
        return

    if args.dry_run:
        _dry_run(files)
        return

    if shutil.which("fpcalc") is None:
        print("error: fpcalc not found. Install chromaprint.", file=sys.stderr)
        sys.exit(1)
    if not ensure_acoustid_key():
        print(f"error: no AcoustID API key; {ACOUSTID_KEY_HINT}", file=sys.stderr)
        sys.exit(1)

    counts: Counter[str] = Counter()
    input_paths = {str(fp.resolve()) for fp, _ in files}
    print(bold(f"  Tagging {len(files)} files"))
    try:
        for fp, _ in files:
            if _derived_duplicate(str(fp), input_paths):
                counts["derived-duplicate"] += 1
                continue
            outcome = process(str(fp), delete_source=args.delete_source)
            counts[outcome.status] += 1
            if outcome.remuxed:
                counts["remuxed"] += 1
            _print_outcome(outcome)
    except (AcoustIDError, MusicBrainzRequestError) as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        sys.exit(1)

    tagged = counts["tagged-release"] + counts["tagged-recording"]
    print()
    print(
        f"  {tagged} tagged ({counts['tagged-release']} with release, "
        f"{counts['tagged-recording']} recording only), "
        f"{counts['needs-manual']} need manual, "
        f"{counts['needs-conversion']} need conversion, "
        f"{counts['failed']} failed, "
        f"{counts['already-complete']} already complete"
    )
    if counts["remuxed"]:
        print(f"  {counts['remuxed']} files remuxed losslessly")
    if counts["derived-duplicate"]:
        print(f"  {counts['derived-duplicate']} sources skipped (their remux output is in the queue)")
    if counts["failed"]:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    configure_parser(parser)
    run(parser.parse_args())
