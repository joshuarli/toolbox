import html as h
import json
import mimetypes
import re
import sqlite3
import zlib
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, override
from urllib.parse import parse_qs, quote, urlparse

from tool.movies.paths import data_dir, db_path

PAGE = 80
FILTER = "enriched_at IS NOT NULL AND title IS NOT NULL AND liked_by >= 100"
DIST_LABELS = ["½", "1", "1½", "2", "2½", "3", "3½", "4", "4½", "5"]

_DB_PATH = db_path()
_IMG_DIR = data_dir() / "static" / "img"
_person_index: dict[str, list[dict[str, Any]]] | None = None
_fts_ready = False


def configure(db: Path, img_dir: Path) -> None:
    global _DB_PATH, _IMG_DIR, _person_index, _fts_ready
    _DB_PATH = db
    _IMG_DIR = img_dir
    _person_index = None
    _fts_ready = False


def _dc(b: bytes) -> str:
    return zlib.decompress(b, wbits=-15).decode()


def _blob(val: bytes | None) -> list[Any]:
    return json.loads(_dc(val)) if val is not None else []


def _text(val: bytes | None) -> str | None:
    return _dc(val) if val is not None else None


def _name(slug: str) -> str:
    return " ".join(w.capitalize() for w in re.sub(r"-\d+$", "", slug).split("-"))


def _fmt(n: int | None) -> str:
    return "—" if n is None else f"{n:,}"


def _stars(r: float | None) -> str:
    if r is None:
        return ""
    full = int(r)
    frac = r - full
    half = 0.25 <= frac < 0.75
    return "★" * full + ("½" if half else "") + "☆" * (5 - full - (1 if half else 0))


def _plink(slug: str) -> str:
    return f'<a href="/person/?q={quote(slug)}" class="person-link">{h.escape(_name(slug))}</a>'


def _histogram(dist: list[Any]) -> str:
    if not dist:
        return ""
    mx = max(max(dist), 1)
    bars = "".join(
        f'<div class="bar-wrap" title="{DIST_LABELS[i]}★: {c:,}"><div class="bar" style="height:{round(c / mx * 100)}%"></div></div>'
        for i, c in enumerate(dist)
    )
    return f'<div class="histogram">{bars}</div>'


def _card_data(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["slug"],
        "title": row["title"],
        "year": row["year"] or "",
        "rating": row["rating"],
        "posterUrl": f"/static/img/{row['poster_local']}" if row["poster_local"] else "",
        "backdropUrl": f"/static/img/{row['backdrop_local']}" if row["backdrop_local"] else "",
    }


def movie_html(m: dict[str, Any]) -> str:
    directors = _blob(m["directors"])
    cast = _blob(m["cast"])
    crew = _blob(m["crew"])
    studios = _blob(m["studios"])
    dist = _blob(m["rating_dist"])
    synopsis = _text(m["synopsis"])

    crew_by_role: dict[str, list[str]] = {}
    for e in crew:
        crew_by_role.setdefault(e["role"], []).append(e["slug"])

    crew_rows = "".join(
        f"<tr><th>{h.escape(role)}</th><td>{''.join(_plink(s) for s in slugs)}</td></tr>"
        for role, slugs in crew_by_role.items()
    )
    cast_li = "".join(f"<li>{_plink(s)}</li>" for s in cast[:18])
    if len(cast) > 18:
        cast_li += f"<li class='more'>+{len(cast) - 18} more</li>"

    rating = m["rating"]
    rating_block = (
        f'<div class="rating-avg">{rating:.2f}</div>'
        f'<div class="rating-stars">{_stars(rating)}</div>'
        f'<div class="rating-count">{_fmt(m["rating_count"])} ratings</div>'
        if rating is not None
        else '<div class="rating-none">No ratings yet</div>'
    )

    ext = []
    if m["imdb_id"]:
        ext.append(
            f'<a href="https://www.imdb.com/title/{h.escape(m["imdb_id"])}/" class="micro-btn" target="_blank" rel="noopener">IMDb</a>'
        )
    if m["tmdb_id"]:
        ext.append(
            f'<a href="https://www.themoviedb.org/movie/{h.escape(str(m["tmdb_id"]))}" class="micro-btn" target="_blank" rel="noopener">TMDb</a>'
        )
    ext.append(
        f'<a href="https://letterboxd.com/film/{h.escape(m["slug"])}/" class="micro-btn micro-btn-lbxd" target="_blank" rel="noopener">Letterboxd</a>'
    )

    bl = m["backdrop_local"]
    bg = f"style=\"background-image:url('/static/img/{h.escape(bl)}')\"" if bl else ""
    pl = m["poster_local"]
    poster = (
        f'<img class="poster-img" src="/static/img/{h.escape(pl)}" alt="{h.escape(m["title"])} poster" loading="lazy">'
        if pl
        else '<div class="poster-img poster-empty"></div>'
    )

    title = m["title"]
    year = m["year"]
    alt = m["alt_title"]
    meta_parts = []
    if year:
        meta_parts.append(f'<span class="film-year">{h.escape(str(year))}</span>')
    if directors:
        meta_parts.append(f'<span class="film-directed">Directed by {", ".join(_plink(d) for d in directors)}</span>')
    meta = f"<p class='film-meta'>{' '.join(meta_parts)}</p>" if meta_parts else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{h.escape(title + (f" ({year})" if year else ""))}</title>
  <link rel="stylesheet" href="/style.css">
