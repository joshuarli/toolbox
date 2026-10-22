# tool

Personal CLI tools, invoked as `uv run tool <name> ...`.

- Build: `uv build`.
- Check: `make fmtlint` (read-only Ruff and ty checks).
- Format and apply Ruff fixes: `make fmt`.
- Tests: `uv run pytest`.

## Invariants

- `sd` runs only on physical removable or external whole disks on macOS 26+; a full run erases the target and leaves it unmounted.
- `movies encode --delete` removes a source only when the output probes strictly better; `rename` and `mkvtomp4` default to dry-run.
- `music tag` keeps the selected recording and release/medium/track identity when supplied; `tags.py` alone maps metadata to disk formats and only updates populated fields.
- `notes` defaults to dry-run and rejects `localize` paths outside the selected vault.

## Topic docs

- User setup and commands: [README.md](README.md)
- Removable-disk qualification: [docs/sd.md](docs/sd.md)
- Movie commands: [docs/movies.md](docs/movies.md)
- Encoding, remux, and loudness: [docs/movies-encoding.md](docs/movies-encoding.md)
- Letterboxd library and scraping: [docs/movies-letterboxd.md](docs/movies-letterboxd.md)
- Music commands: [docs/music.md](docs/music.md)
- MusicBrainz tags and identity: [docs/music-tagging.md](docs/music-tagging.md)
- Audio scan verdicts: [docs/music-scan.md](docs/music-scan.md)
- Obsidian vault tools: [docs/notes.md](docs/notes.md)
