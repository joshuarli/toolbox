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


def test_missing_vault(tmp_path: Path) -> None:
    with pytest.raises(NotesError, match="vault not found"):
        clean.run(argparse.Namespace(vault=tmp_path / "nope", apply=False))


def test_missing_image_folder(tmp_path: Path) -> None:
    with pytest.raises(NotesError, match="image folder not found"):
        clean.list_image_files(tmp_path)
