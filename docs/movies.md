# `tool movies`

The `movies` commands encode, inspect, and remux local video files and maintain a Letterboxd-backed movie database. Local video operations use `ffmpeg` and `ffprobe`; scraping and login need the optional browser extra (`uv sync --extra browser`).

Run `uv run tool movies --help` or `uv run tool movies <command> --help` for current options.

## Local files

| Command | Use |
|---|---|
| `encode` | Encode with one of the listed HEVC presets; behavior and HDR/subtitle handling are in [movies-encoding.md](movies-encoding.md). |
| `scan` | Audit video files for encode quality issues. Defaults to the current directory. |
| `mkvtomp4` | Plan a remux of MKV files to MP4. Dry-run by default; `--apply` writes outputs. |
| `loudnorm` | Normalize the first audio stream and write a sibling output when normalization is needed. |
| `merge-srt` | Merge matching sidecar SRT files into MP4/MKV files in a directory. |
| `generate-library` | Generate a static movie-library page or fetch metadata for one title. |

## Letterboxd library

`search`, `rename`, `scrape`, `seed`, `login`, `kill`, `serve`, and `fetch-images` use the local Letterboxd database. `rename` previews changes unless `--apply` is passed. See [movies-letterboxd.md](movies-letterboxd.md) for database location, browser setup, and crawl behavior.

Examples:

```sh
uv run tool movies encode hq ~/Movies/source.mkv
uv run tool movies scan ~/Movies
uv run tool movies mkvtomp4 ~/Movies/source.mkv
uv run tool movies search "movie title"
uv run tool movies rename ~/Movies
```