</head>
<body class="film-page">
  <div class="backdrop" {bg}></div>
  <div class="film-wrap">
    <div class="film-sidebar">
      <div class="poster-frame">{poster}</div>
      <div class="film-stats">
        <div class="stat-item"><span class="stat-num">{_fmt(m["watched_by"])}</span><span class="stat-lbl">Watches</span></div>
        <div class="stat-item"><span class="stat-num">{_fmt(m["liked_by"])}</span><span class="stat-lbl">Likes</span></div>
        <div class="stat-item"><span class="stat-num">{_fmt(m["fans"])}</span><span class="stat-lbl">Fans</span></div>
      </div>
      <div class="micro-btns">{"".join(ext)}</div>
    </div>
    <div class="film-main">
      <div class="film-masthead">
        <h1 class="film-title">{h.escape(title)}</h1>
        {meta}
        {"<p class='film-original-title'>" + h.escape(alt) + "</p>" if alt else ""}
      </div>
      {"<div class='film-synopsis'><p>" + h.escape(synopsis) + "</p></div>" if synopsis is not None else ""}
      {"<div class='film-section'><h2 class='section-title'>Cast</h2><ul class='cast-list'>" + cast_li + "</ul></div>" if cast else ""}
      {"<div class='film-section'><h2 class='section-title'>Studios</h2><p class='studios-text'>" + h.escape(", ".join(_name(s) for s in studios)) + "</p>" if studios else ""}
      {"<div class='film-section'><h2 class='section-title'>Crew</h2><table class='crew-table'>" + crew_rows + "</table></div>" if crew_rows else ""}
    </div>
    <div class="film-ratings-col">
      <div class="sidebar-section">
        <h3 class="sidebar-heading">Ratings</h3>
        <div class="rating-block">{rating_block}</div>
        {_histogram(dist)}
      </div>
    </div>
  </div>
