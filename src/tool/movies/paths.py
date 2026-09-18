import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def data_dir() -> Path:
    return Path(os.environ.get("MOVIES_DATA_DIR", PROJECT_ROOT / "var" / "letterboxd")).expanduser()


def db_path() -> Path:
    return data_dir() / "letterboxd.db"
