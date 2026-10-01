# `tool movies` — Letterboxd Tooling

## Runtime Data

Runtime Letterboxd data defaults to `var/letterboxd`, overrideable with `MOVIES_DATA_DIR`.

| Runtime path | Purpose |
|---|---|
| `var/letterboxd/letterboxd.db` | SQLite movie database, ignored by git |

`movies.scraper` drives Chromium via the reusable `shadowdriver` Python extension: `ensure_browser()` on the managed profile, one tab per worker, `tab.navigate()` + `tab.evaluate()` for the DOM scrape, and `tab.get_cookies()` for authenticated fetches. Install browser support with `uv sync --extra browser`; ordinary `uv sync` leaves it out. No Selenium, Playwright, or WebDriver server is involved. Scraping works logged-out — everything `search`/`rename` needs (titles, year, directors, cast, studios, crew, poster/backdrop URLs) is public. Only rating statistics (average, distribution, watches, likes) appear to need login; `uv run tool movies login` stores a session in shadowdriver's managed Chromium profile for those.

## Commands

```bash
uv run tool movies login
uv run tool movies search "disorder huang weikai"
uv run tool movies rename /path/to/movies        # dry run by default
uv run tool movies rename file.mkv --apply       # actually rename
uv run tool movies scrape --concurrency 2
uv run tool movies scrape --telemetry --concurrency 1
uv run tool movies scrape --telemetry --rotate-tabs-every 20 --concurrency 1
uv run tool movies seed diary.csv
```

### Scrape telemetry

Pass `--telemetry` to print a JSON snapshot every 15 seconds. It reports
Chromium CPU-time deltas grouped by process type, target counts, per-tab
`Performance.getMetrics()` counters, and the readiness state installed by
`shadowdriver` (pending intervals, animation frames, network work, and DOM
mutations). A sustained renderer `cpu_pct` increase with growing
`ScriptDuration` or `LayoutDuration` points at page work; growing pending
activity or target counts points at cleanup/navigation leakage; high browser
CPU with flat page metrics points at browser/CDP overhead.

`--rotate-tabs-every N` is an opt-in diagnostic mitigation: each enrichment
tab and the discovery tab is replaced after N attempts, including failed
attempts. The default `0` reuses tabs for the whole crawl. If CPU or renderer
memory drops with rotation while the workload and concurrency stay fixed, tab
reuse/page-state accumulation is implicated; if it does not, rotation is not
the primary fix.

## Scrape crawl, resume, and shutdown

Bare `uv run tool movies scrape` runs the whole crawl unattended:

1. **Bootstrap** — an empty DB gets a few well-connected starter films (`BOOTSTRAP_SLUGS`); `seed` CSV rows and `scrape <slug>` also feed the frontier.
2. **Enrich** — `--concurrency N` worker tabs scrape unenriched films and record every person (director/cast/crew page path) into `person`.
3. **Discover** — one extra tab expands unexpanded people into new film slugs via their filmography pages (`/director/…`, `/actor/…`, …), which workers then enrich.

Resume pointers are the DB itself: `movie.enriched_at IS NULL` (films to do) and `person.expanded_at IS NULL` (people to expand). Restarting re-queries both, so a resume is instant and never redoes work. Films skipped for a missing poster/title stay unenriched and are retried on later runs.

SIGINT/SIGTERM shut down gracefully: workers finish the current film, tabs close, the browser stops, and everything commits before exit (`Done: enriched X, Y failed, discovered Z films via W people`). Stopping and re-running later is the normal way to crawl in chunks.

Cloudflare challenges `/films/` browse pages (they never clear headless), so discovery walks the film→people→film graph instead — every page it touches is server-rendered and loads fine. Rating statistics appear to need login; everything else is public.

All user-facing commands go through `uv run tool movies ...`; do not add top-level script shims.

## DB Gotchas

The DB schema is old and loose in a few places; keep the helper functions in `movies.search` as the canonical access layer.

Current schema is created/migrated lazily by `movies.scraper.init_db()` and indexed by `movies.search.ensure_fts()` (called automatically at the top of every `search()`):

