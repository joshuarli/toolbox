# tool

A monolith of personal command-line tools.

```sh
uv run tool sd ...       # destructively benchmark/verify a removable disk with fio — docs/sd.md
uv run tool movies ...   # encode/audit video files, manage a Letterboxd-backed movie library — docs/movies.md
uv run tool music ...    # scan/tag/transcode a local audio library — docs/music.md
uv run tool ig ...       # log in to, scan, and archive Instagram profiles and posts — docs/ig.md
```

See `docs/<name>.md` for each tool's usage and design notes, and `AGENTS.md`
for how the repo is laid out.

## Development

```sh
make fmtlint    # ruff check --fix --unsafe-fixes + ruff format + ty check, in place
uv run pytest
```
