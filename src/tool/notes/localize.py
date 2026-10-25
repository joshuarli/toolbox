"""Download remote markdown images into ``z-images/`` as content-hashed WebP.

Only inline ``![alt](https://...)`` embeds are handled — the same scope as
the webp-paste plugin's Loupe import (`~/d/obsidian-webp-paste`). Video and
social *page* URLs (YouTube watch pages, tweets, …) sometimes appear inside
image embeds as clipping artifacts; those routes never serve image bytes, so
they are classified by :func:`is_page_url` and skipped entirely — never
listed as downloads, never attempted. Each unique image URL is downloaded
once, converted to WebP at quality 85 (already-WebP bytes are kept as-is,
never re-encoded), stored as ``z-images/<sha256>.webp`` where the hash
covers the final WebP bytes, and every occurrence is rewritten to
``![[<sha>.webp]]``. Failures leave the original remote embed intact.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import errno
import hashlib
import os
import re
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from tool.notes.common import (
    IMAGE_ACCEPT,
    IMAGE_FOLDER,
    USER_AGENT,
    VAULT,
    WEBP_QUALITY,
    NotesError,
    images_path,
    require_tool,
    resolve_vault,
)

_FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})")
# Same ASCII-punctuation escape set as the webp-paste plugin's
# unescapeMarkdownDestination: only `\<punct>` unescapes.
_UNESCAPE_RE = re.compile(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]\^_`{|}~])")
# Video/social *page* routes that never serve image bytes (thumbnails live on
# dedicated image hosts like i.ytimg.com, i.vimeocdn.com, pbs.twimg.com, so
# those stay localizable). Matched embeds are clipping artifacts, not images.
_PAGE_URL_RES = (
    re.compile(
        r"^https?://(?:www\.|m\.)?youtube\.com/(?:watch|shorts|live|embed|playlist|clips)(?:[?/#]|$)",
        re.IGNORECASE,
    ),
    re.compile(r"^https?://youtu\.be(?:[?/#]|$)", re.IGNORECASE),
    re.compile(r"^https?://(?:www\.|player\.)?vimeo\.com(?:[?/#]|$)", re.IGNORECASE),
    re.compile(r"^https?://(?:www\.|mobile\.)?(?:x\.com|twitter\.com)/(?:i/|.*/status/)", re.IGNORECASE),
)
_RETRYABLE_STATUS = {408, 425, 429} | set(range(500, 600))
_DOWNLOAD_ATTEMPTS = 3
_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class RemoteImage:
    """One inline remote image embed: ``![alt](url)`` spanning ``[start:end)``."""

    url: str
    start: int
    end: int


class ImageError(Exception):
    """A single image could not be downloaded or converted; its embed is kept."""


def _is_escaped(text: str, pos: int) -> bool:
    count = 0
    i = pos - 1
    while i >= 0 and text[i] == "\\":
        count += 1
        i -= 1
    return count % 2 == 1


def _fenced_ranges(text: str) -> list[tuple[int, int]]:
    """Return ``(start, end)`` offsets of fenced code blocks (``` / ~~~)."""
    ranges: list[tuple[int, int]] = []
    open_char = ""
    open_len = 0
    open_start = 0
    offset = 0
    for line in text.splitlines(keepends=True):
        match = _FENCE_RE.match(line)
        if match is not None:
            run = match.group(2)
            char = run[0]
            rest = line[match.end() :]
            if not open_char:
                if char == "`" and "`" in rest:
                    pass  # info string containing a backtick is not a fence
                else:
                    open_char, open_len, open_start = char, len(run), offset
            elif char == open_char and len(run) >= open_len and rest.strip() == "":
                ranges.append((open_start, offset + len(line)))
                open_char, open_len = "", 0
        offset += len(line)
    if open_char:
        ranges.append((open_start, len(text)))
    return ranges


def _inline_code_spans(line: str, base: int) -> list[tuple[int, int]]:
    """Best-effort backtick spans for one line; unmatched runs are ignored."""
    spans: list[tuple[int, int]] = []
    open_len = 0
    open_start = 0
    i = 0
    while i < len(line):
        if line[i] == "\\":
            i += 2
            continue
        if line[i] != "`":
            i += 1
            continue
        j = i
        while j < len(line) and line[j] == "`":
            j += 1
        if open_len == 0:
            open_len, open_start = j - i, i
        elif j - i == open_len:
            spans.append((base + open_start, base + j))
            open_len = 0
        i = j
    return spans


def _in_code(text: str, pos: int, fences: list[tuple[int, int]]) -> bool:
    for start, end in fences:
        if start <= pos < end:
            return True
    line_start = text.rfind("\n", 0, pos) + 1
    line_end = text.find("\n", pos)
    line = text[line_start:] if line_end == -1 else text[line_start:line_end]
    return any(start <= pos < end for start, end in _inline_code_spans(line, line_start))


def _parse_title(text: str, pos: int) -> int | None:
    """Return the offset just past a link title starting at *pos*, if any."""
    if pos >= len(text):
        return None
    opener = text[pos]
    if opener in ('"', "'"):
        i = pos + 1
        while i < len(text):
            if text[i] == "\\":
                i += 2
                continue
            if text[i] == opener:
                return i + 1
            i += 1
        return None
    if opener == "(":
        depth = 1
        i = pos + 1
        while i < len(text):
            if text[i] == "\\":
                i += 2
                continue
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        return None
    return None


def _parse_destination(text: str, pos: int) -> tuple[str, int] | None:
    """Parse an image destination at *pos*; return ``(destination, end)``.

    *end* is the offset just past the closing ``)`` of the whole embed.
    """
    n = len(text)
    i = pos
    while i < n and text[i] in " \t\n":
        i += 1
    if i < n and text[i] == "<":
        i += 1
        start = i
        while i < n and text[i] != ">":
            if text[i] == "\\":
                i += 2
                continue
            if text[i] == "\n" or text[i] == "<":
                return None
            i += 1
        if i >= n:
            return None
        destination = text[start:i]
        i += 1
    else:
        start = i
        depth = 0
        while i < n:
            char = text[i]
            if char == "\\":
                i += 2
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    break
                depth -= 1
            elif char in " \t\n" or ord(char) < 0x20:
                break
            i += 1
        destination = text[start:i]
    j = i
    while j < n and text[j] in " \t\n":
        j += 1
    if j < n and text[j] == ")":
        return destination, j + 1
    if j == i:
        return None
    title_end = _parse_title(text, j)
    if title_end is None:
        return None
    j = title_end
    while j < n and text[j] in " \t\n":
        j += 1
    if j >= n or text[j] != ")":
        return None
    return destination, j + 1


def _parse_image_at(text: str, bang: int) -> tuple[str, int] | None:
    """Parse ``![alt](dest)`` at *bang*; return ``(destination, end)``."""
    depth = 1
    j = bang + 2
    n = len(text)
    while j < n:
        char = text[j]
        if char == "\\":
            j += 2
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                break
        j += 1
    if depth != 0 or j + 1 >= n or text[j + 1] != "(":
        return None
    return _parse_destination(text, j + 2)


def remote_url(destination: str) -> str | None:
    """Return the URL if *destination* is an absolute remote image URL."""
    cleaned = _UNESCAPE_RE.sub(r"\1", destination).strip()
    if not cleaned or cleaned.startswith("#"):
        return None
    lowered = cleaned.lower()
    if lowered.startswith(("http://", "https://")):
        return cleaned
    return None


def is_page_url(url: str) -> bool:
    """Return True when *url* is a video/social page, never image bytes.

    Clipping pipelines sometimes serialize a video as ``![](<watch-url>)``;
    attempting to "localize" such an embed can only download an HTML page.
    Only routes that provably never serve images are matched — thumbnails on
    dedicated image hosts (``i.ytimg.com``, ``i.vimeocdn.com``,
    ``pbs.twimg.com``) stay localizable.
    """
    return any(pattern.match(url) is not None for pattern in _PAGE_URL_RES)


def _scan_text(text: str) -> tuple[list[RemoteImage], list[str]]:
    """Split remote embeds into ``(localizable, skipped page URLs)``."""
    fences = _fenced_ranges(text)
    found: list[RemoteImage] = []
    skipped: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "!" and i + 1 < n and text[i + 1] == "[" and not _is_escaped(text, i):
            parsed = _parse_image_at(text, i)
            if parsed is None:
                i += 1
                continue
            destination, end = parsed
            url = remote_url(destination)
            if url is None or _in_code(text, i, fences):
                i += 1
                continue
            if is_page_url(url):
                skipped.append(url)
            else:
                found.append(RemoteImage(url=url, start=i, end=end))
            i = end
            continue
        i += 1
    return found, skipped


def find_remote_images(text: str) -> list[RemoteImage]:
    """Find inline remote ``![alt](url)`` embeds outside code spans.

    Video/social page embeds (see :func:`is_page_url`) are excluded: they
    are clipping artifacts, not images.
    """
    return _scan_text(text)[0]


def find_page_urls(text: str) -> list[str]:
    """Return skipped video/social page URLs, unique in first-seen order."""
    unique: list[str] = []
    seen: set[str] = set()
    for url in _scan_text(text)[1]:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def apply_localizations(text: str, url_to_embed: Mapping[str, str]) -> tuple[str, int]:
    """Rewrite remote embeds with a localized mapping; nothing else changes.

    The text is rescanned (like the webp-paste plugin's ``Vault.process``)
    so only embeds whose URL localized are touched, byte-for-byte otherwise.
    Returns the updated text and the number of embeds replaced.
    """
    if not url_to_embed:
        return text, 0
    parts: list[str] = []
    cursor = 0
    replaced = 0
    for occurrence in find_remote_images(text):
        embed = url_to_embed.get(occurrence.url)
        if embed is None:
            continue
        parts.append(text[cursor : occurrence.start])
        parts.append(embed)
        cursor = occurrence.end
        replaced += 1
    if not replaced:
        return text, 0
    parts.append(text[cursor:])
    return "".join(parts), replaced


def is_webp_bytes(data: bytes) -> bool:
    """Return True when *data* starts with the WebP RIFF magic."""
    return len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP"


def download_image(url: str, *, timeout: float = 30.0) -> tuple[bytes, str]:
    """Download *url*; return ``(body, content_type)`` or raise :class:`ImageError`.

    Retries transient network failures and HTTP 408/425/429/5xx with
    exponential backoff, mirroring the webp-paste plugin's retry policy.
    Responses larger than 100 MB are refused without being fully read.
    """
    last_error = "unknown error"
    for attempt in range(_DOWNLOAD_ATTEMPTS):
        try:
            request = urllib.request.Request(
                url,
                headers={"Accept": IMAGE_ACCEPT, "User-Agent": USER_AGENT},
                method="GET",
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(_MAX_DOWNLOAD_BYTES + 1)
                if not isinstance(raw, bytes) or not raw:
                    raise ImageError("empty response")
                if len(raw) > _MAX_DOWNLOAD_BYTES:
                    raise ImageError(f"response exceeds {_MAX_DOWNLOAD_BYTES // 1024 // 1024} MB limit")
                content_type = response.headers.get("Content-Type", "")
                if not isinstance(content_type, str):
                    content_type = ""
                return raw, content_type
        except urllib.error.HTTPError as exc:
            if exc.code in _RETRYABLE_STATUS:
                last_error = f"HTTP {exc.code}"
            else:
                raise ImageError(f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc) or type(exc).__name__
        if attempt + 1 < _DOWNLOAD_ATTEMPTS:
            time.sleep(0.25 * (2**attempt))
    raise ImageError(last_error)


def is_skippable_content_type(content_type: str) -> bool:
    """Return True for responses that are clearly not images (HTML/text pages)."""
    lowered = content_type.lower()
    return "text/" in lowered or "html" in lowered


def convert_to_webp(data: bytes, *, quality: int = WEBP_QUALITY) -> bytes:
    """Convert image *data* to WebP; already-WebP bytes pass through untouched."""
    if is_webp_bytes(data):
        return data
    ffmpeg = require_tool("ffmpeg", "Install ffmpeg: brew install ffmpeg")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "src.bin"
        dst = Path(tmp) / "out.webp"
        src.write_bytes(data)
        proc = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(src),
                "-c:v",
                "libwebp",
                "-quality",
                str(quality),
                str(dst),
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0 or not dst.is_file():
            detail = proc.stderr.strip().splitlines()
            raise ImageError(f"webp conversion failed: {detail[-1] if detail else 'ffmpeg error'}")
        return dst.read_bytes()


def sha256_name(webp_bytes: bytes) -> str:
    """Return the ``<sha256>.webp`` filename for final WebP *webp_bytes*."""
    return hashlib.sha256(webp_bytes).hexdigest() + ".webp"


def resolve_roots(vault: Path, roots: list[Path]) -> list[Path] | None:
    """Resolve CLI *roots* to absolute vault paths.

    Relative paths resolve against the current directory, falling back to the
    vault. Raises :class:`NotesError` for missing paths or paths outside the
    vault instead of silently matching nothing.
    """
    if not roots:
        return None
    resolved: list[Path] = []
    for root in roots:
        candidate = root if root.is_absolute() else Path.cwd() / root
        if not candidate.exists() and (vault / root).exists():
            candidate = vault / root
        if not candidate.exists():
            raise NotesError(f"path does not exist: {root}")
        candidate = candidate.resolve()
        try:
            candidate.relative_to(vault)
        except ValueError:
            raise NotesError(f"path is outside the notes vault: {root}")
        resolved.append(candidate)
    return resolved


def collect_markdown_files(vault: Path, roots: list[Path] | None = None) -> list[Path]:
    """Return vault ``*.md`` files under *roots* (default: whole vault).

    Skips the vault's ``.trash`` and ``.obsidian`` support directories.
    Explicit file roots are honored regardless of suffix.
    """
    if roots is not None and roots:
        candidates: set[Path] = set()
        for root in roots:
            if root.is_file():
                candidates.add(root)
            elif root.is_dir():
                candidates.update(p for p in root.rglob("*.md") if p.is_file())
    else:
        candidates = {p for p in vault.rglob("*.md") if p.is_file()}
    vault = vault.resolve()
    kept: set[Path] = set()
    for path in candidates:
        try:
            rel = path.relative_to(vault)
        except ValueError:
            continue
        if ".trash" in rel.parts or ".obsidian" in rel.parts:
            continue
        resolved = _resolve_note_path(vault, path)
        resolved_rel = resolved.relative_to(vault)
        if ".trash" in resolved_rel.parts or ".obsidian" in resolved_rel.parts:
            continue
        kept.add(resolved)
    return sorted(kept)


def _read_text(path: Path) -> str:
    """Read a note as strict UTF-8 while keeping its original newline bytes."""
    try:
        with path.open("r", encoding="utf-8", newline="") as note_file:
            return note_file.read()
    except UnicodeDecodeError as exc:
        raise NotesError(f"could not read {path} as UTF-8: {exc}") from exc
    except OSError as exc:
        raise NotesError(f"could not read {path}: {exc}") from exc


def _resolve_note_path(vault: Path, path: Path) -> Path:
    """Resolve a note and require its final file to stay inside *vault*."""
    vault = vault.resolve()
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise NotesError(f"could not resolve note path {path}: {exc}") from exc
    try:
        resolved.relative_to(vault)
    except ValueError:
        raise NotesError(f"path is outside the notes vault: {path}")
    try:
        mode = resolved.lstat().st_mode
    except OSError as exc:
        raise NotesError(f"could not inspect note {path}: {exc}") from exc
    if not stat.S_ISREG(mode):
        raise NotesError(f"not a regular note file: {path}")
    return resolved


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomically replace a regular note, preserving its permission bits."""
    original = path.lstat()
    if not stat.S_ISREG(original.st_mode):
        raise OSError(f"not a regular file: {path}")

    temp_path: Path | None = None
    file_descriptor: int | None = None
    try:
        file_descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temp_path = Path(temp_name)
        temp_file = os.fdopen(file_descriptor, "w", encoding="utf-8", newline="")
        file_descriptor = None
        with temp_file:
            temp_file.write(text)
            temp_file.flush()
            os.fchmod(temp_file.fileno(), stat.S_IMODE(original.st_mode))
            os.fsync(temp_file.fileno())

        current = path.lstat()
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_dev != original.st_dev
            or current.st_ino != original.st_ino
        ):
            raise OSError(f"note changed while being rewritten: {path}")
        os.replace(temp_path, path)
        temp_path = None
    except BaseException as exc:
        cleanup_errors: list[str] = []
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError as cleanup_exc:
                if cleanup_exc.errno != errno.EBADF:
                    cleanup_errors.append(f"could not close staged note: {cleanup_exc}")
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                cleanup_errors.append(f"could not remove staged note {temp_path}: {cleanup_exc}")
        if cleanup_errors:
            raise OSError(f"{exc}; {'; '.join(cleanup_errors)}") from exc
        raise


def fetch_and_store(
    url: str,
    folder: Path,
    *,
    quality: int = WEBP_QUALITY,
) -> str:
    """Download *url*, convert to WebP, store under *folder*; return the filename.

    Raises :class:`ImageError` when the URL must stay remote.
    """
    body, content_type = download_image(url)
    if content_type and is_skippable_content_type(content_type):
        raise ImageError(f"non-image content type: {content_type.split(';', 1)[0].strip() or content_type}")
    webp = convert_to_webp(body, quality=quality)
    name = sha256_name(webp)
    target = folder / name
    if not target.is_file():
        # Same hash implies identical bytes, so concurrent writers agree, but
        # a crash mid-write must never leave a truncated file behind: write
        # to a sibling temp file and atomically rename into place.
        fd, tmp_name = tempfile.mkstemp(dir=folder, suffix=".webp.part")
        try:
            with os.fdopen(fd, "wb") as tmp_file:
                tmp_file.write(webp)
            os.replace(tmp_name, target)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
    return name


def configure_parser(p: argparse.ArgumentParser) -> None:
    p.description = (
        f"Download remote markdown images into {IMAGE_FOLDER}/ as WebP and rewrite "
        "embeds to ![[<sha>.webp]]. Dry run by default."
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="download, convert, and rewrite notes (default is dry run)",
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
        help="concurrent image downloads (default: %(default)s)",
    )
    p.add_argument(
        "--quality",
        type=int,
        default=WEBP_QUALITY,
        help="WebP quality 1-100, like the webp-paste plugin setting (default: %(default)s)",
    )
    p.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[],
        help="limit to specific notes or directories (default: whole vault)",
    )


def _scan_notes(files: list[Path]) -> tuple[dict[Path, list[RemoteImage]], list[str]]:
    """Map each file with remote embeds to its occurrences, in file order.

    Also returns the sorted unique skipped video/social page URLs.
    """
    occurrences: dict[Path, list[RemoteImage]] = {}
    pages: set[str] = set()
    for path in files:
        text = _read_text(path)
        found, skipped = _scan_text(text)
        if found:
            occurrences[path] = found
        pages.update(skipped)
    return occurrences, sorted(pages)


def _unique_urls(occurrences: dict[Path, list[RemoteImage]]) -> list[str]:
    """Return distinct URLs in first-seen order."""
    urls: list[str] = []
    seen: set[str] = set()
    for found in occurrences.values():
        for occurrence in found:
            if occurrence.url not in seen:
                seen.add(occurrence.url)
                urls.append(occurrence.url)
    return urls


def _print_dry_run(vault: Path, occurrences: dict[Path, list[RemoteImage]], urls: list[str], pages: list[str]) -> None:
    for path in occurrences:
        print(f"{path.relative_to(vault)}:")
        for occurrence in occurrences[path]:
            print(f"  {occurrence.url}")
    n_images = sum(len(found) for found in occurrences.values())
    print(
        f"\n{len(occurrences)} notes, {n_images} images "
        f"({len(urls)} unique URLs) would be downloaded to {IMAGE_FOLDER}/. "
        "Pass --apply to download."
    )
    if pages:
        print(f"\n{len(pages)} video/page embeds skipped (not images):")
        for url in pages:
            print(f"  {url}")


def _download_all(urls: list[str], folder: Path, *, quality: int, jobs: int) -> tuple[dict[str, str], dict[str, str]]:
    """Fetch every URL into *folder*; return ``(url_to_name, failures)``."""
    url_to_name: dict[str, str] = {}
    failures: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        future_to_url = {pool.submit(fetch_and_store, url, folder, quality=quality): url for url in urls}
        for future in concurrent.futures.as_completed(future_to_url):
            url = future_to_url[future]
            try:
                url_to_name[url] = future.result()
            except (ImageError, OSError) as exc:
                failures[url] = str(exc)
    return url_to_name, failures


def _rewrite_notes(
    vault: Path, occurrences: dict[Path, list[RemoteImage]], url_to_embed: Mapping[str, str]
) -> tuple[int, int]:
    """Rewrite localized embeds; return ``(notes_rewritten, images_replaced)``."""
    n_notes = 0
    n_images = 0
    for path in occurrences:
        path = _resolve_note_path(vault, path)
        text = _read_text(path)
        updated, replaced = apply_localizations(text, url_to_embed)
        if replaced == 0:
            continue
        try:
            _atomic_write_text(path, updated)
        except OSError as exc:
            raise NotesError(f"could not rewrite {path.relative_to(vault)}: {exc}") from exc
        print(f"rewrote {path.relative_to(vault)} ({replaced} images)")
        n_notes += 1
        n_images += replaced
    return n_notes, n_images


def run(args: argparse.Namespace) -> int:
    if args.quality < 1 or args.quality > 100:
        raise NotesError(f"invalid --quality: {args.quality} (expected 1-100)")
    vault = resolve_vault(args.vault).resolve()
    files = collect_markdown_files(vault, resolve_roots(vault, args.paths))
    occurrences, pages = _scan_notes(files)
    if not occurrences:
        if pages:
            print(f"No localizable images found ({len(pages)} video/page embeds skipped).")
        else:
            print("No remote images found.")
        return 0
    urls = _unique_urls(occurrences)
    if not args.apply:
        _print_dry_run(vault, occurrences, urls, pages)
        return 0
    folder = images_path(vault)
    folder.mkdir(parents=True, exist_ok=True)
    url_to_name, failures = _download_all(urls, folder, quality=args.quality, jobs=max(1, args.jobs))
    url_to_embed = {url: f"![[{name}]]" for url, name in url_to_name.items()}
    for url, name in sorted(url_to_name.items()):
        print(f"downloaded {url} -> {name}")
    for url, reason in sorted(failures.items()):
        print(f"skipped {url} ({reason})")
    n_notes, n_images = _rewrite_notes(vault, occurrences, url_to_embed)
    skipped = f", {len(pages)} video/page embeds skipped" if pages else ""
    print(
        f"\n{n_images} images localized in {n_notes} notes as {IMAGE_FOLDER}/*.webp; "
        f"{len(failures)} URLs remain remote{skipped}."
    )
    return 0
