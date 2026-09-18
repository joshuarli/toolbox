"""Build a static Instagram-like site from instagram.db."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from html import escape
from json import dumps
from os import cpu_count
from pathlib import Path
from platform import system
from shutil import which
from typing import Any

from tool.ig.media import media_url_ext

_FFMPEG: str | None = which("ffmpeg")
_SIPS: str | None = which("sips")
_CWEBP: str | None = which("cwebp")
_IS_DARWIN = system() == "Darwin"

DB = "instagram.db"
OUT = Path("site")
WEB = Path("web")
ASSET_NAMES = ("site.css", "site.js", "carousel.svg", "play.svg")
SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
THUMB_MAX = 640
THUMB_QUALITY = 80
VIDEO_EXTS = frozenset({".mp4", ".mov", ".m4v", ".webm"})


class ThumbnailGenerationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ThumbnailBackend:
    method: str


def parse_caption(raw: str) -> dict[str, Any]:
    """Extract likes, date, and body text from raw caption string."""
    raw = (raw or "").strip()
    m = re.match(
        r'^([\d,]+) likes?, [\d,]+ comments? - (\S+) on ([A-Za-z]+ \d{1,2}, \d{4})(?::(?: "(.*)"\.?)?)?$',
        raw,
        re.DOTALL,
    )
    if m:
        date_str = m.group(3)
        try:
            posted_at: datetime | None = datetime.strptime(date_str, "%B %d, %Y")
        except ValueError:
            posted_at = None
        return {
            "likes": int(m.group(1).replace(",", "")),
            "author": m.group(2),
            "date": date_str,
            "posted_at": posted_at,
            "text": m.group(4) or "",
        }
    return {"likes": 0, "author": "", "date": "", "posted_at": None, "text": raw}


def post_datetime_from_shortid(shortid: str) -> datetime | None:
    media_id = 0
    try:
        for char in shortid:
            media_id = media_id * 64 + SHORTCODE_ALPHABET.index(char)
    except ValueError:
        return None

    timestamp_ms = (media_id >> 23) + 1_314_220_021_721
    try:
        return datetime.fromtimestamp(timestamp_ms / 1000)
    except OSError, OverflowError, ValueError:
        return None


def format_post_date(posted_at: datetime | None) -> str:
    if not posted_at:
        return ""
    return f"{MONTHS[posted_at.month - 1]} {posted_at.day}, {posted_at.year}"


def write_if_changed(path: Path, content: str | bytes) -> bool:
    if isinstance(content, str):
        data = content.encode()
        mode = "w"
        kwargs: dict[str, Any] = {"encoding": "utf-8"}
    else:
        data = content
        mode = "wb"
        kwargs = {}

    if path.exists() and path.read_bytes() == data:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, **kwargs) as fh:
        fh.write(content)
    return True


def copy_if_changed(src: Path, dst: Path) -> bool:
    if dst.exists():
        src_stat = src.stat()
        dst_stat = dst.stat()
        if src_stat.st_size == dst_stat.st_size and int(src_stat.st_mtime_ns) == int(dst_stat.st_mtime_ns):
            return False

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _materialize_media(src: Path, dst: Path) -> bool:
    """Materialize ``dst`` from ``src`` without needlessly duplicating bytes.

    During the sharded-to-by-profile migration both paths normally live on the
    same filesystem.  A hard link makes the move effectively instantaneous and
    keeps the operation resumable; the copy fallback handles custom media
    directories on another filesystem.  The caller only counts a successful
    materialization when the destination is a regular, non-empty file.
    """
    if dst.exists():
        return False
    if not src.is_file() or src.stat().st_size <= 0:
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.migrating")
    try:
        try:
            os.link(src, tmp)
        except OSError:
            shutil.copy2(src, tmp)
        tmp.replace(dst)
        return True
    except OSError:
        tmp.unlink(missing_ok=True)
        return False


def site_relative_media_path(path: str, out: Path = OUT) -> str:
    media_path_obj = Path(path)
    try:
        return str(media_path_obj.relative_to(out))
    except ValueError:
        return str(media_path_obj)


def media_source_path(path: str, out: Path = OUT) -> Path:
    media_path_obj = Path(path)
    if media_path_obj.is_absolute():
        return media_path_obj
    return media_path_obj if media_path_obj.parts[:1] == (out.name,) else out / media_path_obj


def inferred_media_path(
    shortid: str,
    post_type: str | None,
    idx: int,
    total: int,
    media_dir: str | Path | None = None,
    url: str | None = None,
    out: Path = OUT,
    profile: str | None = None,
) -> Path:
    ext = media_url_ext(post_type, url)
    name = f"{shortid}{ext}" if total == 1 else f"{shortid}_{idx + 1}{ext}"
    base = Path(media_dir) if media_dir else out / "images"
    if profile:
        return base / "by-profile" / profile / name
    return base / shortid[-2:].lower() / name


def site_media_path_for(source_path: Path, out: Path = OUT, profile: str | None = None) -> Path:
    if profile:
        return out / "images" / "by-profile" / profile / source_path.name
    # Legacy fallback for old sharded layout (no profile)
    return out / "images" / source_path.parent.name / source_path.name


def _legacy_sharded_media_path(
    shortid: str,
    post_type: str | None,
    idx: int,
    total: int,
    media_dir: str | Path | None = None,
    url: str | None = None,
    out: Path = OUT,
) -> Path:
    """Old sharded path for migration fallback."""
    ext = media_url_ext(post_type, url)
    name = f"{shortid}{ext}" if total == 1 else f"{shortid}_{idx + 1}{ext}"
    base = Path(media_dir) if media_dir else out / "images"
    return base / shortid[-2:].lower() / name


def page(title: str, body: str, include_script: bool = False, asset_prefix: str = "") -> str:
    script = f'<script src="{asset_prefix}assets/site.js" defer></script>' if include_script else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<link rel="stylesheet" href="{asset_prefix}assets/site.css">
{script}
</head>
<body>
{body}
</body>
</html>"""


