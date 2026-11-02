# `tool music`

Music commands audit, autotag, resolve, scan, tag, transcode, and suggest artists for local audio files. Run `uv run tool music --help` or `uv run tool music <command> --help` for current flags.

| Command | Behavior |
|---|---|
| `audit` | Reports tag completeness across a directory: complete, missing identity, missing album identity, untagged, and partial, with per-format and per-directory breakdowns and the files that need work. Read-only and offline; `--json` for scripting. |
| `scan` | Reports codec, bitrate, artwork, missing tags, a lossy-transcode heuristic, and mastering flags; it does not write tags. See [music-scan.md](music-scan.md). |
| `tag` | Looks up one audio file and writes the selected MusicBrainz/AcoustID metadata. Use `--read` to inspect tags, `--force` to refresh identity, and `-y` to skip the write confirmation. Details: [music-tagging.md](music-tagging.md). |
| `autotag` | Burns down a directory's tagging queue: skips complete files, remuxes non-taggable containers losslessly, and writes unambiguous fingerprint matches. Ambiguous files are reported for `tag`. `--dry-run` lists the queue; `--delete-source` removes an original after a verified remux. |
| `resolve` | Chooses a release edition for files that already have a recording MBID, when the file's album and track point at one release group. Ambiguous files are left for `tag`; `--dry-run` lists the queue. |
| `suggest` | Ranks similar artists for a playlist directory or audio file. It can auto-tag unidentified playlist tracks; use `--no-tag` to avoid tag writes. |
| `transcode` | Moves audio into a taggable container with `ffmpeg`: a source that is already taggable is left alone, a source whose codec has a native taggable container is remuxed losslessly, and the rest are encoded to 320 kbps AAC/M4A. `--encode` forces the encoder, `-o` selects an output for one input, and `--overwrite` replaces an existing output. |

`audit` reads tags through the same adapter as `scan` and `tag` and makes no network requests. A file is `complete` when it satisfies the durable-identity rule in [music-tagging.md](music-tagging.md); `missing-identity` means title and artist exist but there is no recording MBID, `missing-album` means the recording is identified but a claimed album lacks release MBIDs, `untagged` means there is no title, artist, or album, and `partial` covers the rest. The report also flags containers `tag` cannot write, such as WebM, which need `transcode` first.

`autotag` is the batch form of the unambiguous half of `tag`: it never asks which release to use. A lossless remux preserves the source's existing tags, so a converted file usually starts in `missing-identity` or `missing-album`. Files left as `needs-manual` need `tool music tag <file>`; files reported as `needs-conversion` have no lossless taggable container and are not silently re-encoded. A service rejection such as an invalid AcoustID key stops the run instead of being reported as an ambiguous match.

`resolve` finishes the queue that `autotag` leaves at recording level. It browses the recording's releases, keeps those whose title matches the file's album (allowing an artist prefix or edition suffix) and whose track position matches, and writes only when those candidates share one release group. If no title matches, or no edition matches the track position, a sole release group carrying the recording is still used and the run marks the album tag as replaced. Among editions it prefers an official release, then the file's own media, country, and date when tagged, then Digital Media and Worldwide. Each choice prints the edition it used.

Without `-o`, transcode writes a same-stem file beside each input: `.opus`, `.ogg`, `.flac`, or `.m4a` from the audio codec, falling back to `.m4a` for the encoder. A source already in a taggable container is left alone; `--encode` forces AAC. Existing outputs are refused unless `--overwrite` is set.

`scan` and `transcode` need `ffprobe`/`ffmpeg`. `tag` and `autotag` need `fpcalc` and an `ACOUSTID_API_KEY`; when the environment lacks the key, the tool reads it with `vault get ACOUSTID_API_KEY`. MusicBrainz lookups need no key. `suggest` queries MusicBrainz and ListenBrainz, and auto-tagging needs the same AcoustID setup as `tag`.

Suggestions seed from playlist artist credits, resolve missing MBIDs by name, query ListenBrainz, and exclude artists already in the library. Auto-tagging is limited to unambiguous fingerprint matches.

The tool writes only through commands that explicitly modify files: `tag` updates the supplied file, `autotag` tags a library and may remux (`--delete-source` also removes the original), `resolve` writes a chosen release identity, `transcode` writes a converted file, and `suggest` may tag playlist files unless `--no-tag` is set. The default library exclusion for `suggest` is `~/Sync/M`; pass `--library` to use another directory.

The metadata model and its per-container mappings are documented in [music-tagging.md](music-tagging.md). Scan thresholds and the limits of its verdicts are in [music-scan.md](music-scan.md).
