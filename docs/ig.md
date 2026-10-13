# `tool ig`

The Instagram tool uses Chromium through the optional `shadowdriver` extension. Install it with `uv sync --extra browser`, then run `uv run tool ig login` once to establish the session. Browser commands use Shadowdriver's isolated profile and must not touch a personal browser profile.

The SQLite database defaults to `instagram.db` in the current directory; downloaded media defaults to site/images. `get`, `update`, and `build` accept `--media-dir` where applicable. `tool ig build` writes the static archive under site; `make site` builds and serves it locally.

## Scan and download

```sh
uv run tool ig login
uv run tool ig scan @username
uv run tool ig get @username
uv run tool ig scan --saved
uv run tool ig get --saved
uv run tool ig update
```

Normal profile scans use Instagram's timeline API when available and fall back to scrolling the profile grid. Saved-post scans record a separate saved membership. Without a profile argument, normal `scan` revisits known profiles; `scan --saved` uses the logged-in account's saved collection.

`get` downloads incomplete scanned posts; without a profile it processes incomplete collections in the database. `get --saved` limits that work to saved collections. `get-post` is a direct single-post path that accepts a shortcode or post/reel URL. `update` rescans known profiles and saved posts, then downloads pending media.

A post is complete only when its metadata and all expected media files exist. The same post can belong to both a normal profile and a saved collection; keep those memberships separate even though the post/media data is shared. HTTP 429 stops parallel downloads so unfinished rows can be retried later.

## Other commands

| Command | Behavior |
|---|---|
| `get-following @username` | Sync the account's following list to SQLite; `--refresh` forces a fresh fetch. |
| `follow @account ...` | Follow the requested accounts in Instagram. |
| `info` | Summarize profiles and collections; `--images` checks media files and `--usernames` prints scanned profile names. |
| `import-db FILE` | Import profile data from another Instagram database. |
| `remove @username` | Delete membership and associated media; use `--dry-run` to preview. Removal is immediate without that flag. |
| `doctor`, `fingerprint`, `kill` | Diagnose the managed browser profile, inspect its fingerprint, or stop its process. |
| `build` | Generate the local static archive from the database and media. |

Profile scan and download commands require `@username` when a profile is supplied. `--live` shows browser activity; `-j` controls concurrent post downloads. Use each subcommand's `--help` for remaining options.

The profile workflow lives in [profile_scan.py](../src/tool/ig/profile_scan.py), storage and completion checks in [db.py](../src/tool/ig/db.py), and the isolated browser adapter in [browser.py](../src/tool/ig/browser.py).