</body>
</html>"""


def index_html(count: int) -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Films</title>
  <link rel="stylesheet" href="/style.css">
</head>
<body>
  <header class="index-header">
    <h1>Films</h1>
    <p class="subtitle">""" + str(count) + """ films</p>
    <input id="search" type="search" placeholder="Search by title, year…" autocomplete="off" spellcheck="false">
  </header>
  <div id="grid" class="grid"></div>
  <div id="sentinel"></div>
  <script>
const grid = document.getElementById('grid');
const sentinel = document.getElementById('sentinel');
const input = document.getElementById('search');
let offset = 0, loading = false, searchActive = false;

const imgObs = new IntersectionObserver(entries => {
  for (const e of entries) if (e.isIntersecting) { e.target.src = e.target.dataset.src; imgObs.unobserve(e.target); }
}, { rootMargin: '200px' });

const pageObs = new IntersectionObserver(entries => {
  if (entries[0].isIntersecting && !searchActive && !loading) loadMore();
}, { rootMargin: '400px' });

function starsHtml(r) {
  if (r == null) return '';
  const full = Math.floor(r), half = (r-full) >= 0.25 && (r-full) < 0.75, empty = 5-full-(half?1:0);
  return '★'.repeat(full) + (half?'½':'') + '☆'.repeat(empty);
}

function cardHtml(m) {
  const img = m.posterUrl || m.backdropUrl;
  const thumb = img ? `<img class="card-img" data-src="${img}" alt="">` : `<div class="card-img card-img-empty"></div>`;
  const rating = m.rating != null ? `<span class="card-rating" title="${m.rating.toFixed(2)}">${starsHtml(m.rating)}</span>` : '';
  return `<a class="card" href="/${m.id}/">${thumb}<div class="card-body"><div class="card-title">${m.title}</div><div class="card-meta">${m.year ? m.year+' · ':'' }${m.directors||''}</div>${rating}</div></a>`;
}

function appendCards(items, clear) {
  if (clear) grid.innerHTML = '';
  const t = document.createElement('template');
  t.innerHTML = items.map(cardHtml).join('');
  for (const img of t.content.querySelectorAll('img[data-src]')) imgObs.observe(img);
  grid.appendChild(t.content);
}

function loadMore() {
  loading = true;
  fetch(`/api/movies?offset=${offset}`)
    .then(r => r.json())
    .then(data => { appendCards(data, false); offset += data.length; loading = false; });
}

pageObs.observe(sentinel);
loadMore();

let debounce;
input.addEventListener('input', () => {
  clearTimeout(debounce);
  debounce = setTimeout(() => {
    const q = input.value.trim();
    if (!q) {
      searchActive = false; offset = 0; grid.innerHTML = ''; loadMore(); pageObs.observe(sentinel);
    } else {
      searchActive = true; pageObs.unobserve(sentinel);
      fetch(`/api/search?q=${encodeURIComponent(q)}`).then(r => r.json()).then(data => appendCards(data, true));
    }
  }, 150);
});
  </script>
</body>
</html>"""


def person_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Person</title>
  <link rel="stylesheet" href="/style.css">
</head>
<body>
  <div id="person-header" class="index-header" style="display:none">
    <h1 id="person-name"></h1>
    <p id="person-roles" class="subtitle"></p>
  </div>
  <div id="not-found" class="index-header" style="display:none"><h1>Person not found</h1></div>
  <div id="grid" class="grid"></div>
  <script>
const q = new URLSearchParams(location.search).get('q') || '';
const grid = document.getElementById('grid');

const imgObs = new IntersectionObserver(entries => {
  for (const e of entries) if (e.isIntersecting) { e.target.src = e.target.dataset.src; imgObs.unobserve(e.target); }
}, { rootMargin: '200px' });

function slugToName(s) { return s.replace(/-\\d+$/, '').split('-').map(w => w[0].toUpperCase()+w.slice(1)).join(' '); }

function starsHtml(r) {
  if (r == null) return '';
  const full = Math.floor(r), half = (r-full) >= 0.25 && (r-full) < 0.75, empty = 5-full-(half?1:0);
  return '★'.repeat(full) + (half?'½':'') + '☆'.repeat(empty);
}

function cardHtml(m) {
  const img = m.posterUrl || m.backdropUrl;
  const thumb = img ? `<img class="card-img" data-src="${img}" alt="">` : `<div class="card-img card-img-empty"></div>`;
  const rating = m.rating != null ? `<span class="card-rating">${starsHtml(m.rating)}</span>` : '';
  return `<a class="card" href="/${m.id}/">${thumb}<div class="card-body"><div class="card-title">${m.title}</div><div class="card-meta">${m.year||''}</div>${rating}</div></a>`;
}

if (!q) {
  document.getElementById('not-found').style.display = '';
} else {
  fetch(`/api/person?q=${encodeURIComponent(q)}`).then(r => r.json()).then(data => {
    if (!data.length) { document.getElementById('not-found').style.display = ''; return; }
    const hdr = document.getElementById('person-header');
    hdr.style.display = '';
    document.getElementById('person-name').textContent = slugToName(q);
    const roles = [...new Set(data.flatMap(m => m.roles))];
    document.getElementById('person-roles').textContent = roles.join(' · ');
    const t = document.createElement('template');
    t.innerHTML = data.map(cardHtml).join('');
    for (const img of t.content.querySelectorAll('img[data-src]')) imgObs.observe(img);
    grid.appendChild(t.content);
  });
}
  </script>