```sql
movie (
  slug TEXT PRIMARY KEY,    -- e.g. "the-traveler-1974-1"
  title TEXT,               -- display title, may contain \xa0 (non-breaking spaces)
  alt_title TEXT,           -- original-language title
  year TEXT/INTEGER,        -- old DBs may store TEXT; scraper declares INTEGER
  directors BLOB,           -- raw deflate-compressed JSON list of director slugs
  cast BLOB,                -- same encoding
  synopsis BLOB,            -- same encoding
  rating REAL,
  rating_count INTEGER,
  rating_dist BLOB,
  studios BLOB,
  crew BLOB,
  ...
)

movie_fts USING fts5(slug UNINDEXED, title, alt_title, directors_text, tokenize='trigram')
```

Gotchas:

- `movie.year` may be text even when scraper code inserts integers; use `_row_year(row)` before comparing.
- `movie.title` can contain non-breaking spaces; use `_normalize()` or `_clean_title()` for comparisons and filenames.
- `directors`, `cast`, `synopsis`, `rating_dist`, `studios`, and `crew` are raw deflate-compressed JSON blobs using `zlib.decompress(val, wbits=-15)` and `json.loads`; use `_blob(val)` for JSON lists and `_text(val)` where available for compressed strings.
- Search uses FTS5 plus `_rank_score(row, title, year)`: title similarity is primary, exact year match is a strong secondary signal, popularity is only a tiebreaker.
- `movie_fts` stores director display names in `directors_text`; `movies.search.ensure_fts()` rebuilds it automatically when the indexed movie count is stale.

## Search API

`src/tool/movies/search.py` is the canonical access layer for search, parsing, and DB-specific cleanup.

| Function | Purpose |
|---|---|
| `extract_title_year(stem)` | Parse title + year from a raw filename stem with no extension. Pure string logic, easily testable. |
| `parse_filename(filename)` | Thin wrapper around `extract_title_year`; also returns the lowercased extension without the dot. |
| `search(query, year, limit)` | FTS5 search, then re-rank `limit * 10` candidates by `_rank_score` before truncating. Pass `year` so year-matched results are not lost to popularity. |
| `match_confidence(title, year, row)` | 0-100 score: title similarity contributes 60% and year match 40% when a year is given; title similarity contributes 100% when no year is given. |
| `confidence_breakdown(title, year, row)` | Returns `{title: int, year: bool | None, pop: int}` for CLI display. |
| `_rank_score(row, title, year)` | Composite ranking score: title similarity first, exact year match second, popularity as a weak tiebreaker. |
| `_clean_title(row)` | Normalizes DB title whitespace, including `\xa0`, for display and filenames. |
| `_director_lastname(directors_blob)` | Last word of the first director's display name, using `_blob()` and `_name()`. |

Filename parsing heuristics in `extract_title_year()`:

1. Dot-separated scene release names with no spaces are normalized by replacing dots with spaces.
2. The first standalone `19xx` or `20xx` year is the anchor; everything before it is the title and everything after it is ignored.
3. Any open bracket or parenthesis immediately before the year is stripped, which handles names like `(2009, Huang Weikai)`.

Scoring weights are intentionally title-similarity-first:

- `title_sim * 3.0` is the primary signal; exact title matches should beat partial matches.
- `year_bonus * 5.0` is a strong secondary signal; it prevents a popular wrong-year film from winning.
- `pop * 0.5` is only a weak tiebreaker; pure popularity caused wrong matches such as _The Time Traveler's Wife_ beating _The Traveler_.

Popularity is normalized as `min(_popularity(row), 25.0) / 25.0`, where `_popularity = rating * log10(rating_count + 1)`.

## Filename Rename Flow

`movies.rename.propose_rename(path)` is dry-run logic only. It returns `(new_path, match_row, parsed_year, error)` and constructs names as:

```text
<director lastname> - <title> (<year>).<ext>
```

`movies.rename.apply_rename(path, new_path)` performs the filesystem rename. `movies.rename.collect_media(target)` accepts a file or recursively scans a directory for video and subtitle extensions.

## Smoke Checks

The parser/search tests do not need the DB. The manual DB smoke test is:

```bash
uv run tool movies search "disorder huang weikai"
```