def build_profile(username: str, posts: list[dict[str, Any]]) -> str:
    grid_items: list[str] = []
    label = posts[0].get("archive_label", f"@{username}") if posts else f"@{username}"

    for post in posts:
        shortid = post["shortid"]
        images = post["images"]
        if not images:
            continue

        escaped_shortid = escape(shortid)
        thumb = escape(post.get("thumb") or images[0])
        icon = (
            '<img class="carousel-icon" src="assets/carousel.svg" alt="" aria-hidden="true">' if len(images) > 1 else ""
        )
        if any(is_video_path(path) for path in images):
            icon += '<img class="play-icon" src="assets/play.svg" alt="" aria-hidden="true">'

        media = f'<img src="{thumb}" alt="" loading="lazy">'
        grid_items.append(
            f'<a class="grid-item" href="p/{escaped_shortid}" data-open-modal="{escaped_shortid}">{media}{icon}</a>'
        )

    body = (
        f'<header class="profile-header">'
        f'<a href="index.html" class="back">&#8592; All profiles</a>'
        f"<h1>{escape(label)}</h1>"
        f'<div class="post-count">{len(grid_items)} posts</div>'
        f"</header>"
        f'<main class="grid" data-posts-src="data/{escape(username)}.json">{"".join(grid_items)}</main>'
        f'<div class="modal" id="post-modal" aria-hidden="true">'
        f'<button class="modal-backdrop" type="button" data-close-modal aria-label="Close post"></button>'
        f'<div class="modal-content">'
        f'<button class="modal-close" type="button" data-close-modal aria-label="Close post">&#x2715;</button>'
        f'<div class="carousel" id="modal-carousel"></div>'
        f'<div class="modal-caption">'
        f'<div class="modal-author"></div>'
        f'<div class="modal-meta"></div>'
        f'<a class="modal-post-link" href="#" target="_blank" rel="noopener">View on Instagram</a>'
        f'<div class="modal-text"></div>'
        f"</div></div></div>"
    )

    return page(label, body, include_script=True)


def build_profile_data(posts: list[dict[str, Any]]) -> str:
    payload: dict[str, list[dict[str, Any]]] = {"posts": [post_payload(post) for post in posts if post["images"]]}
    return dumps(payload, ensure_ascii=False, separators=(",", ":"))


def post_payload(post: dict[str, Any]) -> dict[str, Any]:
    return {
        "shortid": post["shortid"],
        "post_type": post["post_type"],
        "author": post["caption"].get("author") or post.get("author") or "",
        "url": f"https://www.instagram.com/p/{post['shortid']}/",
        "media": [
            {
                "type": "video" if is_video_path(path) else "image",
                "src": path,
            }
            for path in post["images"]
        ],
        "meta": format_post_date(post.get("posted_at") or post["caption"]["posted_at"]),
        "caption": post["caption"]["text"],
    }


