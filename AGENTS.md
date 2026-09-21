# tool

A monolith of personal command-line tools, invoked as `tool <name> ...` (`uv run tool <name> ...`).

Each tool lives at `src/tool/<name>.py` or `src/tool/<name>/` (subpackage) and
registers itself as a subcommand in `src/tool/cli.py`. Tests for a tool live
at `tests/test_<name>*.py` or `tests/<name>/`. Implementation notes, nuances,
and design rationale for a given tool live in `docs/<name>.md` — read the
relevant one before working on that tool.

Current tools:

- `sd` — destructively benchmark/verify a removable disk with fio. See `docs/sd.md`.
- `movies` — encode/audit video files and manage a Letterboxd-backed movie library. See `docs/movies.md`.
- `music` — scan/tag/transcode a local audio library. See `docs/music.md`.
- `ig` — log in to, scan, and archive Instagram profiles and posts. See `docs/ig.md`.
- `notes` — clean unreferenced `z-images/` files and localize remote images in the Obsidian vault. See `docs/notes.md`.

Lint, format, and type-check with `make fmtlint`. Run tests with `uv run pytest`.
