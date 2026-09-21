import argparse
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from tool.notes import clean
from tool.notes.common import NotesError

needs_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="rg not installed")


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "Notes"
    (v / "z-images").mkdir(parents=True)
    (v / "z-images" / "keep.webp").write_bytes(b"keep")
    (v / "z-images" / "orphan.webp").write_bytes(b"orphan")
    (v / "z-images" / "sp ace.webp").write_bytes(b"space")
    (v / "a.md").write_text("hello ![[keep.webp]]\n")
    sub = v / "sub"
    sub.mkdir()
    (sub / "b.md").write_text("![](<z-images/sp ace.webp>) and ![[keep.webp|300]]\n")
    return v


def _args(vault: Path, *, apply: bool = False) -> argparse.Namespace:
    return argparse.Namespace(vault=vault, apply=apply)


@needs_rg
def test_find_unreferenced(vault: Path) -> None:
    unreferenced, files = clean.find_unreferenced(vault)
    assert [p.name for p in files] == ["keep.webp", "orphan.webp", "sp ace.webp"]
    assert [p.name for p in unreferenced] == ["orphan.webp"]


@needs_rg
def test_trash_dir_does_not_pin_images(vault: Path) -> None:
    trash = vault / ".trash"
    trash.mkdir()
    (trash / "old.md").write_text("![[orphan.webp]]\n")
    (vault / ".obsidian" / "x.md").parent.mkdir(parents=True, exist_ok=True)
    (vault / ".obsidian" / "x.md").write_text("![[orphan.webp]]\n")
    unreferenced, _ = clean.find_unreferenced(vault)
    assert [p.name for p in unreferenced] == ["orphan.webp"]


@needs_rg
def test_referenced_names_empty_input(vault: Path) -> None:
    assert clean.referenced_names(vault, []) == set()