def _post_page_media(media: dict[str, str]) -> str:
    src = "../" + media["src"]
    if media["type"] == "video":
        return f'<video src="{escape(src)}" controls loop playsinline preload="metadata"></video>'
    return f'<img src="{escape(src)}" alt="" loading="lazy">'


def build_post_page(post: dict[str, Any]) -> str:
    payload = post_payload(post)
    author = f"@{payload['author']}" if payload["author"] else archive_label_from_key(post["archive_key"])
    title = f"{author} / {post['shortid']}"
    media = "".join(f'<figure class="single-post-media">{_post_page_media(item)}</figure>' for item in payload["media"])
    body = (
        f'<header class="profile-header">'
        f'<a href="../{escape(post["archive_key"])}.html" class="back">&#8592; {escape(post["archive_label"])}</a>'
        f"<h1>{escape(author)}</h1>"
        f'<a class="back" href="{escape(payload["url"])}" target="_blank" rel="noopener">Instagram</a>'
        f"</header>"
        f'<main class="single-post">'
        f'<section class="single-post-gallery">{media}</section>'
        f'<section class="single-post-caption">'
        f'<div class="modal-author">{escape(author)}</div>'
        f'<div class="modal-meta">{escape(payload["meta"])}</div>'
        f'<div class="modal-text">{escape(payload["caption"])}</div>'
        f"</section>"
        f"</main>"
    )
    return page(title, body, asset_prefix="../")


def archive_key(username: str, saved: int | bool) -> str:
    return f"{username}-saved" if saved else username


def archive_label_from_key(key: str) -> str:
    if key.endswith("-saved"):
        return f"@{key[:-6]} saved"
    return f"@{key}"


def build_index(usernames: list[str]) -> str:
    items = "".join(
        f'<li><a href="{escape(u)}.html">{escape(archive_label_from_key(u))}</a></li>' for u in sorted(usernames)
    )
    body = (
        '<header class="index-header"><h1>Instagram Archive</h1></header>'
        f'<main class="index-list"><ul>{items}</ul></main>'
    )
    return page("Instagram Archive", body)


def load_posts(
    db_path: str = DB,
    media_dir: str | Path | None = None,
    out: Path = OUT,
) -> dict[str, list[dict[str, Any]]]:
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    profile_post_cols = {row[1] for row in cur.execute("PRAGMA table_info(profile_post)")}
    saved_expr = "pp.saved" if "saved" in profile_post_cols else "0"
    cur.execute(
        f"""
        SELECT p.shortid, pp.username, {saved_expr} AS saved, p.username AS author,
               p.post_type, p.caption, p.scraped_at, m.idx, m.url
        FROM post AS p
        JOIN profile_post AS pp ON pp.shortid = p.shortid
        JOIN media AS m ON m.shortid = p.shortid
        ORDER BY pp.username, saved, pp.seq, p.shortid, m.idx
        """
    )
    rows = cur.fetchall()
    con.close()

    posts: dict[str, dict[str, Any]] = {}
    for shortid, username, saved, author, post_type, caption, scraped_at, idx, media_url in rows:
        key = archive_key(username, saved)
        post_key = f"{key}:{shortid}"
        existing: dict[str, Any] | None = posts.get(post_key)
        post: dict[str, Any]
        if existing is not None:
            post = existing
        else:
            post = {
                "shortid": shortid,
                "username": username,
                "author": author,
                "archive_key": key,
                "archive_label": archive_label_from_key(key),
                "post_type": post_type,
                "caption": parse_caption(caption),
                "posted_at": post_datetime_from_shortid(shortid),
                "scraped_at": scraped_at,
                "images": [],
                "image_sources": [],
                "media_indexes": [],
            }
            posts[post_key] = post
        post["media_indexes"].append((idx, media_url))

    for post in posts.values():
        total = len(post["media_indexes"])
        profile = post["archive_key"]
        for idx, media_url in post.pop("media_indexes"):
            source_path = inferred_media_path(
                post["shortid"],
                post["post_type"],
                idx,
                total,
                media_dir=media_dir,
                url=media_url,
                out=out,
                profile=profile,
            )
            site_path = site_media_path_for(source_path, out=out, profile=profile)
            post["images"].append(site_relative_media_path(str(site_path), out=out))
            post["image_sources"].append(str(source_path))

    by_user: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for post in posts.values():
        by_user[post["archive_key"]].append(post)

    for user_posts in by_user.values():
        user_posts.sort(
            key=lambda p: p["posted_at"] or p["caption"]["posted_at"] or datetime.min,
            reverse=True,
        )

    return by_user


