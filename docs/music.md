# `tool music`

Music commands audit, scan, tag, transcode, and suggest artists for local audio files. Run `uv run tool music --help` or `uv run tool music <command> --help` for current flags.

| Command | Behavior |
|---|---|
| `audit` | Reports tag completeness across a directory: complete, missing identity, missing album identity, untagged, and partial, with per-format and per-directory breakdowns and the files that need work. Read-only and offline; `--json` for scripting. |
| `scan` | Reports codec, bitrate, artwork, missing tags, a lossy-transcode heuristic, and mastering flags; it does not write tags. See [music-scan.md](music-scan.md). |
| `tag` | Looks up one audio file and writes the selected MusicBrainz/AcoustID metadata. Use `--read` to inspect tags, `--force` to refresh identity, and `-y` to skip the write confirmation. Details: [music-tagging.md](music-tagging.md). |
| `suggest` | Ranks similar artists for a playlist directory or audio file. It can auto-tag unidentified playlist tracks; use `--no-tag` to avoid tag writes. |
| `transcode` | Converts audio to 320 kbps AAC/M4A with `ffmpeg`; `-o` selects an output for one input, and `--overwrite` replaces an existing output. |

`audit` reads tags through the same adapter as `scan` and `tag` and makes no network requests. A file is `complete` when it satisfies the durable-identity rule in [music-tagging.md](music-tagging.md); `missing-identity` means title and artist exist but there is no recording MBID, `missing-album` means the recording is identified but a claimed album lacks release MBIDs, `untagged` means there is no title, artist, or album, and `partial` covers the rest. The report also flags containers `tag` cannot write, such as WebM, which need `transcode` first.

Without `-o`, transcode writes a same-stem `.m4a` beside each input. Existing outputs are refused unless `--overwrite` is set.

`scan` and `transcode` need `ffprobe`/`ffmpeg`. `tag` needs `fpcalc` and an `ACOUSTID_API_KEY`; MusicBrainz lookups need no key. `suggest` queries MusicBrainz and ListenBrainz, and auto-tagging needs the same AcoustID setup as `tag`.

Suggestions seed from playlist artist credits, resolve missing MBIDs by name, query ListenBrainz, and exclude artists already in the library. Auto-tagging is limited to unambiguous fingerprint matches.

The tool writes only through commands that explicitly modify files: `tag` updates the supplied file, `transcode` writes an M4A, and `suggest` may tag playlist files unless `--no-tag` is set. Its default library exclusion for `suggest` is `~/Sync/M`; pass `--library` to use another directory.

The metadata model and its per-container mappings are documented in [music-tagging.md](music-tagging.md). Scan thresholds and the limits of its verdicts are in [music-scan.md](music-scan.md).