@needs_rg
def test_run_dry_run_lists_orphan(vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert clean.run(_args(vault)) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "would trash z-images/orphan.webp" in out
    assert "keep.webp" not in out
    assert (vault / "z-images" / "orphan.webp").is_file()


@needs_rg
def test_run_apply_trashes_orphan(vault: Path) -> None:
    with patch.object(clean, "trash_files") as trash:
        assert clean.run(_args(vault, apply=True)) == 0
    trash.assert_called_once()
    assert [p.name for p in trash.call_args.args[0]] == ["orphan.webp"]


@needs_rg
def test_run_all_referenced(vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (vault / "c.md").write_text("![[orphan.webp]]\n")
    assert clean.run(_args(vault)) == 0
    assert "all referenced" in capsys.readouterr().out


def test_trash_files_batches_and_fails(tmp_path: Path) -> None:
    target = tmp_path / "x.webp"
    target.write_bytes(b"x")
    with (
        patch("tool.notes.clean.subprocess.run") as run,
        patch("tool.notes.clean.require_tool", return_value="/usr/bin/trash"),
    ):
        run.return_value.returncode = 0
        clean.trash_files([target])
        argv = run.call_args.args[0]
        assert argv[0] == "trash"
        assert argv[1] == str(target)
        run.return_value.returncode = 1
        run.return_value.stderr = "boom"
        run.return_value.stdout = ""
        with pytest.raises(NotesError, match="trash failed"):
            clean.trash_files([target])


def test_trash_files_skips_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "gone.md"
    with (
        patch("tool.notes.clean.subprocess.run") as run,
        patch("tool.notes.clean.require_tool", return_value="/usr/bin/trash"),
    ):
        clean.trash_files([missing])
        run.assert_not_called()
    assert "already gone" in capsys.readouterr().err


def test_trash_files_chunk_error_falls_back_per_file(tmp_path: Path) -> None:
    good = tmp_path / "good.md"
    bad = tmp_path / "bad.md"
    good.write_text("good\n")
    bad.write_text("bad\n")
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> object:
        calls.append(list(argv))
        result = argparse.Namespace()
        if len(argv) == 3:  # chunk of two: fail like the iCloud fnfErr case
            result.returncode = 1
            result.stderr = "fnfErr: File not found"
            result.stdout = ""
        else:
            # Per-file retry: pretend the OS moved the file out from under us.
            Path(argv[1]).unlink(missing_ok=True)
            result.returncode = 0
            result.stderr = ""
            result.stdout = ""
        return result

    with patch("tool.notes.clean.subprocess.run", side_effect=fake_run):
        with patch("tool.notes.clean.require_tool", return_value="/usr/bin/trash"):
            clean.trash_files([good, bad])  # must not raise: files are gone
    assert [c[0] for c in calls] == ["trash"] * 3
    assert not good.exists() and not bad.exists()


def test_missing_vault(tmp_path: Path) -> None:
    with pytest.raises(NotesError, match="vault not found"):
        clean.run(argparse.Namespace(vault=tmp_path / "nope", apply=False))


def test_missing_image_folder(tmp_path: Path) -> None:
    with pytest.raises(NotesError, match="image folder not found"):
        clean.list_image_files(tmp_path)


def test_find_empty_notes(tmp_path: Path) -> None:
    v = tmp_path / "Notes"
    (v / "z-images").mkdir(parents=True)
    (v / "a.md").write_text("content\n")
    (v / "empty1.md").write_bytes(b"")
    (v / "sub").mkdir()
    (v / "sub" / "empty2.md").write_bytes(b"")
    assert [p.relative_to(v).as_posix() for p in clean.find_empty_notes(v)] == [
        "empty1.md",
        "sub/empty2.md",
    ]


def test_find_duplicate_groups_keeps_shortest(tmp_path: Path) -> None:
    v = tmp_path / "Notes"
    (v / "z-images").mkdir(parents=True)
    body = "same body\n"
    (v / "hackernews.md").write_text(body)
    (v / "hackernews 2.md").write_text(body)
    (v / "Custom Linux Kernel.md").write_text(body)
    (v / "other.md").write_text("different\n")
    groups = clean.find_duplicate_groups(v)
    assert len(groups) == 1
    assert [p.name for p in groups[0]] == [
        "hackernews.md",
        "hackernews 2.md",
        "Custom Linux Kernel.md",
    ]


def test_duplicate_groups_skip_empty_and_support_dirs(tmp_path: Path) -> None:
    v = tmp_path / "Notes"
    (v / "z-images").mkdir(parents=True)
    (v / "empty.md").write_bytes(b"")
    (v / "also-empty.md").write_bytes(b"")
    (v / "keep.md").write_text("same\n")
    (v / "copy.md").write_text("same\n")
    (v / ".trash").mkdir()
    (v / ".trash" / "old.md").write_text("same\n")
    (v / ".obsidian").mkdir()
    (v / ".obsidian" / "x.md").write_text("same\n")
    empty, groups = clean.find_duplicate_targets(v)
    assert [p.name for p in empty] == ["also-empty.md", "empty.md"]
    assert len(groups) == 1
    assert [p.name for p in groups[0]] == ["copy.md", "keep.md"]


@needs_rg
def test_run_dry_run_lists_duplicates(vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (vault / "hackernews.md").write_text("same body\n")
    (vault / "hackernews 2.md").write_text("same body\n")
    (vault / "empty.md").write_bytes(b"")
    assert clean.run(_args(vault)) == 0
    out = capsys.readouterr().out
    assert "would trash hackernews 2.md (duplicate of hackernews.md)" in out
    assert "would trash empty.md (empty note)" in out
    assert "hackernews.md (duplicate of" not in out
    assert (vault / "hackernews 2.md").is_file()
    assert (vault / "empty.md").is_file()


@needs_rg
def test_run_apply_trashes_images_and_notes(vault: Path) -> None:
    (vault / "hackernews.md").write_text("same body\n")
    (vault / "hackernews 2.md").write_text("same body\n")
    (vault / "empty.md").write_bytes(b"")
    with patch.object(clean, "trash_files") as trash:
        assert clean.run(_args(vault, apply=True)) == 0
    assert trash.call_count == 2
    assert [p.name for p in trash.call_args_list[0].args[0]] == ["orphan.webp"]
    trashed_notes = sorted(p.name for p in trash.call_args_list[1].args[0])
    assert trashed_notes == ["empty.md", "hackernews 2.md"]


@needs_rg
def test_run_no_duplicates_message(vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert clean.run(_args(vault)) == 0
    assert "No duplicate or empty notes" in capsys.readouterr().out