def sync_assets(out: Path = OUT) -> int:
    copied = 0
    for name in ASSET_NAMES:
        if copy_if_changed(WEB / "assets" / name, out / "assets" / name):
            copied += 1
    return copied


def _migrate_by_profile_symlinks(out: Path = OUT) -> int:
    """Replace by-profile symlinks (old content-addressed layout) with regular files."""
    base = out / "images" / "by-profile"
    if not base.exists():
        return 0
    migrated = 0
    for profile_dir in list(base.iterdir()):
        if not profile_dir.is_dir() or profile_dir.is_symlink():
            continue
        for item in list(profile_dir.iterdir()):
            if not item.is_symlink():
                continue
            try:
                target_str = os.readlink(item)
            except OSError:
                continue
            target_path = (item.parent / target_str).resolve()
            # Fallback: item.resolve() may already point to the canonical file
            if not target_path.exists():
                try:
                    resolved = item.resolve()
                    if resolved.exists() and resolved.is_file():
                        target_path = resolved
                    else:
                        continue
                except OSError:
                    continue
            if not target_path.is_file():
                continue
            try:
                tmp = item.with_name(f".{item.name}.migrating")
                shutil.copy2(target_path, tmp)
                item.unlink()
                tmp.replace(item)
                migrated += 1
            except OSError:
                continue
    return migrated


def _clean_legacy_sharded_images(out: Path = OUT) -> int:
    """Delete old sharded image dirs (e.g. site/images/a1) after migration."""
    images_dir = out / "images"
    if not images_dir.exists():
        return 0
    removed = 0
    for entry in list(images_dir.iterdir()):
        # Keep unrelated files/directories in images/ (for example a manually
        # placed README or a future non-sharded asset).  Legacy shards are
        # exactly two characters from the shortcode alphabet.
        if entry.name == "by-profile" or len(entry.name) != 2 or any(
            char not in SHORTCODE_ALPHABET for char in entry.name
        ):
            continue
        try:
            if entry.is_symlink() or entry.is_file():
                entry.unlink()
                removed += 1
            elif entry.is_dir():
                shutil.rmtree(entry)
                removed += 1
        except OSError:
            pass
    return removed


