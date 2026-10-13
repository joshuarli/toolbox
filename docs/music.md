# `tool music`

Music commands scan, tag, transcode, and suggest artists for local audio files. Run `uv run tool music --help` or `uv run tool music <command> --help` for current flags.

| Command | Behavior |
|---|---|
| `scan` | Reports codec, bitrate, artwork, missing tags, a lossy-transcode heuristic, and mastering flags; it does not write tags. See [music-scan.md](music-scan.md). |
| `tag` | Looks up one audio file and writes the selected MusicBrainz/AcoustID metadata. Use `--read` to inspect tags, `--force` to refresh identity, and `-y` to skip the write confirmation. Details: [music-tagging.md](music-tagging.md). |
| `suggest` | Ranks similar artists for a playlist directory or audio file. It can auto-tag unidentified playlist tracks; use `--no-tag` to avoid tag writes. |
| `transcode` | Converts audio to 320 kbps AAC/M4A with `ffmpeg`; `-o` selects an output for one input. |

Without `-o`, transcode writes a same-stem `.m4a` beside each input.

`scan` and `transcode` need `ffprobe`/`ffmpeg`. `tag` needs `fpcalc` and an `ACOUSTID_API_KEY`; MusicBrainz lookups need no key. `suggest` queries MusicBrainz and ListenBrainz, and auto-tagging needs the same AcoustID setup as `tag`.

Suggestions seed from playlist artist credits, resolve missing MBIDs by name, query ListenBrainz, and exclude artists already in the library. Auto-tagging is limited to unambiguous fingerprint matches.

The tool writes only through commands that explicitly modify files: `tag` updates the supplied file, `transcode` writes an M4A, and `suggest` may tag playlist files unless `--no-tag` is set. Its default library exclusion for `suggest` is `~/Sync/M`; pass `--library` to use another directory.

The metadata model and its per-container mappings are documented in [music-tagging.md](music-tagging.md). Scan thresholds and the limits of its verdicts are in [music-scan.md](music-scan.md).
