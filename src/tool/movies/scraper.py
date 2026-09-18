import asyncio
import csv
import json
import re
import signal
import sqlite3
import sys
import time
import traceback
import urllib.request
import zlib
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from pathlib import Path
from typing import Any

import shadowdriver
import uvloop

from tool.movies.paths import db_path

DB_PATH = db_path


def compress(s: str) -> bytes:
    obj = zlib.compressobj(wbits=-15)
    return obj.compress(s.encode()) + obj.flush()


def decompress(b: bytes) -> str:
    return zlib.decompress(b, wbits=-15).decode()


def init_db() -> sqlite3.Connection:
    path = DB_PATH()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS movie (slug TEXT PRIMARY KEY)")
    # Discovery frontier: person pages (director/actor/...) expanded into new
    # film slugs. Together with movie.enriched_at IS NULL, these NULL frontiers
    # are the resume pointers — a restart picks up exactly where it left off.
    db.execute("CREATE TABLE IF NOT EXISTS person (path TEXT PRIMARY KEY, expanded_at INTEGER)")
    existing = {row[1] for row in db.execute("PRAGMA table_info(movie)")}

    def add_col(col: str, typ: str) -> None:
        if col not in existing:
            db.execute(f'ALTER TABLE movie ADD COLUMN "{col}" {typ}')

    add_col("title", "TEXT")
    add_col("alt_title", "TEXT")
    add_col("year", "INTEGER")
    add_col("directors", "BLOB")
    add_col("cast", "BLOB")
    add_col("synopsis", "BLOB")
    add_col("rating", "REAL")
    add_col("rating_count", "INTEGER")
    add_col("rating_dist", "BLOB")
    add_col("fans", "INTEGER")
    add_col("watched_by", "INTEGER")
    add_col("liked_by", "INTEGER")
    add_col("imdb_id", "TEXT")
    add_col("tmdb_id", "TEXT")
    add_col("studios", "BLOB")
    add_col("crew", "BLOB")
    add_col("backdrop_url", "TEXT")
    add_col("poster_url", "TEXT")
    add_col("enriched_at", "INTEGER")
    # Fix existing rows where year was stored as a float text (e.g. '2025.0').
    # The legacy Waterfox BiDi client serialized JS numbers as doubles in JSON.
    cur = db.execute(
        "UPDATE movie SET year = CAST(CAST(year AS REAL) AS INTEGER)"
        " WHERE year LIKE '%.%'"
    )
    if cur.rowcount:
        print(f"  fixed {cur.rowcount} movie{'s' if cur.rowcount != 1 else ''} with float-formatted year", flush=True)
    db.commit()
    return db


_READY_JS = """(function() {
    if (!document.querySelector('.production-statistic')) return false;
    var avg = document.querySelector('a.averagerating');
    if (avg && (!avg.getAttribute('data-original-title')
             || !document.querySelector('.rating-histogram .chart .column'))) return false;
    return true;
})()"""


async def wait_page_ready(tab, timeout: float = 5) -> None:
    # Logged-out sessions never get rating stats (no averagerating anchor,
    # histogram, or production statistics), so skip that wait entirely.
    logged_out = await tab.evaluate("document.body.classList.contains('logged-out')")
    if logged_out:
        return
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        if await tab.evaluate(_READY_JS):
            return
        if loop.time() >= end:
            return
        await asyncio.sleep(0.1)