def sync_media(by_user: dict[str, list[dict[str, Any]]], out: Path = OUT) -> tuple[int, int, int]:
    copied = 0
    missing = 0
    expected: set[Path] = set()

    # An empty database is not evidence that an existing archive is stale.  Do
    # not remove any media in that case; a later build can reconcile it once the
    # database is available again.
    if not by_user:
        return 0, 0, 0

    # Migrate old symlink layout before syncing.
    _migrate_by_profile_symlinks(out)

    for posts in by_user.values():
        for post in posts:
            shortid = post.get("shortid", "")
            for rel_path, source in zip(post["images"], post.get("image_sources", post["images"]), strict=False):
                dst = out / rel_path
                expected.add(dst)
                src = media_source_path(source, out=out)
                # If src is the new by-profile location but missing, try the
                # legacy sharded fallback.
                if (not src.is_file() or src.stat().st_size <= 0) and "by-profile" in src.parts:
                    # Legacy sharded file is at <media_base>/<shard>/<filename>
                    # while src is <media_base>/by-profile/<profile>/<filename>.
                    filename = src.name
                    shard = shortid[-2:].lower() if shortid else ""
                    parts = src.parts
                    profile_index = parts.index("by-profile")
                    media_base = Path(*parts[:profile_index]) if profile_index > 0 else Path(".")
                    legacy = media_base / shard / filename
                    # Hard links avoid a second 100+ GB copy when both paths
                    # share a volume.  _materialize_media falls back to an
                    # atomic copy for custom media directories.
                    if legacy.is_file() and legacy.stat().st_size > 0 and _materialize_media(legacy, src):
                        copied += 1
                if not src.is_file() or src.stat().st_size <= 0:
                    missing += 1
                elif src.resolve() != dst.resolve():
                    if _materialize_media(src, dst) or copy_if_changed(src, dst):
                        copied += 1
                else:
                    # src and dst are same file (typical when media_dir == out)
                    # Ensure dst exists as regular file (migrated) – nothing to copy
                    pass

    # Remove stale by-profile files and empty profile dirs only after all
    # referenced sources were found.  Keeping stale files on a partial run is
    # deliberate: they may be the only recoverable copy of an old download.
    removed = 0
    base = out / "images" / "by-profile"
    if missing == 0 and base.exists():
        for profile_dir in list(base.iterdir()):
            if not profile_dir.is_dir() or profile_dir.is_symlink():
                try:
                    if profile_dir.is_symlink() or profile_dir.is_file():
                        profile_dir.unlink()
                    elif profile_dir.is_dir():
                        shutil.rmtree(profile_dir)
                    removed += 1
                except OSError:
                    pass
                continue
            for child in list(profile_dir.iterdir()):
                if child not in expected:
                    try:
                        if child.is_symlink() or child.is_file():
                            child.unlink()
                        elif child.is_dir():
                            shutil.rmtree(child)
                        else:
                            child.unlink()
                        removed += 1
                    except OSError:
                        pass
            try:
                if not any(profile_dir.iterdir()):
                    profile_dir.rmdir()
            except OSError:
                pass
        # Remove base if empty
        try:
            if not any(base.iterdir()):
                base.rmdir()
        except OSError:
            pass

    # Purging legacy shards is safe only after every referenced destination was
    # materialized.  If even one source is missing, leave all old shards in
    # place so a retry can recover the remaining files.
    if missing == 0:
        legacy_removed = _clean_legacy_sharded_images(out)
        removed += legacy_removed

    return copied, removed, missing


def sync_by_profile(by_user: dict[str, list[dict[str, Any]]], out: Path = OUT) -> tuple[int, int]:
    """Deprecated: by-profile is now authoritative; migrates old symlinks."""
    # Old implementation created by-profile symlinks to sharded images.
    # Now media lives directly in by-profile; just migrate and clean.
    _migrate_by_profile_symlinks(out)
    _clean_legacy_sharded_images(out)
    return 0, 0


def thumb_path_for(media_path: str) -> Path:
    stem = Path(media_path).stem
    shortid = re.sub(r"_\d+$", "", stem)
    return Path("thumbs") / shortid[-2:].lower() / f"{stem}.webp"


def is_video_path(path: str) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTS


def _command_error(cmd: list[str], result: subprocess.CompletedProcess[bytes]) -> str:
    detail = (result.stderr or result.stdout or b"").decode(errors="replace").strip()
    rendered = " ".join(cmd)
    if detail:
        return f"{rendered}\n{detail}"
    return rendered


def run_quiet(cmd: list[str]) -> bool:
    result = subprocess.run(cmd, capture_output=True, check=False)
    return result.returncode == 0


def run_thumbnail_cmd(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, check=False)
    if result.returncode != 0:
        raise ThumbnailGenerationError(_command_error(cmd, result))


def ffmpeg_supports_encoder(encoder: str) -> bool:
    if not _FFMPEG:
        return False
    result = subprocess.run([_FFMPEG, "-hide_banner", "-encoders"], capture_output=True, check=False)
    if result.returncode != 0:
        raise ThumbnailGenerationError(_command_error([_FFMPEG, "-hide_banner", "-encoders"], result))
    needle = f" {encoder} "
    return any(needle in f" {line} " for line in result.stdout.decode(errors="replace").splitlines())


def sips_supports_webp_write() -> bool:
    if not (_IS_DARWIN and _SIPS):
        return False
    result = subprocess.run([_SIPS, "--formats"], capture_output=True, check=False)
    if result.returncode != 0:
        raise ThumbnailGenerationError(_command_error([_SIPS, "--formats"], result))
    for line in result.stdout.decode(errors="replace").splitlines():
        if "webp" in line.lower():
            return "Writable" in line
    return False