</body>
</html>"""


CSS = """*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg: #14151a; --surface: #1c1c24; --surface2: #252730; --border: #303442;
  --text: #9ab; --text-hi: #e0e8f0; --muted: #678; --green: #00c030; --green-dim: #009928; --orange: #f0c040;
}

html { font-size: 16px; }
body { background: var(--bg); color: var(--text); font-family: "Graphik", system-ui, -apple-system, sans-serif; line-height: 1.5; }
a { color: inherit; text-decoration: none; }

.backdrop { width: 100%; height: 300px; background-size: cover; background-position: center 20%; background-color: var(--surface); position: relative; }
.backdrop::after { content: ''; position: absolute; inset: 0; background: linear-gradient(to bottom, transparent 40%, var(--bg) 100%); }

.film-wrap { max-width: 1000px; margin: -80px auto 0; padding: 0 1.5rem 4rem; display: flex; gap: 2.5rem; align-items: flex-start; position: relative; z-index: 1; }

.film-sidebar { flex: 0 0 230px; display: flex; flex-direction: column; gap: 1.5rem; }
.poster-frame { border-radius: 6px; overflow: hidden; box-shadow: 0 6px 30px rgba(0,0,0,0.7); }
.poster-img { display: block; width: 100%; aspect-ratio: 2/3; object-fit: cover; }
.poster-empty { width: 100%; aspect-ratio: 2/3; background: var(--surface2); }

.film-stats { display: flex; justify-content: space-between; border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); padding: 0.75rem 0; }
.stat-item { text-align: center; flex: 1; }
.stat-num { display: block; font-size: 1.1rem; font-weight: 700; color: var(--text-hi); line-height: 1.2; }
.stat-lbl { font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); }

.sidebar-heading { font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.12em; color: var(--muted); margin-bottom: 0.6rem; border-bottom: 1px solid var(--border); padding-bottom: 0.35rem; }
.rating-block { display: flex; flex-direction: column; gap: 0.25rem; margin-bottom: 0.75rem; }
.rating-avg { font-size: 2rem; font-weight: 700; color: var(--text-hi); line-height: 1; }
.rating-stars { font-size: 0.95rem; color: var(--orange); letter-spacing: 0.04em; }
.rating-count { font-size: 0.75rem; color: var(--muted); }
.rating-none { font-size: 0.82rem; color: var(--muted); }

.histogram { display: flex; align-items: flex-end; gap: 3px; height: 64px; padding-bottom: 18px; }
.bar-wrap { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; height: 100%; }
.bar { width: 100%; background: var(--green-dim); border-radius: 2px 2px 0 0; min-height: 2px; transition: background 0.12s; }
.bar-wrap:hover .bar { background: var(--green); }

