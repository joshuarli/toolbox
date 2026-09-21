"""Remove image files in ``z-images/`` that no markdown note references.

Reference detection shells out to ``rg`` instead of walking markdown in
Python: every ``z-images/`` basename is written to a patterns file and a
single ``rg -o -F -f`` pass over ``*.md`` reports which names occur anywhere
in the vault. Plain substring matching covers every Obsidian reference style
(``![[name]]``, ``![[name|300]]``, ``![](z-images/name)``, ``[[name]]``),
including folder-prefixed embeds such as ``![[z-images/name]]``.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from collections.abc import Collection
from pathlib import Path

from tool.notes.common import IMAGE_FOLDER, VAULT, NotesError, images_path, require_tool, resolve_vault


def configure_parser(p: argparse.ArgumentParser) -> None:
    p.description = f"Move files in {IMAGE_FOLDER}/ that no markdown note references to the Trash. Dry run by default."
    p.add_argument(
        "--apply",
        action="store_true",
        help="move unreferenced images to the Trash (default is dry run)",
    )
    p.add_argument(
        "--vault",
        type=Path,
        default=VAULT,
        help="notes vault (default: %(default)s)",
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


def trash_files(paths: list[Path]) -> None:
    """Move *paths* to the Trash with the system ``trash`` command."""
    if not paths:
        return
    require_tool("trash", "The trash command ships with macOS 15+.")
    for i in range(0, len(paths), 500):
        chunk = paths[i : i + 500]
        proc = subprocess.run(["trash", *(str(p) for p in chunk)], capture_output=True, text=True)
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip()
            raise NotesError(f"trash failed: {detail}")


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
    dry_run = not args.apply
    if dry_run:
        print("DRY RUN — pass --apply to move unreferenced images to the Trash\n")
    if not files:
        print(f"No files in {IMAGE_FOLDER}/.")
        return 0
    if not unreferenced:
        print(f"{len(files)} images in {IMAGE_FOLDER}/, all referenced. Nothing to do.")
        return 0
    targets: list[tuple[Path, int]] = []
    for path in unreferenced:
        try:
            size = path.stat().st_size
        except OSError as exc:
            print(f"warning: skipping {IMAGE_FOLDER}/{path.name} ({exc})", file=sys.stderr)
            continue
        targets.append((path, size))
        verb = "would trash" if dry_run else "trashing"
        print(f"{verb} {IMAGE_FOLDER}/{path.name} ({_format_size(size)})")
    if dry_run:
        total_bytes = sum(size for _, size in targets)
        print(
            f"\n{len(targets)} unreferenced ({_format_size(total_bytes)}) would be trashed, "
            f"{len(files) - len(unreferenced)} referenced kept."
        )
        return 0
    trash_files([path for path, _ in targets])
    total_bytes = sum(size for _, size in targets)
    print(
        f"\n{len(targets)} unreferenced ({_format_size(total_bytes)}) trashed, "
        f"{len(files) - len(unreferenced)} referenced kept."
    )
    return 0
