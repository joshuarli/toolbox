# tool

A collection of command-line tools for removable disks, movie files, music libraries, and Obsidian notes.

## Install and run

Requires `uv` and Python 3.15 or newer. From adjacent `toolbox` and
`shadowdriver` checkouts:

```sh
git clone https://github.com/joshuarli/tool.git
git clone https://github.com/joshuarli/shadowdriver.git
cd tool
uv sync
uv run tool --help
```

Letterboxd scraping uses the optional browser extra:

```sh
uv sync --extra browser
```

The browser extra uses the adjacent `../shadowdriver/py` checkout.

Run a command with `uv run tool <name> ...`. For example:

```sh
uv run tool movies --help
uv run tool music scan ~/Music
uv run tool notes clean
```

`tool sd` is destructive in its normal mode: it erases the selected whole disk. Read [the sd guide](docs/sd.md) before running it.

## Configuration and data

- `music tag` needs `fpcalc` and an `ACOUSTID_API_KEY` environment variable; it updates the supplied audio file.
- Video encoding and music transcoding write their outputs beside the source by default.
- The Letterboxd database defaults to var/letterboxd/letterboxd.db; `MOVIES_DATA_DIR` changes its parent directory.
- Notes commands use `~/iCloud~md~obsidian/Documents/Notes` by default; pass `--vault` to select another vault. Both commands preview changes unless given `--apply`.

Use `uv run tool <name> --help` and the [tool guides in `docs/`](docs/) for command options, requirements, and output details.

This repository does not declare a license.