.micro-btns { display: flex; flex-wrap: wrap; gap: 0.35rem; }
.micro-btn { font-size: 0.7rem; font-weight: 600; line-height: 1; padding: 0.3rem 0.6rem; border-radius: 3px; background: var(--surface2); color: var(--text); border: 1px solid var(--border); transition: border-color 0.12s, color 0.12s; }
.micro-btn:hover { color: var(--text-hi); border-color: #5a6070; }
.micro-btn-lbxd { color: var(--green); border-color: var(--green-dim); }
.micro-btn-lbxd:hover { border-color: var(--green); }

.film-ratings-col { flex: 0 0 180px; padding-top: 4.5rem; display: flex; flex-direction: column; gap: 1.5rem; }
.film-main { flex: 1; min-width: 0; padding-top: 4.5rem; }
.film-masthead { margin-bottom: 1.5rem; }
.film-title { font-size: 1.9rem; font-weight: 700; color: var(--text-hi); line-height: 1.15; margin-bottom: 0.4rem; }
.film-meta { font-size: 0.85rem; color: var(--muted); margin-bottom: 0.3rem; }
.film-year { font-weight: 600; color: var(--text); }
.person-link { color: var(--text); }
.person-link:hover { color: var(--text-hi); text-decoration: underline; }
.film-original-title { font-size: 0.85rem; color: var(--muted); font-style: italic; }
.film-synopsis { font-size: 0.9rem; line-height: 1.7; color: var(--text); max-width: 600px; margin-bottom: 2rem; }
.studios-text { font-size: 0.82rem; line-height: 1.5; }
.film-section { margin-bottom: 2.25rem; }
.section-title { font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.12em; color: var(--muted); border-bottom: 1px solid var(--border); padding-bottom: 0.35rem; margin-bottom: 0.85rem; }
.cast-list { list-style: none; display: flex; flex-wrap: wrap; gap: 0.4rem; }
.cast-list li { font-size: 0.8rem; background: var(--surface2); border: 1px solid var(--border); border-radius: 3px; padding: 0.15rem 0.5rem; }
.cast-list li.more { color: var(--muted); background: none; border-color: transparent; }
.crew-table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
.crew-table th { text-align: left; width: 150px; vertical-align: top; padding: 0.3rem 1rem 0.3rem 0; color: var(--muted); font-weight: 400; }
.crew-table td { padding: 0.3rem 0; border-bottom: 1px solid var(--border); }

.index-header { text-align: center; padding: 3.5rem 1rem 2rem; }
.index-header h1 { font-size: 2.2rem; font-weight: 700; color: var(--text-hi); margin-bottom: 0.3rem; }
.subtitle { color: var(--muted); font-size: 0.9rem; margin-bottom: 1.75rem; }
#search { display: block; width: min(500px, 90vw); margin: 0 auto; background: var(--surface); border: 1px solid var(--border); border-radius: 6px; color: var(--text-hi); font-size: 0.95rem; padding: 0.65rem 1rem; outline: none; transition: border-color 0.15s; }
#search:focus { border-color: var(--green-dim); }
#search::placeholder { color: var(--muted); }

.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(155px, 1fr)); gap: 1.1rem; padding: 1.5rem 2rem 4rem; max-width: 1200px; margin: 0 auto; }
.card { display: flex; flex-direction: column; background: var(--surface); border: 1px solid var(--border); border-radius: 6px; overflow: hidden; transition: transform 0.14s, border-color 0.14s; }
.card:hover { transform: translateY(-3px); border-color: #4a5060; }
.card-img { display: block; width: 100%; aspect-ratio: 2/3; object-fit: cover; }
.card-img-empty { width: 100%; aspect-ratio: 2/3; background: var(--surface2); }
.card-body { padding: 0.55rem 0.65rem 0.7rem; flex: 1; display: flex; flex-direction: column; gap: 0.15rem; }
.card-title { font-size: 0.82rem; font-weight: 600; color: var(--text-hi); line-height: 1.3; }
.card-meta { font-size: 0.7rem; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.card-rating { font-size: 0.7rem; color: var(--orange); margin-top: auto; padding-top: 0.2rem; }

@media (max-width: 780px) {
  .film-wrap { flex-wrap: wrap; gap: 1.5rem; margin-top: -40px; padding: 0 1rem 3rem; }
  .film-sidebar { flex: 0 0 120px; }
  .film-main { flex: 1 1 0; min-width: 200px; padding-top: 0; }
  .film-ratings-col { flex: 0 0 100%; padding-top: 0; order: 3; }
  .film-title { font-size: 1.4rem; }
  .grid { grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); padding: 1rem; }
}
"""


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(_DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def ensure_fts() -> None:
    global _fts_ready
    if _fts_ready:
        return
    db = get_db()
    db.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS movie_fts USING fts5(
            slug UNINDEXED, title, alt_title, directors_text,
            tokenize='trigram'
        )
    """)
    movie_count = db.execute(f"SELECT COUNT(*) FROM movie WHERE {FILTER}").fetchone()[0]
    fts_count = db.execute("SELECT COUNT(*) FROM movie_fts").fetchone()[0]
    if fts_count != movie_count:
        print(f"Building FTS index ({movie_count} movies)...", flush=True)
        db.execute("DELETE FROM movie_fts")
        rows = db.execute(f"SELECT slug, title, alt_title, directors FROM movie WHERE {FILTER}").fetchall()
        db.executemany(
            "INSERT INTO movie_fts(slug, title, alt_title, directors_text) VALUES (?,?,?,?)",
            [
                (r["slug"], r["title"] or "", r["alt_title"] or "", " ".join(_name(s) for s in _blob(r["directors"])))
                for r in rows
            ],
        )
        db.commit()
        print("FTS index ready.", flush=True)
    db.close()
    _fts_ready = True