def thumbnail_backend() -> ThumbnailBackend | None:
    if not _FFMPEG:
        return None
    if ffmpeg_supports_encoder("libwebp"):
        return ThumbnailBackend("ffmpeg-webp")
    if not ffmpeg_supports_encoder("mjpeg"):
        raise ThumbnailGenerationError(f"{_FFMPEG} supports neither libwebp nor mjpeg thumbnail output")
    if sips_supports_webp_write():
        return ThumbnailBackend("ffmpeg-jpg-sips")
    if _CWEBP:
        return ThumbnailBackend("ffmpeg-jpg-cwebp")
    raise ThumbnailGenerationError(f"{_FFMPEG} does not support libwebp and no WebP converter is available")


def temp_output_path(dst: Path, suffix: str) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(delete=False, dir=dst.parent, prefix=f".{dst.stem}.", suffix=suffix) as fh:
        path = Path(fh.name)
    path.unlink(missing_ok=True)
    return path


def replace_if_changed(tmp: Path, dst: Path, *, touch_when_unchanged: bool = False) -> bool:
    data = tmp.read_bytes()
    changed = write_if_changed(dst, data)
    if not changed and touch_when_unchanged:
        dst.touch()
    tmp.unlink(missing_ok=True)
    return changed


def ffmpeg_base_cmd(seek: str | None = None) -> list[str]:
    cmd = [_FFMPEG or "ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if seek is not None:
        cmd.extend(["-ss", seek])
    return cmd


def generate_webp_with_ffmpeg(src: Path, dst: Path, seek: str | None = None) -> bool:
    tmp = temp_output_path(dst, ".webp")
    try:
        run_thumbnail_cmd(
            [
                *ffmpeg_base_cmd(seek),
                "-i",
                str(src),
                "-vf",
                f"scale='min({THUMB_MAX},iw)':'min({THUMB_MAX},ih)':force_original_aspect_ratio=decrease",
                "-frames:v",
                "1",
                "-c:v",
                "libwebp",
                "-quality",
                str(THUMB_QUALITY),
                str(tmp),
            ]
        )
        return replace_if_changed(tmp, dst, touch_when_unchanged=True)
    finally:
        tmp.unlink(missing_ok=True)


def generate_jpg_with_ffmpeg(src: Path, tmp: Path, seek: str | None = None) -> None:
    run_thumbnail_cmd(
        [
            *ffmpeg_base_cmd(seek),
            "-i",
            str(src),
            "-vf",
            f"scale='min({THUMB_MAX},iw)':'min({THUMB_MAX},ih)':force_original_aspect_ratio=decrease",
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(tmp),
        ]
    )


def convert_jpg_to_webp(src: Path, dst: Path, backend: ThumbnailBackend) -> None:
    if backend.method == "ffmpeg-jpg-sips":
        run_thumbnail_cmd([_SIPS or "sips", "-s", "format", "webp", str(src), "--out", str(dst)])
    elif backend.method == "ffmpeg-jpg-cwebp":
        run_thumbnail_cmd([_CWEBP or "cwebp", "-quiet", "-q", str(THUMB_QUALITY), str(src), "-o", str(dst)])
    else:
        raise ThumbnailGenerationError(f"unsupported thumbnail backend: {backend.method}")


def generate_webp_via_jpg(src: Path, dst: Path, backend: ThumbnailBackend, seek: str | None = None) -> bool:
    jpg = temp_output_path(dst, ".jpg")
    webp = temp_output_path(dst, ".webp")
    try:
        generate_jpg_with_ffmpeg(src, jpg, seek=seek)
        convert_jpg_to_webp(jpg, webp, backend)
        return replace_if_changed(webp, dst, touch_when_unchanged=True)
    finally:
        jpg.unlink(missing_ok=True)
        webp.unlink(missing_ok=True)


def generate_frame_thumbnail(src: Path, dst: Path, backend: ThumbnailBackend, seek: str | None = None) -> bool:
    if backend.method == "ffmpeg-webp":
        return generate_webp_with_ffmpeg(src, dst, seek=seek)
    return generate_webp_via_jpg(src, dst, backend, seek=seek)


def generate_video_thumbnail(src: Path, dst: Path, backend: ThumbnailBackend) -> bool:
    failures: list[str] = []
    for seek in ("1", "0"):
        try:
            return generate_frame_thumbnail(src, dst, backend, seek=seek)
        except ThumbnailGenerationError as exc:
            failures.append(str(exc))
    raise ThumbnailGenerationError("\n".join(failures))


def generate_image_thumbnail(src: Path, dst: Path, backend: ThumbnailBackend) -> bool:
    return generate_frame_thumbnail(src, dst, backend)


def generate_thumbnail(src: Path, dst: Path, backend: ThumbnailBackend | None = None) -> bool:
    backend = backend or thumbnail_backend()
    if backend is None:
        return False
    if is_video_path(str(src)):
        return generate_video_thumbnail(src, dst, backend)
    return generate_image_thumbnail(src, dst, backend)


def thumbnail_workers(total_needed: int) -> int:
    if total_needed <= 1:
        return 1
    return max(1, min(total_needed, cpu_count() or 1))


def _generate_thumbnail_task(
    task: tuple[int, dict[str, Any], Path, Path, ThumbnailBackend],
) -> tuple[int, dict[str, Any], Path, bool]:
    index, post, src, dst, backend = task
    return index, post, dst, generate_thumbnail(src, dst, backend)


def _print_thumbnail_progress(done: int, total: int, *, final: bool = False) -> None:
    end = "\n" if final else "\r"
    print(f"  thumbnails: {done}/{total}", end=end, flush=True)


def sync_thumbnails(
    by_user: dict[str, list[dict[str, Any]]],
    out: Path = OUT,
    progress: bool = False,
    workers: int | None = None,
) -> tuple[int, int, int, int]:
    referenced: set[Path] = set()
    generated = 0
    missing = 0
    failed = 0

    thumbnails_needed: list[tuple[dict[str, Any], Path, Path]] = []
    queued: set[Path] = set()
    for posts in by_user.values():
        for post in posts:
            if not post["images"]:
                continue
            source = post.get("image_sources", post["images"])[0]
            src = media_source_path(source, out=out)
            rel_thumb = thumb_path_for(post["images"][0])
            post["thumb"] = str(rel_thumb)
            referenced.add(rel_thumb)
            if not src.exists():
                post["thumb"] = post["images"][0]
                missing += 1
                continue
            dst = out / rel_thumb
            if dst.exists():
                continue
            if rel_thumb in queued:
                continue
            queued.add(rel_thumb)
            thumbnails_needed.append((post, src, dst))

    total_needed = len(thumbnails_needed)
    backend = thumbnail_backend() if total_needed else None
    if total_needed and backend is None:
        raise ThumbnailGenerationError("ffmpeg is required to generate thumbnails")
    if total_needed:
        (out / "thumbs").mkdir(parents=True, exist_ok=True)
    worker_count = workers if workers is not None else thumbnail_workers(total_needed)
    tasks = [(i, post, src, dst, backend) for i, (post, src, dst) in enumerate(thumbnails_needed, 1) if backend]
    if progress and total_needed > 1:
        print(f"  using {worker_count} thumbnail workers", flush=True)
    if progress and total_needed:
        _print_thumbnail_progress(0, total_needed)

    if worker_count <= 1:
        for done, (_i, _post, dst, ok) in enumerate(map(_generate_thumbnail_task, tasks), 1):
            if progress:
                _print_thumbnail_progress(done, total_needed, final=done == total_needed)
            if ok:
                generated += 1
            elif not dst.exists():
                raise ThumbnailGenerationError(f"thumbnail generation failed for {dst}")
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(_generate_thumbnail_task, task) for task in tasks]
            for done, future in enumerate(as_completed(futures), 1):
                _i, _post, dst, ok = future.result()
                if progress:
                    _print_thumbnail_progress(done, total_needed, final=done == total_needed)
                if ok:
                    generated += 1
                elif not dst.exists():
                    raise ThumbnailGenerationError(f"thumbnail generation failed for {dst}")

    removed = 0
    dst_thumbs = out / "thumbs"
    if dst_thumbs.exists():
        referenced_lower = {str(r).lower() for r in referenced}
        for path in dst_thumbs.rglob("*"):
            if path.is_file() and str(path.relative_to(out)).lower() not in referenced_lower:
                path.unlink()
                removed += 1

    return generated, removed, missing, failed