_SCRAPE_JS = r"""
(function() {
    function hrefSlug(href, type) {
        var m = href.match(new RegExp('/' + type + '/([^/]+)/'));
        return m ? m[1] : null;
    }

    var titleEl = document.querySelector('#film-page-wrapper h1');
    var title = titleEl ? titleEl.textContent.trim() : null;

    var altEl = document.querySelector('[class*=original]');
    var altTitle = altEl ? altEl.textContent.trim() : null;

    var yearEl = document.querySelector('a[href*="/films/year/"]');
    var year = yearEl ? parseInt(yearEl.textContent.trim(), 10) : null;

    var directors = [...new Set(
        Array.from(document.querySelectorAll('a[href*="/director/"]'))
            .map(function(a) { return hrefSlug(a.getAttribute('href'), 'director'); })
            .filter(Boolean)
    )];

    var cast = Array.from(document.querySelectorAll(
        '#tab-panel-cast a[href*="/actor/"], #tab-cast a[href*="/actor/"]'
    ))
        .map(function(a) { return hrefSlug(a.getAttribute('href'), 'actor'); })
        .filter(Boolean);

    var synEl = document.querySelector('.production-synopsis .truncate p');
    var synopsis = synEl ? synEl.textContent.trim() : null;

    var rating = null;
    var ratingCount = null;
    var avgEl = document.querySelector('a.averagerating');
    if (avgEl) {
        var tip = avgEl.getAttribute('data-original-title') || '';
        var m = tip.match(/Weighted average of ([\d.]+) based on ([\d,]+)/);
        if (m) {
            rating = parseFloat(m[1]);
            ratingCount = parseInt(m[2].replace(/,/g, ''), 10);
        }
    }

    var ratingDist = Array.from(
        document.querySelectorAll('.rating-histogram .chart .plot tr.column')
    ).map(function(row) {
        var anchor = row.querySelector('a.barcolumn');
        if (!anchor) return 0;
        var tip = anchor.getAttribute('data-original-title') || '';
        var m = tip.match(/^([\d,]+)/);
        return m ? parseInt(m[1].replace(/,/g, ''), 10) : 0;
    });

    var fans = null;
    var fansEl = document.querySelector('.ratings-histogram-chart a[href*="/fans/"], a[href*="/fans/"]');
    if (fansEl) {
        // Counts may be abbreviated ("195K fans", "1.2M fans") or full ("12,345 fans")
        var m = fansEl.textContent.trim().match(/^([\d,.]+)\s*([KMB])?/i);
        if (m) {
            var mult = { K: 1e3, M: 1e6, B: 1e9 }[(m[2] || '').toUpperCase()] || 1;
            fans = Math.round(parseFloat(m[1].replace(/,/g, '')) * mult);
        }
    }

    function parseStatLabel(sel) {
        var el = document.querySelector(sel);
        if (!el) return null;
        var label = el.getAttribute('aria-label') || '';
        var m = label.match(/([\d,]+)/);
        return m ? parseInt(m[1].replace(/,/g, ''), 10) : null;
    }
    var watchedBy = parseStatLabel('.production-statistic.-watches');
    var likedBy = parseStatLabel('.production-statistic.-likes');

    var imdbId = null;
    var tmdbId = null;
    var imdbBtn = document.querySelector('a.micro-button[data-track-action="IMDb"]');
    if (imdbBtn) {
        var m = imdbBtn.getAttribute('href').match(/(tt\d+)/);
        if (m) imdbId = m[1];
    }
    var tmdbBtn = document.querySelector('a.micro-button[data-track-action="TMDB"]');
    if (tmdbBtn) {
        var m = tmdbBtn.getAttribute('href').match(/\/movie\/(\d+)/);
        if (m) tmdbId = m[1];
    }

    var studios = Array.from(document.querySelectorAll(
        '#tab-panel-details a[href*="/studio/"], #tab-details a[href*="/studio/"]'
    ))
        .map(function(a) { return hrefSlug(a.getAttribute('href'), 'studio'); })
        .filter(Boolean);

    var crew = [];
    // NOTE: #tab-crew is the tab button (it precedes the panel in document
    // order), so the panel selector must come first.
    var crewTab = document.querySelector('#tab-panel-crew') || document.querySelector('#tab-crew');
    if (crewTab) {
        var currentRole = null;
        for (var i = 0; i < crewTab.children.length; i++) {
            var child = crewTab.children[i];
            if (child.tagName === 'H3') {
                var roleEl = child.querySelector('.crewrole.-full');
                currentRole = roleEl ? roleEl.textContent.trim() : child.textContent.trim();
            } else if (child.classList.contains('text-sluglist') && currentRole) {
                var anchors = child.querySelectorAll('a[href]');
                for (var j = 0; j < anchors.length; j++) {
                    var href = anchors[j].getAttribute('href');
                    var m = href.match(/\/([^/]+)\/?$/);
                    var personSlug = m ? m[1] : null;
                    if (personSlug) crew.push({ role: currentRole, slug: personSlug });
                }
            }
        }
    }

    var backdropUrl = null;
    var backdropEl = document.querySelector('#backdrop');
    if (backdropEl) {
        backdropUrl = backdropEl.getAttribute('data-backdrop') ||
                      backdropEl.getAttribute('data-backdrop-image') ||
                      backdropEl.getAttribute('data-src');
        if (!backdropUrl) {
            var style = backdropEl.getAttribute('style') || '';
            var m = style.match(/url\(["']?([^"')]+)["']?\)/);
            if (m) backdropUrl = m[1];
        }
    }
    if (!backdropUrl) {
        var metaImg = document.querySelector('meta[property="og:image"]');
        if (metaImg) backdropUrl = metaImg.getAttribute('content');
    }

    var posterUrl = null;
    var posterImg = document.querySelector('#js-poster-col .film-poster img')
                 || document.querySelector('.film-poster img');
    if (posterImg) {
        var src = posterImg.getAttribute('src') || posterImg.getAttribute('data-src')
               || (posterImg.getAttribute('srcset') || '').split(' ')[0] || '';
        if (src && !src.includes('empty-poster')) {
            posterUrl = src.replace(/0-\d+-0-\d+(-crop)/, '0-1000-0-1500$1');
        }
    }

    // Deterministic fallback: the poster CDN parameters are server-rendered
    // in data attributes, so no LazyPoster hydration (or tab focus) is needed.
    if (!posterUrl) {
        try {
            var resEl = document.querySelector('[data-resolvable-poster-path]');
            var identEl = document.querySelector('[data-postered-identifier]');
            var meta = resEl && JSON.parse(resEl.getAttribute('data-resolvable-poster-path'));
            var ident = identEl && JSON.parse(identEl.getAttribute('data-postered-identifier'));
            var filmId = ident && (ident.uid || '').replace('film:', '');
            var key = meta && meta.cacheBustingKey;
            var base = meta && (meta.posteredBaseLink || '').match(/\/film\/([^/]+)\//);
            if (filmId && key && base) {
                var digits = filmId.split('').join('/');
                posterUrl = 'https://a.ltrbxd.com/resized/film-poster/' + digits + '/'
                    + filmId + '-' + base[1] + '-0-1000-0-1500-crop.jpg?v=' + key;
            }
        } catch (e) {}
    }

    var posterApiUrl = null;
    if (!posterUrl) {
        var lazyEl = document.querySelector('#js-poster-col .react-component[data-component-class="LazyPoster"]');
        if (lazyEl) posterApiUrl = lazyEl.getAttribute('data-details-endpoint');
    }

    // Person pages (director/actor/...) expanded by the discovery crawl into
    // new film slugs. NOTE: #tab-crew is the tab button, so the panel
    // selector comes first (see crewTab above).
    function personPath(href) {
        var m = (href || '').match(/^\/([a-z-]+\/[^/]+)\/?$/);
        return m ? m[1] : null;
    }

    var people = [...new Set(
        Array.from(document.querySelectorAll(
            'a[href*="/director/"],'
            + ' #tab-panel-cast a[href*="/actor/"], #tab-cast a[href*="/actor/"],'
            + ' #tab-panel-crew a[href], #tab-crew a[href]'
        ))
            .map(function(a) { return personPath(a.getAttribute('href')); })
            .filter(Boolean)
    )];

    return {
        title: title,
        altTitle: altTitle,
        year: year,
        directors: directors,
        cast: cast,
        synopsis: synopsis,
        rating: rating,
        ratingCount: ratingCount,
        ratingDist: ratingDist,
        fans: fans,
        watchedBy: watchedBy,
        likedBy: likedBy,
        imdbId: imdbId,
        tmdbId: tmdbId,
        studios: studios,
        crew: crew,
        backdropUrl: backdropUrl,
        posterUrl: posterUrl,
        posterApiUrl: posterApiUrl,
        people: people,
    };
})()
"""


