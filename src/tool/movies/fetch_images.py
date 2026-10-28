import hashlib
import http.client
import os
import sqlite3
import subprocess
import tempfile
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

from tool.movies.paths import data_dir, db_path

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
WORKERS = 8
COMMIT_EVERY = 200

_local = threading.local()


def img_dir() -> Path:
    return data_dir() / "static" / "img"


def _conn(host: str) -> http.client.HTTPSConnection:
    stored = getattr(_local, "conns", None)
    if stored is None:
        stored = {}
        _local.conns = stored
    conns = cast("dict[str, http.client.HTTPSConnection]", stored)
    if host not in conns:
        conns[host] = http.client.HTTPSConnection(host, timeout=30)
    return conns[host]


def _fetch(url: str) -> bytes:
    p = urlparse(url)
    path = p.path + (f"?{p.query}" if p.query else "")
    for attempt in range(2):
        try:
            conn = _conn(p.netloc)
            conn.request("GET", path, headers={"User-Agent": UA, "Connection": "keep-alive"})
            resp = conn.getresponse()
            data = resp.read()
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            return data
        except RuntimeError:
            raise
        except Exception:
            conns = getattr(_local, "conns", None)
            if conns is not None:
                conns.pop(p.netloc, None)
            if attempt == 1:
                raise
    raise RuntimeError(f"unreachable: {url}")


def download_and_convert(url: str, out_dir: Path | None = None) -> str:
    out = (out_dir or img_dir()) / f"{hashlib.sha256(url.encode()).hexdigest()[:24]}.webp"
    if out.exists():
        return out.name

    ext = os.path.splitext(urlparse(url).path)[-1] or ".jpg"
    data = _fetch(url)

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(data)
        tmppath = tmp.name
    try:
        result = subprocess.run(
            ["cwebp", "-q", "85", tmppath, "-o", str(out)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"cwebp exit {result.returncode}: {result.stderr.strip() or result.stdout.strip()}")
    finally:
        try:
            os.unlink(tmppath)
        except OSError:
            pass

    return out.name


def _pending_work(
    rows: list[tuple[str, str | None, str | None, str | None, str | None]],
    out_dir: Path,
) -> list[tuple[str, str, str]]:
    work = []
    for slug, backdrop_url, poster_url, backdrop_local, poster_local in rows:
        for col, url, local in (
            ("backdrop_local", backdrop_url, backdrop_local),
            ("poster_local", poster_url, poster_local),
        ):
            if url is None or "empty-poster" in url:
                continue
            if local is not None and local != "" and (out_dir / local).exists():
                continue
            work.append((slug, col, url))
    return work


def run(workers: int = WORKERS) -> None:
    out_dir = img_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path())
    rows = db.execute(
        "SELECT slug, backdrop_url, poster_url, backdrop_local, poster_local FROM movie WHERE backdrop_url IS NOT NULL OR poster_url IS NOT NULL"
    ).fetchall()

    total = len(rows)
    done = 0
    pending: dict[str, dict[str, str]] = defaultdict(dict)

    def _flush() -> None:
        nonlocal done
        for s, updates in pending.items():
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            db.execute(f"UPDATE movie SET {set_clause} WHERE slug = ?", [*updates.values(), s])
            done += 1
        db.commit()
        pending.clear()
        print(f"\r{done}/{total}", end="", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(download_and_convert, url, out_dir): (slug, col)
            for slug, col, url in _pending_work(rows, out_dir)
        }

        for fut in as_completed(futures):
            slug, col = futures[fut]
            try:
                pending[slug][col] = fut.result()
            except Exception as e:
                print(f"\nFAIL {col} {slug}: {e}")

            if len(pending) >= COMMIT_EVERY:
                _flush()

    if pending:
        _flush()

    print(f"\r{done}/{total}")
    print(f"Finished. done={done}")
    db.close()