def sync_post_pages(by_user: dict[str, list[dict[str, Any]]], out: Path = OUT) -> tuple[int, int]:
    written = 0
    referenced: set[Path] = set()
    posts_by_shortid: dict[str, dict[str, Any]] = {}
    for posts in by_user.values():
        for post in posts:
            if post["images"]:
                posts_by_shortid.setdefault(post["shortid"], post)

    for shortid, post in posts_by_shortid.items():
        rel_path = Path("p") / shortid
        referenced.add(rel_path)
        if write_if_changed(out / rel_path, build_post_page(post)):
            written += 1

    removed = 0
    dst_posts = out / "p"
    if dst_posts.exists():
        for path in dst_posts.rglob("*"):
            if path.is_file() and path.relative_to(out) not in referenced:
                path.unlink()
                removed += 1

    return written, removed


def sync_profile_data(by_user: dict[str, list[dict[str, Any]]], out: Path = OUT) -> tuple[int, int]:
    written = 0
    referenced = {Path("data") / f"{username}.json" for username in by_user}
    for username, posts in by_user.items():
        if write_if_changed(out / "data" / f"{username}.json", build_profile_data(posts)):
            written += 1

    removed = 0
    dst_data = out / "data"
    if dst_data.exists():
        for path in dst_data.rglob("*.json"):
            if path.relative_to(out) not in referenced:
                path.unlink()
                removed += 1

    return written, removed


