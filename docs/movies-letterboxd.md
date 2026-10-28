# Letterboxd movie library

The Letterboxd database is SQLite at var/letterboxd/letterboxd.db, relative to the repository. Set `MOVIES_DATA_DIR` to use another directory; the database filename remains `letterboxd.db`.

Scraping and login use the optional `shadowdriver` Chromium integration
(`uv sync --extra browser`), installed from the adjacent `../shadowdriver/py`
checkout. Scraping works logged out for public film data; login is only needed
for rating statistics. The browser profile is managed separately from the
user's normal browser profile.

## Commands

```sh
uv run tool movies seed diary.csv
uv run tool movies scrape
uv run tool movies scrape film-slug
uv run tool movies search "title or director"
uv run tool movies rename ~/Movies
uv run tool movies rename ~/Movies --apply
uv run tool movies fetch-images
uv run tool movies serve
uv run tool movies serve --port 8000 --host 127.0.0.1
```

`seed` reads a Letterboxd diary/watchlist CSV export and adds its film slugs to the crawl. `scrape [slug]` crawls queued film and person pages into the database; `--concurrency`, `--live`, `--telemetry`, and `--rotate-tabs-every` control the crawl. Crawl progress is stored in SQLite so a later run continues with unfinished film/person rows.

`serve` starts the library web UI (film pages plus `/api/movies`, `/api/search`, and `/api/person`) on port 8000 by default.

`fetch-images` downloads poster and backdrop images for rows missing local copies, converting them to WebP with `cwebp` under `var/letterboxd/static/img/` (or `$MOVIES_DATA_DIR/static/img/`). Rows whose remote URL is an `empty-poster` placeholder are skipped.

`login` opens the managed browser profile. `kill` stops its background browser process. A full crawl can run without logging in, but rating statistics are omitted when unavailable.

`search` searches local film records. `rename` parses titles and years from video filenames, then proposes names from the closest database match; it is a dry-run until `--apply`.

## Database notes

The database is created and migrated lazily. Existing databases may contain years stored as either text or integers, and several detail columns are compressed JSON blobs. Use the helpers in [search.py](../src/tool/movies/search.py) instead of decoding those columns independently.

The main accessors for paths and scraping are [paths.py](../src/tool/movies/paths.py) and [scraper.py](../src/tool/movies/scraper.py).
