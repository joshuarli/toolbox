"""Remove unreferenced ``z-images/`` files and duplicate notes.

Reference detection shells out to ``rg`` instead of walking markdown in
Python: every ``z-images/`` basename is written to a patterns file and a
single ``rg -o -F -f`` pass over ``*.md`` reports which names occur anywhere
in the vault. Plain substring matching covers every Obsidian reference style
(``![[name]]``, ``![[name|300]]``, ``![](z-images/name)``, ``[[name]]``),
including folder-prefixed embeds such as ``![[z-images/name]]``.

Duplicate notes are exact byte-identical ``*.md`` files grouped by md5.
Empty (0-byte) notes are all listed for trashing. In each non-empty
duplicate group the survivor is the shortest vault-relative path, ties
broken alphabetically; the rest are trashed.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import subprocess
import sys
import tempfile
from collections.abc import Collection
from pathlib import Path

from tool.notes.common import IMAGE_FOLDER, VAULT, NotesError, images_path, require_tool, resolve_vault


def configure_parser(p: argparse.ArgumentParser) -> None:
    p.description = (
        f"Move files in {IMAGE_FOLDER}/ that no markdown note references, plus "
        "duplicate and empty notes, to the Trash. Dry run by default."
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="move unreferenced images and duplicate/empty notes to the Trash (default is dry run)",
    )
    p.add_argument(
        "--vault",
        type=Path,
        default=VAULT,
        help="notes vault (default: %(default)s)",
    )
    p.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=8,
        help="concurrent trash batches (default: %(default)s)",
    )


def list_image_files(vault: Path) -> list[Path]:
    """Return sorted files directly inside ``z-images/``."""
    folder = images_path(vault)
    if not folder.is_dir():
        raise NotesError(f"image folder not found: {folder}")
    return sorted(p for p in folder.iterdir() if p.is_file())


def referenced_names(vault: Path, names: Collection[str]) -> set[str]:
    """Return the subset of *names* that occur in any vault ``*.md`` file.

    Runs one ``rg -o -F -f`` pass so thousands of image files cost a single
    scan instead of one scan per file.
    """
    todo = list(names)
    if not todo:
        return set()
    rg = require_tool("rg", "Install ripgrep: brew install ripgrep")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as tmp:
        patterns_path = tmp.name
        for name in todo:
            tmp.write(name + "\n")
    try:
        proc = subprocess.run(
            [
                rg,
                "-N",
                "--no-filename",
                "--no-heading",
                "-o",
                "-F",
                "-f",
                patterns_path,
                "-g",
                "*.md",
                "--glob",
                "!**/.trash/**",
                "--glob",
                "!**/.obsidian/**",
                "--",
                str(vault),
            ],
            capture_output=True,
            text=True,
        )
    finally:
        Path(patterns_path).unlink(missing_ok=True)
    if proc.returncode == 1:
        return set()  # rg exits 1 when nothing matches
    if proc.returncode != 0:
        raise NotesError(f"rg failed: {proc.stderr.strip()}")
    return set(proc.stdout.splitlines())


def find_unreferenced(vault: Path) -> tuple[list[Path], list[Path]]:
    """Return ``(unreferenced, all)`` image files in ``z-images/``."""
    files = list_image_files(vault)
    if not files:
        return [], []
    seen = referenced_names(vault, [p.name for p in files])
    return [p for p in files if p.name not in seen], files


def list_note_files(vault: Path) -> list[Path]:
    """Return sorted vault ``*.md`` files, excluding support directories."""
    found = [p for p in vault.rglob("*.md") if p.is_file()]
    kept = []
    for path in found:
        try:
            rel = path.relative_to(vault)
        except ValueError:
            continue
        if ".trash" in rel.parts or ".obsidian" in rel.parts:
            continue
        kept.append(path)
    return sorted(kept)


def _md5_of(path: Path) -> str | None:
    try:
        return hashlib.md5(path.read_bytes()).hexdigest()
    except OSError as exc:
        print(f"warning: skipping {path.name} ({exc})", file=sys.stderr)
        return None


def find_empty_notes(vault: Path) -> list[Path]:
    """Return sorted empty (0-byte) ``*.md`` files; every one is trash."""
    empty = []
    for path in list_note_files(vault):
        try:
            if path.stat().st_size == 0:
                empty.append(path)
        except OSError as exc:
            print(f"warning: skipping {path.name} ({exc})", file=sys.stderr)
    return empty


def find_duplicate_groups(vault: Path) -> list[list[Path]]:
    """Return groups of byte-identical non-empty notes, survivor first.

    Files are grouped by md5; groups with a single member are dropped.
    Within a group the survivor is the shortest vault-relative path, ties
    broken alphabetically, so ``hackernews.md`` beats ``hackernews 2.md``.
    Groups are sorted by survivor for deterministic output.
    """
    by_hash: dict[str, list[Path]] = {}
    for path in list_note_files(vault):
        try:
            if path.stat().st_size == 0:
                continue
        except OSError as exc:
            print(f"warning: skipping {path.name} ({exc})", file=sys.stderr)
            continue
        digest = _md5_of(path)
        if digest is None:
            continue
        by_hash.setdefault(digest, []).append(path)
    groups = []
    for paths in by_hash.values():
        if len(paths) < 2:
            continue
        ordered = sorted(paths, key=lambda p: (len(p.relative_to(vault).as_posix()), p.relative_to(vault).as_posix()))
        groups.append(ordered)
    return sorted(groups, key=lambda g: g[0].relative_to(vault).as_posix())


def find_duplicate_targets(vault: Path) -> tuple[list[Path], list[list[Path]]]:
    """Return ``(empty, groups)`` duplicate-note findings for *vault*."""
    return find_empty_notes(vault), find_duplicate_groups(vault)


def _trash_chunk(chunk: list[Path]) -> list[str]:
    """Trash one batch; return per-file error strings (empty on success).

    A failed batch falls back to one file per ``trash`` invocation so a
    single flaky file cannot fail the whole batch. Files that vanish
    mid-batch count as success: the scan-then-trash gap races iCloud sync.
    """
    proc = subprocess.run(["trash", *(str(p) for p in chunk)], capture_output=True, text=True)
    if proc.returncode == 0:
        return []
    detail = proc.stderr.strip() or proc.stdout.strip()
    failures: list[str] = []
    for path in chunk:
        if not path.exists():
            continue  # moved despite the batch error; goal achieved
        retry = subprocess.run(["trash", str(path)], capture_output=True, text=True)
        if retry.returncode != 0 and path.exists():
            err = retry.stderr.strip() or retry.stdout.strip() or detail
            failures.append(f"{path}: {err}")
        elif retry.returncode != 0:
            print(f"warning: trash reported an error for {path} but it is gone", file=sys.stderr)
    return failures


def trash_files(paths: list[Path], *, jobs: int = 8) -> None:
    """Move *paths* to the Trash with the system ``trash`` command.

    Missing files are skipped with a warning: the scan-then-trash gap races
    iCloud sync, so a file can vanish (or land in the Trash) between listing
    and trashing. Files go in batches of 100 trashed by up to *jobs*
    concurrent ``trash`` processes — one process per batch is latency-bound
    on FileProvider coordination (~20 files/s measured), so concurrency is
    the speed lever. A progress line prints after each batch completes.
    """
    pending = []
    for path in paths:
        if path.exists():
            pending.append(path)
        else:
            print(f"warning: skipping {path} (already gone)", file=sys.stderr)
    if not pending:
        return
    require_tool("trash", "The trash command ships with macOS 15+.")
    workers = max(1, jobs)
    chunk_size = 100
    chunks = [pending[i : i + chunk_size] for i in range(0, len(pending), chunk_size)]
    total = len(chunks)
    print(f"trashing {len(pending)} files in {total} batches ({workers} at a time) ...", flush=True)
    failures: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_batch = {pool.submit(_trash_chunk, chunk): n for n, chunk in enumerate(chunks, start=1)}
        done = 0
        for future in concurrent.futures.as_completed(future_to_batch):
            failures.extend(future.result())
            done += 1
            print(f"trashed {done}/{total} batches", flush=True)
    if failures:
        raise NotesError(f"trash failed for {len(failures)} file(s): {failures[0]}")


def _format_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def run(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    unreferenced, files = find_unreferenced(vault)
    empty, groups = find_duplicate_targets(vault)
    note_targets: list[tuple[Path, str]] = [(p, "empty note") for p in empty]
    for group in groups:
        survivor = group[0].relative_to(vault).as_posix()
        note_targets.extend((dup, f"duplicate of {survivor}") for dup in group[1:])
    # Deterministic order for output and trashing.
    note_targets.sort(key=lambda item: item[0].relative_to(vault).as_posix())

    dry_run = not args.apply
    if dry_run:
        print("DRY RUN — pass --apply to move unreferenced images and duplicate notes to the Trash\n")

    if not files:
        print(f"No files in {IMAGE_FOLDER}/.")
    elif not unreferenced:
        print(f"{len(files)} images in {IMAGE_FOLDER}/, all referenced. Nothing to do.")
    else:
        targets: list[tuple[Path, int]] = []
        for path in unreferenced:
            try:
                size = path.stat().st_size
            except OSError as exc:
                print(f"warning: skipping {IMAGE_FOLDER}/{path.name} ({exc})", file=sys.stderr)
                continue
            targets.append((path, size))
            if dry_run:
                print(f"would trash {IMAGE_FOLDER}/{path.name} ({_format_size(size)})", flush=True)
        total_bytes = sum(size for _, size in targets)
        kept = len(files) - len(unreferenced)
        if dry_run:
            print(
                f"\n{len(targets)} unreferenced ({_format_size(total_bytes)}) would be trashed, {kept} referenced kept."
            )
        else:
            trash_files([path for path, _ in targets], jobs=args.jobs)
            for path, size in targets:
                print(f"trashed {IMAGE_FOLDER}/{path.name} ({_format_size(size)})", flush=True)
            print(f"\n{len(targets)} unreferenced ({_format_size(total_bytes)}) trashed, {kept} referenced kept.")

    if not note_targets:
        print("No duplicate or empty notes. Nothing to do.")
        return 0
    if dry_run:
        for path, reason in note_targets:
            print(f"would trash {path.relative_to(vault)} ({reason})", flush=True)
        n_dupes = len(note_targets) - len(empty)
        print(
            f"\n{len(note_targets)} duplicate/empty notes would be trashed "
            f"({len(empty)} empty, {n_dupes} duplicates in {len(groups)} groups)."
        )
        return 0
    trash_files([path for path, _ in note_targets], jobs=args.jobs)
    for path, reason in note_targets:
        print(f"trashed {path.relative_to(vault)} ({reason})", flush=True)
    n_dupes = len(note_targets) - len(empty)
    print(
        f"\n{len(note_targets)} duplicate/empty notes trashed "
        f"({len(empty)} empty, {n_dupes} duplicates in {len(groups)} groups)."
    )
    return 0
