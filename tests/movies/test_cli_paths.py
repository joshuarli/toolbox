import subprocess
import sys
from pathlib import Path

from tool.movies import paths


def test_paths_use_env_override(monkeypatch, tmp_path):
    data = tmp_path / "data"
    monkeypatch.setenv("MOVIES_DATA_DIR", str(data))

    assert paths.data_dir() == data
    assert paths.db_path() == data / "letterboxd.db"


def test_cli_help_registers_expected_subcommands():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "tool", "movies", "--help"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    for command in (
        "encode",
        "loudnorm",
        "scan",
        "search",
        "rename",
        "scrape",
        "seed",
        "login",
        "kill",
        "generate-library",
    ):
        assert command in result.stdout