def build(
    media_dir: str | Path | None = None,
    db_path: str = DB,
    out: Path = OUT,
    verbose: bool = True,
) -> None:
    out.mkdir(exist_ok=True)

    if verbose:
        print(f"Building site from {db_path}", flush=True)
        if media_dir:
            print(f"  media dir: {media_dir}", flush=True)

    if verbose:
        print("Loading posts...", flush=True)
    by_user = load_posts(db_path=db_path, media_dir=media_dir, out=out)
    total_posts = sum(len(posts) for posts in by_user.values())
    if verbose:
        print(f"  {len(by_user)} profiles, {total_posts} posts loaded", flush=True)

    if verbose:
        print("Copying assets...", flush=True)
    assets_copied = sync_assets(out=out)
    if verbose:
        print(f"  {assets_copied} updated", flush=True)

    if verbose:
        print("Syncing media...", flush=True)
    media_copied, media_removed, media_missing = sync_media(by_user, out=out)
    if verbose:
        print(f"  {media_copied} copied, {media_removed} removed, {media_missing} missing", flush=True)

    # Media migration runs first so thumbnail discovery sees the authoritative
    # by-profile files.  Running this in the opposite order makes a legacy
    # archive look entirely missing and causes every profile grid to fall back
    # to loading full-size images.
    if verbose:
        print("Generating thumbnails...", flush=True)
    thumbs_generated, thumbs_removed, thumbs_missing, thumbs_failed = sync_thumbnails(
        by_user, out=out, progress=verbose
    )
    if verbose:
        print(f"  {thumbs_generated} generated, {thumbs_missing + thumbs_failed} failed/missing", flush=True)

    if verbose:
        print("Writing profile data...", flush=True)
    data_written, data_removed = sync_profile_data(by_user, out=out)
    if verbose:
        print(f"  {data_written} written, {data_removed} removed", flush=True)

    if verbose:
        print("Generating pages...", flush=True)
    html_written = 0
    post_pages_written, post_pages_removed = sync_post_pages(by_user, out=out)
    html_written += post_pages_written
    for username, posts in by_user.items():
        if write_if_changed(out / f"{username}.html", build_profile(username, posts)):
            html_written += 1
        if verbose:
            print(f"  {username}.html  ({len(posts)} posts)", flush=True)

    if write_if_changed(out / "index.html", build_index(list(by_user.keys()))):
        html_written += 1
    if verbose:
        print(f"  index.html  ({len(by_user)} profiles)", flush=True)
        print(f"  p/<shortid>  ({post_pages_written} updated)", flush=True)

    if verbose:
        stale = media_removed + thumbs_removed + data_removed + post_pages_removed
        print(
            f"\nSite written to {out}/ "
            f"({html_written} html, {data_written} data, {assets_copied} assets, "
            f"{thumbs_generated} thumbs generated, {media_copied} media copied, "
            f"{stale} stale removed, "
            f"{media_missing + thumbs_missing} missing, {thumbs_failed} thumb failures)",
            flush=True,
        )


def main(media_dir: str | Path | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Build a static Instagram-like site from instagram.db")
    parser.add_argument("--media-dir", default=None, help="directory containing downloaded media files")
    args = parser.parse_args()
    build(media_dir=args.media_dir or media_dir)
