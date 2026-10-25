import re
from pathlib import Path

from tool.movies.search import ALL_MEDIA_EXTS, _clean_title, _director_full_name, parse_filename, search


def _safe(s: str) -> str:
    # Strip characters that are invalid in filenames on macOS/Linux/Windows
    return re.sub(r'[/\\:*?"<>|]', "", s)


def _best_match(title: str, year: int | None):
    results = search(title, year=year)
    return results[0] if results else None


def propose_rename(path: Path) -> tuple[Path | None, object, int | None, str | None]:
    """Return (new_path, match_row, parsed_year, error). Does not touch the filesystem."""
    title, year, ext = parse_filename(path.name)
    match = _best_match(title, year)
    if match is None:
        return None, None, year, "no results"
    clean = _safe(_clean_title(match))
    director = _director_full_name(match["directors"])
    stem = f"{_safe(director)} - {clean}" if director is not None and director else clean
    new_name = f"{stem} ({match['year']}).{ext}"
    new_path = path.parent / new_name
    if new_path == path:
        return None, match, year, "already correctly named"
    return new_path, match, year, None


def apply_rename(path: Path, new_path: Path) -> str | None:
    """Rename path → new_path. Returns an error string or None on success."""
    if new_path.exists():
        return f"destination already exists: {new_path.name}"
    path.rename(new_path)
    return None


def collect_media(target: Path) -> list[Path]:
    """Return all media files under target (recursively if a directory)."""
    if target.is_file():
        return [target] if target.suffix.lower() in ALL_MEDIA_EXTS else []
    return sorted(p for p in target.rglob("*") if p.is_file() and p.suffix.lower() in ALL_MEDIA_EXTS)