async def wait_for_selector(tab, selector: str, timeout: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    selector_json = json.dumps(selector)
    while True:
        if await tab.evaluate(f"!!document.querySelector({selector_json})"):
            return
        if loop.time() >= end:
            raise TimeoutError(f"timed out waiting for selector: {selector}")
        await asyncio.sleep(0.1)


async def scrape_movie(tab, slug: str) -> dict[str, Any]:
    # Foreground the tab: sibling worker tabs leave this one backgrounded,
    # and background tabs don't fire the IntersectionObserver that hydrates
    # the LazyPoster (poster stays an empty-poster placeholder forever).
    await tab.activate()
    timed_out = True
    for attempt in range(3):
        if attempt > 0:
            await shadowdriver.sleep(2**attempt * 1000)
        try:
            await tab.navigate(f"https://letterboxd.com/film/{slug}/")
            await wait_for_selector(tab, "#film-page-wrapper", timeout=15)
            timed_out = False
            break
        except TimeoutError:
            pass
    if timed_out:
        raise RuntimeError(f"timed out loading /film/{slug}/")

    # stats and rating widgets are injected by JS after initial render
    await wait_page_ready(tab)

    info = await tab.evaluate(_SCRAPE_JS)

    # LazyPoster React component may not hydrate in scraper context.
    # Reconstruct the CDN poster URL from the data-resolvable-poster-path attributes.
    if not info.get("posterUrl") and info.get("posterApiUrl"):
        cdn_url = await tab.evaluate(
            f"""
            (async function() {{
                try {{
                    var r = await fetch("/film/{slug}/image-125/");
                    var html = await r.text();
                    var doc = new DOMParser().parseFromString(html, 'text/html');
                    var el = doc.querySelector('[data-resolvable-poster-path]');
                    if (!el) return null;
                    var meta = JSON.parse(el.getAttribute('data-resolvable-poster-path'));
                    var key = meta.cacheBustingKey;
                    var uid = JSON.parse(el.getAttribute('data-postered-identifier') || '{{}}').uid || '';
                    var filmId = uid.replace('film:', '');
                    if (!filmId || !key) return null;
                    var digits = filmId.split('').join('/');
                    return 'https://a.ltrbxd.com/resized/film-poster/' + digits + '/' + filmId + '-{slug}-0-1000-0-1500-crop.jpg?v=' + key;
                }} catch(e) {{ return null; }}
            }})()
        """,
        )
        if cdn_url and cdn_url.startswith("https://"):
            info["posterUrl"] = cdn_url

    return info


def validate(info: dict[str, Any]) -> list[str]:
    warnings: list[str] = []

    if not info.get("title"):
        warnings.append("missing title")
    year = info.get("year")
    if year is not None and not isinstance(year, int):
        warnings.append(f"year is {type(year).__name__} ({json.dumps(year)}), will be coerced to int")
    elif not year or year < 1880 or year > 2100:
        warnings.append(f"unexpected year: {json.dumps(year)}")
    if not info.get("directors"):
        warnings.append("no directors")
    if not info.get("synopsis"):
        warnings.append("missing synopsis")
    rating = info.get("rating")
    if rating is not None and (not isinstance(rating, (int, float)) or rating < 0.5 or rating > 5):
        warnings.append(f"unexpected rating: {rating}")
    dist = info.get("ratingDist") or []
    if info.get("rating") is not None and len(dist) != 10:
        warnings.append(f"ratingDist has {len(dist)} entries (expected 10)")
    if info.get("fans") is not None and not isinstance(info["fans"], int):
        warnings.append(f"unexpected fans: {info['fans']}")
    if info.get("watchedBy") is not None and not isinstance(info["watchedBy"], int):
        warnings.append(f"unexpected watchedBy: {info['watchedBy']}")
    if info.get("likedBy") is not None and not isinstance(info["likedBy"], int):
        warnings.append(f"unexpected likedBy: {info['likedBy']}")
    imdb_id = info.get("imdbId")
    if imdb_id is not None and not (isinstance(imdb_id, str) and imdb_id.startswith("tt")):
        warnings.append(f"unexpected imdbId: {imdb_id}")
    tmdb_id = info.get("tmdbId")
    if tmdb_id is not None and not (isinstance(tmdb_id, str) and tmdb_id.isdigit()):
        warnings.append(f"unexpected tmdbId: {tmdb_id}")
    if not info.get("backdropUrl"):
        warnings.append("missing backdropUrl")
    if not info.get("posterUrl"):
        warnings.append("missing posterUrl")
    if not info.get("crew"):
        warnings.append("no crew")
    if not info.get("studios"):
        warnings.append("no studios")

    return warnings


async def login() -> None:
    profile_dir = await shadowdriver.default_profile_dir()
    print(f"Profile: {profile_dir}", flush=True)
    print("Log in to Letterboxd, then close the window.", flush=True)
    try:
        await shadowdriver.launch_browser_window(url="https://letterboxd.com/")
        await shadowdriver.wait_browser_window_exit()
    finally:
        await shadowdriver.kill()


async def kill_browser() -> None:
    profile_dir = await shadowdriver.default_profile_dir()
    await shadowdriver.kill()
    print(f"browser stopped ({profile_dir})")


SEED_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
SEED_WORKERS = 8


def slug_from_uri(uri: str) -> str | None:
    """Extract a /film/<slug>/ slug from a Letterboxd URL, if present."""
    m = re.search(r"letterboxd\.com/film/([^/?#]+)/?", uri)
    if m is None:
        return None
    slug = m.group(1)
    assert isinstance(slug, str)
    return slug


def _resolve_boxd(uri: str, timeout: float = 20.0) -> str | None:
    """Follow a boxd.it short link to its /film/<slug>/ page and return the slug."""
    try:
        req = urllib.request.Request(uri, headers={"User-Agent": SEED_UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return slug_from_uri(resp.geturl())
    except Exception:
        return None


def seed(csv_path: str | Path) -> None:
    """Seed unenriched movie slugs from a Letterboxd diary/watchlist CSV export.

    The export's URI column holds boxd.it short links (diary) or full
    letterboxd.com/film/ URLs (watchlist); short links are resolved over
    plain HTTP — no browser needed. Bare `scrape` picks up the new rows.
    """
    path = Path(csv_path).expanduser()
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print(f"No data rows in {path}.")
        return
    headers = list(rows[0].keys())
    uri_col = next((h for h in headers if "uri" in h.lower()), None)
    if uri_col is None:
        uri_col = next((h for h in headers if "letterboxd" in h.lower()), None)
    if uri_col is None:
        print(f"No URI column found in {path} (headers: {', '.join(headers)}).")
        return

    direct: set[str] = set()
    short: set[str] = set()
    for row in rows:
        uri = (row.get(uri_col) or "").strip()
        if not uri:
            continue
        slug = slug_from_uri(uri)
        if slug:
            direct.add(slug)
        elif "boxd.it" in uri:
            short.add(uri)

    resolved: set[str] = set()
    failed: list[str] = []
    if short:
        print(f"Resolving {len(short)} boxd.it links...", flush=True)
        with ThreadPoolExecutor(max_workers=SEED_WORKERS) as pool:
            futures = {pool.submit(_resolve_boxd, uri): uri for uri in sorted(short)}
            for fut in as_completed(futures):
                uri = futures[fut]
                try:
                    slug = fut.result()
                except Exception:
                    slug = None
                if slug:
                    resolved.add(slug)
                else:
                    failed.append(uri)

    slugs = direct | resolved
    db = init_db()
    new = 0
    if slugs:
        cur = db.executemany("INSERT OR IGNORE INTO movie(slug) VALUES (?)", [(s,) for s in sorted(slugs)])
        db.commit()
        new = cur.rowcount
    db.close()
    print(f"seeded {new} new ({len(slugs) - new} already present, {len(failed)} unresolvable)")
    for uri in failed[:10]:
        print(f"  unresolved: {uri}")


# Starter films spanning eras and industries, used to bootstrap the crawl
# when the DB is completely empty. Well-connected pages (prolific casts and
# crews) fan out into the wider graph within a few hops.
BOOTSTRAP_SLUGS = [
    "the-godfather",
    "come-and-see",
    "v-for-vendetta",
    "imagine-me-you",
    "planet-earth-2006",
    "parasite-2019",
    "sinners-2025",
    "hamnet",
]

_FILM_HREF_RE = re.compile(r"/film/([^/?#\s'\"]+)/?")


def bootstrap_seeds(db: sqlite3.Connection) -> int:
    """Insert starter slugs when the DB holds neither films nor people."""
    films = int(db.execute("SELECT COUNT(*) FROM movie").fetchone()[0])
    people = int(db.execute("SELECT COUNT(*) FROM person").fetchone()[0])
    if films or people:
        return 0
    before = films
    db.executemany("INSERT OR IGNORE INTO movie(slug) VALUES (?)", [(s,) for s in BOOTSTRAP_SLUGS])
    db.commit()
    after = int(db.execute("SELECT COUNT(*) FROM movie").fetchone()[0])
    return after - before


def _now_ms() -> int:
    return int(time.time() * 1000)


_TELEMETRY_INTERVAL_S = 15.0
_TELEMETRY_METRICS = (
    "ProcessTime",
    "TaskDuration",
    "ScriptDuration",
    "LayoutDuration",
    "RecalcStyleDuration",
    "JSHeapUsedSize",
    "JSHeapTotalSize",
    "Documents",
    "Nodes",
    "Frames",
    "JSEventListeners",
    "LayoutCount",
    "RecalcStyleCount",
)
_TELEMETRY_JS = r"""
(() => {
    const state = window.__grabReadinessSnapshot ? window.__grabReadinessSnapshot() : {};
    const readiness = {};
    for (const key of [
        "pendingTimers", "pendingIntervals", "pendingAnimationFrames", "pendingNetwork",
        "pendingXhr", "pendingWebSockets", "pendingEventSources", "domMutations",
        "navigationCount", "targetCount", "workerTargetCount", "lastMutation",
        "termination", "terminationReason",
    ]) {
        if (Object.prototype.hasOwnProperty.call(state, key)) readiness[key] = state[key];
    }
    return {
        url: location.href,
        title: document.title,
        visibility: document.visibilityState,
        readyState: document.readyState,
        readiness: readiness,
    };
})()
"""


def _metric_map(result: Any) -> dict[str, float]:
    if not isinstance(result, dict):
        return {}
    metrics = result.get("metrics")
    if not isinstance(metrics, list):
        return {}
    return {
        row["name"]: float(row["value"])
        for row in metrics
        if isinstance(row, dict)
        and isinstance(row.get("name"), str)
        and isinstance(row.get("value"), (int, float))
    }


def _summarize_processes(
    processes: Any,
    previous_cpu: dict[str, float],
    interval_s: float,
) -> tuple[dict[str, Any], dict[str, float]]:
    """Group Chromium CPU counters by process type and calculate deltas."""
    current_cpu: dict[str, float] = {}
    summary: dict[str, Any] = {}
    for index, process in enumerate(processes if isinstance(processes, list) else []):
        if not isinstance(process, dict):
            continue
        process_type = str(process.get("type") or "unknown")
        cpu_time = process.get("cpuTime")
        if not isinstance(cpu_time, (int, float)):
            continue
        cpu_time = float(cpu_time)
        pid = str(process.get("id") or f"{process_type}:{index}")
        current_cpu[pid] = cpu_time
        item = summary.setdefault(
            process_type,
            {"count": 0, "cpu_s": 0.0, "cpu_delta_s": 0.0, "delta_processes": 0},
        )
        item["count"] += 1
        item["cpu_s"] += cpu_time
        if pid in previous_cpu:
            item["cpu_delta_s"] += max(0.0, cpu_time - previous_cpu[pid])
            item["delta_processes"] += 1

    for item in summary.values():
        item["cpu_s"] = round(item["cpu_s"], 3)
        item["cpu_delta_s"] = round(item["cpu_delta_s"], 3)
        item["cpu_pct"] = (
            round(item["cpu_delta_s"] / interval_s * 100.0, 1)
            if item["delta_processes"] and interval_s > 0
            else None
        )
        del item["delta_processes"]
    return summary, current_cpu


def _summarize_targets(result: Any) -> dict[str, Any]:
    infos = result.get("targetInfos") if isinstance(result, dict) else None
    by_type: dict[str, int] = {}
    if isinstance(infos, list):
        for info in infos:
            if isinstance(info, dict):
                target_type = str(info.get("type") or "unknown")
                by_type[target_type] = by_type.get(target_type, 0) + 1
    return {"total": sum(by_type.values()), "by_type": by_type}


async def _telemetry_tab(tab: Any, index: int) -> dict[str, Any]:
    try:
        activity, performance = await asyncio.gather(
            tab.evaluate(_TELEMETRY_JS),
            tab.command("Performance.getMetrics"),
        )
        metrics = _metric_map(performance)
        selected_metrics = {
            key: round(metrics[key], 6)
            for key in _TELEMETRY_METRICS
            if key in metrics
        }
        result: dict[str, Any] = {"tab": index, "metrics": selected_metrics}
        if isinstance(activity, dict):
            result.update(
                {
                    "url": activity.get("url"),
                    "visibility": activity.get("visibility"),
                    "ready_state": activity.get("readyState"),
                    "readiness": activity.get("readiness", {}),
                }
            )
        return result
    except Exception as error:
        return {"tab": index, "error": f"{type(error).__name__}: {error}"}


async def _telemetry_snapshot(
    browser: Any,
    tabs: list[Any],
    previous_cpu: dict[str, float],
    previous_at: float,
    started_at: float,
    progress: Any,
) -> tuple[dict[str, Any], dict[str, float], float]:
    process_result, target_result = await asyncio.gather(
        browser.command("SystemInfo.getProcessInfo"),
        browser.command("Target.getTargets"),
        return_exceptions=True,
    )
    now = time.monotonic()
    interval_s = max(now - previous_at, 0.001)
    processes = (
        process_result.get("processInfo", [])
        if isinstance(process_result, dict)
        else []
    )
    process_summary, current_cpu = _summarize_processes(processes, previous_cpu, interval_s)
    snapshot: dict[str, Any] = {
        "elapsed_s": round(now - started_at, 1),
        "interval_s": round(interval_s, 1),
        "progress": progress(),
        "processes": process_summary,
        "targets": _summarize_targets(target_result if not isinstance(target_result, BaseException) else {}),
        "tabs": await asyncio.gather(*(_telemetry_tab(tab, i) for i, tab in enumerate(tabs))),
    }
    errors = {}
    if isinstance(process_result, BaseException):
        errors["processes"] = f"{type(process_result).__name__}: {process_result}"
    if isinstance(target_result, BaseException):
        errors["targets"] = f"{type(target_result).__name__}: {target_result}"
    if errors:
        snapshot["errors"] = errors
    return snapshot, current_cpu, now


async def _telemetry_loop(browser: Any, tabs: list[Any], stop: asyncio.Event, progress: Any) -> None:
    """Print opt-in browser/page telemetry without changing scrape behavior."""
    await asyncio.gather(
        *(tab.command("Performance.enable") for tab in tabs),
        return_exceptions=True,
    )
    started_at = time.monotonic()
    previous_at = started_at
    previous_cpu: dict[str, float] = {}
    while not stop.is_set():
        snapshot, previous_cpu, previous_at = await _telemetry_snapshot(
            browser,
            tabs,
            previous_cpu,
            previous_at,
            started_at,
            progress,
        )
        print(f"[telemetry] {json.dumps(snapshot, sort_keys=True, separators=(',', ':'))}", flush=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=_TELEMETRY_INTERVAL_S)
        except TimeoutError:
            pass


async def _replace_tab(
    browser: Any,
    tabs: list[Any],
    index: int,
    old_tab: Any,
    *,
    enable_telemetry: bool,
) -> Any:
    """Replace one live tab without changing the number of scraper tabs."""
    new_tab = await browser.new_tab()
    tabs[index] = new_tab
    if enable_telemetry:
        with suppress(Exception):
            await new_tab.command("Performance.enable")
    with suppress(Exception):
        await old_tab.close()
    return new_tab


async def _evaluate_retry(tab, js: str, attempts: int = 3) -> Any:
    """Evaluate, retrying transient CDP races (execution context destroyed by
    late subframe navigations, etc.) before surfacing the failure."""
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return await tab.evaluate(js)
        except Exception as e:
            last = e
            await asyncio.sleep(2)
    assert last is not None
    raise last


_PERSON_HREFS_JS = (
    r"Array.from(document.querySelectorAll('a[href]'))"
    r".map(function(a){return a.getAttribute('href');}).join('\n')"
)


async def discover_people(
    tab,
    db: sqlite3.Connection,
    stop: asyncio.Event,
    discovery_done: asyncio.Event,
    claimed: set[str],
    rotate_tab: Callable[[Any, int], Awaitable[Any]] | None = None,
    rotate_every: int = 0,
) -> tuple[int, int]:
    """Expand unexpanded person pages into new film slugs.

    Runs alongside the enrichment workers: every scraped film records its
    people, and this loop turns them back into films. Exits once the graph
    is drained (no unexpanded people, no pending or in-flight films) or
    `stop` is set. Returns (people_expanded, new_films_found).
    """
    expanded = 0
    new_films = 0
    attempts = 0
    while not stop.is_set():
        row = db.execute("SELECT path FROM person WHERE expanded_at IS NULL ORDER BY path LIMIT 1").fetchone()
        if row is None:
            pending = db.execute("SELECT COUNT(*) FROM movie WHERE enriched_at IS NULL").fetchone()[0]
            if pending == 0 and not claimed:
                break
            await asyncio.sleep(5)
            continue
        path = row[0]
        try:
            await tab.activate()
            await tab.navigate(f"https://letterboxd.com/{path}/")
            try:
                await wait_for_selector(tab, 'a[href*="/film/"]', timeout=15)
            except TimeoutError:
                pass
            await asyncio.sleep(1)
            title = await _evaluate_retry(tab, "document.title") or ""
            if "Just a moment" in title:
                # Cloudflare challenge: leave unexpanded and back off so a
                # later pass (or run) retries it instead of poisoning the crawl.
                await asyncio.sleep(10)
                continue
            hrefs = await _evaluate_retry(tab, _PERSON_HREFS_JS)
            slugs = {m.group(1) for m in _FILM_HREF_RE.finditer(hrefs or "")}
            if slugs:
                before = int(db.execute("SELECT COUNT(*) FROM movie").fetchone()[0])
                db.executemany("INSERT OR IGNORE INTO movie(slug) VALUES (?)", [(s,) for s in sorted(slugs)])
                after = int(db.execute("SELECT COUNT(*) FROM movie").fetchone()[0])
                new_films += after - before
            db.execute("UPDATE person SET expanded_at = ? WHERE path = ?", (_now_ms(), path))
            db.commit()
            expanded += 1
            print(f"  [discover] {path}: {len(slugs)} films", flush=True)
        except Exception as e:
            # Transient CDP/navigation races land here; the person stays
            # unexpanded and is retried on a later pass.
            print(f"  [discover] {path}: transient {type(e).__name__}, retrying later", file=sys.stderr)
            await asyncio.sleep(10)
        finally:
            attempts += 1
            task = asyncio.current_task()
            if (
                rotate_tab is not None
                and rotate_every > 0
                and attempts % rotate_every == 0
                and not stop.is_set()
                and not (task is not None and task.cancelling())
            ):
                tab = await rotate_tab(tab, attempts)
    discovery_done.set()
    return expanded, new_films


async def scrape_from_args(args) -> None:
    single_slug = args.slug
    concurrency = max(1, args.concurrency)
    rotate_tabs_every = int(max(0, getattr(args, "rotate_tabs_every", 0)))
    db = init_db()

    if single_slug:
        # Targeted mode: just this film, no discovery. Upsert inserts the row
        # if needed, so any slug can be scraped on demand.
        db.execute("INSERT OR IGNORE INTO movie(slug) VALUES (?)", (single_slug,))
        db.commit()
        print(f"Enriching {single_slug}...")
    else:
        added = bootstrap_seeds(db)
        if added:
            print(f"Empty DB: seeded {added} starter films to bootstrap the crawl.", flush=True)
        pending = db.execute("SELECT COUNT(*) FROM movie WHERE enriched_at IS NULL").fetchone()[0]
        people = db.execute("SELECT COUNT(*) FROM person WHERE expanded_at IS NULL").fetchone()[0]
        if pending == 0 and people == 0:
            total = db.execute("SELECT COUNT(*) FROM movie").fetchone()[0]
            if total == 0:
                print("No films in the DB yet. Seed slugs first, e.g.:\n  uv run movies-cli seed diary.csv")
            else:
                print("All movies already enriched.")
            db.close()
            return
        print(f"Enriching {pending} movies... ({people} unexpanded people)", flush=True)

    upsert_sql = """
        INSERT INTO movie (
            slug, title, alt_title, year,
            directors, "cast", synopsis,
            rating, rating_count, rating_dist,
            fans, watched_by, liked_by,
            imdb_id, tmdb_id, studios, crew,
            backdrop_url, poster_url, enriched_at
        ) VALUES (
            ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?
        ) ON CONFLICT(slug) DO UPDATE SET
            title = excluded.title, alt_title = excluded.alt_title, year = excluded.year,
            directors = excluded.directors, "cast" = excluded."cast",
            synopsis = excluded.synopsis,
            rating = excluded.rating, rating_count = excluded.rating_count,
            rating_dist = excluded.rating_dist,
            fans = excluded.fans, watched_by = excluded.watched_by,
            liked_by = excluded.liked_by,
            imdb_id = excluded.imdb_id, tmdb_id = excluded.tmdb_id,
            studios = excluded.studios, crew = excluded.crew,
            backdrop_url = excluded.backdrop_url, poster_url = excluded.poster_url,
            enriched_at = excluded.enriched_at
    """

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    discovery_done = asyncio.Event()
    # SIGINT/SIGTERM drive the same graceful path: workers finish the current
    # film, tabs close, the browser stops, and the DB — the resume pointer —
    # is committed before exit.
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):
            pass

    headless = not getattr(args, "live", False)
    browser, tab0 = await shadowdriver.ensure_browser(
        headless=headless,
        persistent=True,
        stealth=True,
    )
    tabs = [tab0] + [await browser.new_tab() for _ in range(concurrency - 1)]
    enable_telemetry = bool(getattr(args, "telemetry", False))

    async def rotate_live_tab(index: int, old_tab: Any, label: str) -> Any:
        try:
            new_tab = await _replace_tab(
                browser,
                tabs,
                index,
                old_tab,
                enable_telemetry=enable_telemetry,
            )
        except Exception as error:
            print(
                f"  [rotate] {label}: failed ({type(error).__name__}), keeping current tab",
                file=sys.stderr,
            )
            return old_tab
        print(f"  [rotate] {label}", flush=True)
        return new_tab

    done = 0
    errors = 0
    lock = asyncio.Lock()
    # Slugs claimed this run. Never released: a failed film stays
    # enriched_at IS NULL in the DB (retried next run) but won't hot-loop
    # within this one.
    claimed: set[str] = set()
    telemetry_stop = asyncio.Event()
    telemetry_task = None

    async def claim() -> str | None:
        async with lock:
            if single_slug:
                assert isinstance(single_slug, str)
                if single_slug in claimed:
                    return None
                claimed.add(single_slug)
                return single_slug
            rows = db.execute("SELECT slug FROM movie WHERE enriched_at IS NULL ORDER BY slug LIMIT 100").fetchall()
            for row in rows:
                slug = row[0]
                assert isinstance(slug, str)
                if slug not in claimed:
                    claimed.add(slug)
                    return slug
            return None

    async def worker(tab, tab_index: int) -> None:
        nonlocal done, errors
        attempts = 0
        while not stop.is_set():
            slug = await claim()
            if slug is None:
                if single_slug or discovery_done.is_set():
                    break
                await asyncio.sleep(5)
                continue
            print(f"[enriched={done} failed={errors}] {slug}", flush=True)
            try:
                info = await scrape_movie(tab, slug)
                for w in validate(info):
                    print(f"  [{slug}] {w}", file=sys.stderr)
                # Record people even when the film itself is skipped so the
                # discovery crawl keeps growing through poster-less films.
                people = [(p,) for p in info.get("people") or []]
                if people:
                    db.executemany("INSERT OR IGNORE INTO person(path) VALUES (?)", people)
                if not info.get("title"):
                    # A title-less row is useless for search/rename and would
                    # be marked enriched. Skip it for a later retry instead.
                    print(f"  [{slug}] skipping upsert: no title (leaving unenriched for a later retry)", file=sys.stderr)
                    db.commit()
                    errors += 1
                    continue
                if not info.get("posterUrl"):
                    # Not fatal: the film may genuinely have no poster, or
                    # hydration flaked. Leave it unenriched so a later run
                    # retries it instead of aborting the whole rescrape.
                    print(f"  [{slug}] skipping upsert: no posterUrl (leaving unenriched for a later retry)", file=sys.stderr)
                    db.commit()
                    errors += 1
                    continue
                synopsis = info.get("synopsis")
                # Coerce year to int defensively — the legacy Waterfox BiDi
                # client sent integer-valued numbers as floats (e.g. 2025.0),
                # which SQLite stored as the text '2025.0' in a TEXT column.
                year = int(info["year"]) if info.get("year") is not None else None
                db.execute(
                    upsert_sql,
                    (
                        slug,
                        info.get("title"),
                        info.get("altTitle"),
                        year,
                        compress(json.dumps(info.get("directors") or [])),
                        compress(json.dumps(info.get("cast") or [])),
                        compress(synopsis) if synopsis else None,
                        info.get("rating"),
                        info.get("ratingCount"),
                        compress(json.dumps(info.get("ratingDist") or [])),
                        info.get("fans"),
                        info.get("watchedBy"),
                        info.get("likedBy"),
                        info.get("imdbId"),
                        info.get("tmdbId"),
                        compress(json.dumps(info.get("studios") or [])),
                        compress(json.dumps(info.get("crew") or [])),
                        info.get("backdropUrl"),
                        info.get("posterUrl"),
                        _now_ms(),
                    ),
                )
                db.commit()

                done += 1
            except Exception:
                traceback.print_exc()
                errors += 1
            finally:
                attempts += 1
                task = asyncio.current_task()
                if (
                    rotate_tabs_every > 0
                    and attempts % rotate_tabs_every == 0
                    and not stop.is_set()
                    and not (task is not None and task.cancelling())
                ):
                    tab = await rotate_live_tab(
                        tab_index,
                        tab,
                        f"worker {tab_index} after {attempts} attempts",
                    )

    disc_stats = (0, 0)
    # Create every tab before any navigation starts so CDP target
    # handshakes never interleave with page traffic.
    enrich_tabs = list(tabs)
    dtab = None
    if not single_slug:
        dtab = await browser.new_tab()
        tabs.append(dtab)
    worker_tasks = [asyncio.create_task(worker(t, i)) for i, t in enumerate(enrich_tabs)]
    if enable_telemetry:
        telemetry_task = asyncio.create_task(
            _telemetry_loop(
                browser,
                tabs,
                telemetry_stop,
                lambda: {"enriched": done, "failed": errors, "claimed": len(claimed)},
            )
        )
    try:
        if single_slug:
            await asyncio.gather(*worker_tasks)
        else:
            assert dtab is not None
            discovery_index = len(tabs) - 1

            async def rotate_discovery_tab(current_tab: Any, attempts: int) -> Any:
                return await rotate_live_tab(
                    discovery_index,
                    current_tab,
                    f"discovery after {attempts} attempts",
                )

            disc_task = asyncio.create_task(
                discover_people(
                    dtab,
                    db,
                    stop,
                    discovery_done,
                    claimed,
                    rotate_discovery_tab if rotate_tabs_every > 0 else None,
                    rotate_tabs_every,
                )
            )
            await asyncio.gather(*worker_tasks)
            disc_stats = await disc_task
    except asyncio.CancelledError:
        pass
    finally:
        if telemetry_task is not None:
            telemetry_stop.set()
            telemetry_task.cancel()
            with suppress(asyncio.CancelledError):
                await telemetry_task
        for tab in tabs:
            with suppress(Exception):
                await tab.close()
        try:
            await browser.close(terminate=True)
        except Exception:
            pass
    db.close()
    summary = f"enriched {done}, {errors} failed" if errors else f"enriched {done}"
    if not single_slug:
        expanded, new_films = disc_stats
        summary += f", discovered {new_films} films via {expanded} people"
    print(f"\nDone: {summary}.")


async def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("slug", nargs="?")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--live", action="store_true", help="run headed Chromium for debugging")
    parser.add_argument("--telemetry", action="store_true", help="print periodic Chromium/page telemetry")
    parser.add_argument(
        "--rotate-tabs-every",
        type=int,
        default=0,
        help="replace each scraper tab after this many attempts (0 disables rotation)",
    )
    await scrape_from_args(parser.parse_args(argv))


def run(argv: list[str] | None = None) -> None:
    try:
        uvloop.run(main(argv))
    except KeyboardInterrupt:
        pass