def _fts_query(q: str) -> str | None:
    words = [w for w in q.split() if w]
    return " ".join(f'"{w.replace(chr(34), "")}"' for w in words) if words else None


def build_person_index() -> None:
    global _person_index
    _person_index = defaultdict(list)
    db = get_db()
    rows = db.execute(
        f'SELECT slug, title, year, rating, poster_local, backdrop_local, directors, "cast", crew FROM movie WHERE {FILTER}'
    ).fetchall()
    db.close()
    for row in rows:
        card = _card_data(row)
        dirs = _blob(row["directors"])
        cast = _blob(row["cast"])
        crew = _blob(row["crew"])
        persons: dict[str, list[str]] = {}
        for s in dirs:
            persons.setdefault(s, []).append("Director")
        for s in cast:
            persons.setdefault(s, []).append("Cast")
        for e in crew:
            persons.setdefault(e["slug"], []).append(e["role"])
        for slug, roles in persons.items():
            _person_index[slug].append({**card, "roles": roles})
    print(f"Person index built: {len(_person_index)} people")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/":
            db = get_db()
            count = db.execute(f"SELECT COUNT(*) FROM movie WHERE {FILTER}").fetchone()[0]
            db.close()
            self._html(index_html(count))
        elif path == "/style.css":
            self._send(CSS.encode(), "text/css")
        elif path.startswith("/static/"):
            self._static(path)
        elif path == "/api/movies":
            offset = int(qs.get("offset", ["0"])[0])
            db = get_db()
            rows = db.execute(
                f"SELECT slug, title, year, rating, poster_local, backdrop_local FROM movie WHERE {FILTER} ORDER BY title COLLATE NOCASE LIMIT {PAGE} OFFSET ?",
                (offset,),
            ).fetchall()
            db.close()
            self._json([_card_data(r) for r in rows])
        elif path == "/api/search":
            q = qs.get("q", [""])[0].strip()
            if not q:
                self._json([])
                return
            ensure_fts()
            fq = _fts_query(q)
            if fq is None:
                self._json([])
                return
            db = get_db()
            try:
                slugs = [
                    r[0]
                    for r in db.execute(
                        "SELECT slug FROM movie_fts WHERE movie_fts MATCH ? ORDER BY rank LIMIT 200",
                        (fq,),
                    ).fetchall()
                ]
            except sqlite3.OperationalError:
                slugs = []
            if slugs:
                ph = ",".join("?" * len(slugs))
                rows = db.execute(
                    f"SELECT slug, title, year, rating, poster_local, backdrop_local FROM movie WHERE slug IN ({ph})",
                    slugs,
                ).fetchall()
                by_slug = {r["slug"]: r for r in rows}
                results = [_card_data(by_slug[s]) for s in slugs if s in by_slug]
            else:
                results = []
            db.close()
            self._json(results)
        elif path == "/person/" or path == "/person":
            self._html(person_html())
        elif path == "/api/person":
            q = qs.get("q", [""])[0].strip()
            if not q:
                self._json([])
                return
            if _person_index is None:
                build_person_index()
            assert _person_index is not None
            self._json(_person_index.get(q, []))
        elif path.endswith("/") and len(path) > 1:
            slug = path.strip("/")
            db = get_db()
            row = db.execute("SELECT * FROM movie WHERE slug = ?", (slug,)).fetchone()
            db.close()
            if row is None:
                self.send_error(404)
                return
            self._html(movie_html(dict(row)))
        else:
            self.send_error(404)

    def _html(self, body: str) -> None:
        self._send(body.encode(), "text/html; charset=utf-8")

    def _json(self, obj: object) -> None:
        self._send(json.dumps(obj).encode(), "application/json")

    def _send(self, data: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _static(self, path: str) -> None:
        fpath = _IMG_DIR / Path(path).name
        if not fpath.exists():
            self.send_error(404)
            return
        data = fpath.read_bytes()
        mime, _ = mimetypes.guess_type(str(fpath))
        self._send(data, mime or "application/octet-stream")

    @override
    def log_message(self, format: str, *args: Any) -> None:
        pass


def run(host: str = "", port: int = 8000) -> None:
    configure(db_path(), data_dir() / "static" / "img")
    ensure_fts()
    print(f"http://localhost:{port}")
    HTTPServer((host, port), Handler).serve_forever()
