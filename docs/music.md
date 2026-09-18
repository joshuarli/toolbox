# `tool music`

## What this is

Three CLI tools for managing a local audio library:

- **`scan`** — walk a directory tree, probe every audio file with ffprobe, and
  print a colour-coded table of codec, bitrate, cover art, missing tags, and a
  lossy-transcode verdict.  Given a single file, prints a detailed per-file
  breakdown instead of the table.
- **`tag`** — fingerprint a single file with `fpcalc`, look up its MusicBrainz
  metadata via the AcoustID API (enriched with track number, genre, and album
  artist from MusicBrainz), interactively select a match with arrow keys, and
  write tags to the file with mutagen. Skips files that already have complete
  metadata (use `-f` to override). Transcodes opus/vorbis audio to AAC/M4A
  for tag compatibility and iOS playback. Also supports `--read` to inspect
  existing tags without any network call.
- **`transcode`** — transcode a single audio file to high-quality AAC in an
  M4A container via ffmpeg, preserving channel layout. Uses Apple AudioToolbox
  (`aac_at`) on macOS, ffmpeg's native `aac` elsewhere. Supports `-o` to
  specify the output path.

All three run through `uv run tool music scan ...` / `uv run tool music tag ...` / `uv run tool music transcode ...`.

## Working style

Music tooling is small and self-contained within `src/tool/music/`. Work on
it **inline** — never spawn sub-agents, explore agents, forks, or workflows.
Read files directly, edit files directly. The only background work is running
tests or the app itself.

## Separation of concerns

`scan` and `tag` are independent tools with a hard boundary:

| | `scan` | `tag` |
|---|---|---|
| **Domain** | Local analysis | Metadata lookup + mutation |
| **Reads tags with** | ffprobe (JSON output) | mutagen |
| **Writes tags?** | No (read-only) | Yes (mutagen) |
| **Network?** | No (local only) | Yes (AcoustID + MusicBrainz) |
| **Key dependency** | ffmpeg/ffprobe | fpcalc (chromaprint) |
| **Transcodes?** | No | Yes (opus/vorbis → AAC via ffmpeg) |

- `scan.py` owns the table-scan UI, ffprobe integration, and verdict display.
  It never imports mutagen.
- `tag.py` owns fingerprinting, API lookups, interactive selection, and tag
  mutation. It delegates tag I/O and transcoding to `tags.py`.
- `tags.py` is the single authority for mutagen tag read/write.
  Shared by both tools — scan reads tags through it, tag reads and writes
  through it.
- `transcode.py` handles ffmpeg audio transcoding (probe codec, opus/vorbis
  → AAC/M4A).  Used by `tags.py` when a file needs format conversion before
  tag writing.

They share:
- `constants.py` — mutagen-type quality tiers, ffprobe codec-name sets,
  ANSI colour codes, tag-name mappings
- `tags.py` — mutagen tag read/write (the single place that touches mutagen
  for metadata I/O)
- `transcode.py` — ffmpeg audio transcoding: probe codec, opus/vorbis → AAC/M4A
- `verdict.py` — lossy-transcode detection (used by scan; importable by tag)
- `ui.py` — ANSI terminal formatting helpers (colored, bold, dim, cursor)

## Layout

```
src/tool/music/
├── __init__.py      # empty
├── constants.py     # mutagen type tiers, ffprobe codec sets, colours, tag names
├── tags.py          # mutagen tag read/write (shared I/O)
├── transcode.py     # ffmpeg audio transcode (probe codec, opus/vorbis → AAC/M4A)
├── verdict.py       # multi-point spectral brickwall detector
├── ui.py            # ANSI 256-color terminal formatting (colored, bold, dim)
├── scan.py          # probe, format, collect, main() entry point
├── tag.py           # CLI orchestrator: fpcalc → AcoustID → MB → mutagen write
└── api/
    ├── __init__.py     # empty
    ├── acoustid.py     # fpcalc + AcoustID API lookup (rate-limited 3/sec) + result parsing
    ├── musicbrainz.py  # MusicBrainz search + recording lookup (rate-limited 1/sec)
    └── spotify.py      # unauthenticated Spotify playlist fetching
```

| Module | Purpose |
|---|---|
| `constants.py` | Shared data: mutagen-type quality tiers (`LOSSY_HIGH_TYPES`, `LOSSLESS_TYPES`, `DSD_TYPES` — use with `isinstance`), ffprobe codec-name sets (`LOSSY_HIGH`, `LOSSLESS`, `DSD` — for codec-level string matching), 256-color ANSI codes (`MAGENTA`, `GOLD`, …), `TAG_NAMES`, `AUDIO_EXTENSIONS`. No logic. |
| `tags.py` | Single authority for mutagen tag read/write. `read_tags()` / `write_tags()` — the only place that imports mutagen file types. `write_tags()` detects opus/vorbis types and delegates to `transcode.py` before writing, using a tempdir so the original is kept intact on failure. |
| `transcode.py` | ffmpeg audio transcoding. `probe_codec()` wraps ffprobe to detect the audio codec. `transcode_to_aac()` transcodes opus/vorbis to AAC at 256k in an M4A container using Apple AudioToolbox (`aac_at`) on macOS or ffmpeg's native `aac` elsewhere, with ffmpeg's progress bar shown on stderr. |
| `verdict.py` | Lossy-transcode detection via multi-point spectral slope profile. Module docstring is the canonical methodology reference. Exports `compute_verdict(codec, filepath, sample_rate, **thresholds)` → `(text, color, dim)`. One ffmpeg call per file (`asplit=N` in a `filter_complex` graph with FIR energy-above-frequency bands at 15/17/19/20.5/21.5 kHz, plus 25 kHz for hi-res files). Thresholds are module-level constants (`BRICKWALL_DROP_DB`, `LOW_ENERGY_DB`, `NO_HF_DB`, `HI_RES_NO_HF_DB`), overridable via kwargs and CLI flags. |
| `ui.py` | Shared ANSI formatting: `colored(text, code, bold, dim)`, `bold(text)`, `dim(text)`, `cursor_up(n)`, `clear_line()`, `clear_below()`. No business logic. |
| `scan.py` | The `scan` CLI. `probe()` wraps ffprobe. `collect_files()` walks dirs. `_process_one()` does the per-file work (probe → format → verdict). `main()` runs a `ThreadPoolExecutor` over files (default `cpu_count` workers, override with `-j N`). Column widths and ANSI formatting live here. Single-file mode (`_print_file_detail()`) prints a full breakdown: streams, all tags, verdict. Supports `--brickwall-threshold`, `--low-energy-db`, `--no-hf-db`, `--hi-res-no-hf-db`. |
| `tag.py` | The `tag` CLI. Orchestrator — delegates fingerprinting + AcoustID lookup to `api.acoustid`, MB enrichment to `api.musicbrainz`. Interactive arrow-key selector with live diff (`interactive_select()`). Enter writes immediately (no confirmation prompt); `s` skips. Skips files with complete metadata unless `-f`/`--force`. `-y` skips the write confirmation in non-TTY mode. `--read` inspects local tags without any network call. |
| `api/acoustid.py` | AcoustID API client. `get_audio_fingerprint()` runs fpcalc. `fetch_acoustid_metadata()` calls the AcoustID v2/lookup endpoint (rate-limited 3 req/sec). `extract_metadata()` parses the result into tag fields. Docs: https://acoustid.org/webservice |
| `api/musicbrainz.py` | MusicBrainz API client. `search()` queries MB recordings, `fetch_recording()` looks up a single recording for genre/track number/album artist. Rate-limited 1 req/sec. Exports `MB_BASE` and `USER_AGENT` for use by `resolver.py`. Docs: https://musicbrainz.org/doc/MusicBrainz_API |
| `api/spotify.py` | Unauthenticated Spotify playlist fetching via Spotify's private web-player API. `SpotifyPublicPlaylist.get_playlist()` returns playlist metadata and tracks. Docs: https://developer.spotify.com/documentation/web-api |

## Commands

```bash
uv run tool music scan ~/Music/FLAC                    # scan a directory
uv run tool music scan song.flac                       # single-file detailed breakdown
uv run tool music scan -j 4 ~/Music/FLAC               # 4-thread scan
uv run tool music scan --brickwall-threshold 30 .       # tune sensitivity
uv run tool music tag ~/Music/song.flac                # fingerprint + lookup + write (Enter=write, s=skip)
uv run tool music tag --read ~/Music/song.flac         # inspect existing tags (no network)
uv run tool music tag -f ~/Music/song.flac             # re-tag even if metadata is already complete
uv run tool music transcode ~/Music/song.opus          # transcode opus to AAC/M4A
uv run tool music transcode -o out.m4a song.webm       # specify output path
make fmtlint                                            # lint + format + type-check
uv run pytest tests/music
```

External prerequisites: `ffmpeg` / `ffprobe` (for scan), `fpcalc` from
[chromaprint](https://acoustid.org/chromaprint) (for tag). The scan tool
checks for ffprobe at startup; tag checks for fpcalc on first use.

## Architecture notes

### Verdict flow

```
scan.main()
  └─ ThreadPoolExecutor.map(_process_one, files)
       └─ probe(fp)            # ffprobe → JSON
       └─ compute_verdict()    # from verdict.py
            ├─ LOSSY_ALL?  → "N/A (Lossy Codec)"
            ├─ DSD?        → "Lossless Codec (DSD)"
            └─ LOSSLESS?   → analyze_steepness()
                 └─ single ffmpeg call: asplit=N + firequalizer + astats
                      ├─ overall RMS        (no filter)
                      ├─ RMS peak above 15  kHz
                      ├─ RMS peak above 17  kHz
                      ├─ RMS peak above 19  kHz
                      ├─ RMS peak above 20.5 kHz
                      ├─ RMS peak above 21.5 kHz
                      └─ RMS peak above 25  kHz (hi-res files only)
```

The astats outputs arrive in stderr in any order.  `analyze_steepness` sorts
the short-window RMS peak values descending: highest = overall, then ascending
analysis frequency.  The maximum adjacent drop across the standard 15–21.5 kHz
band pairs is checked against `BRICKWALL_DROP_DB` — a brickwall concentrates
its energy gap into a single pair, while a natural roll-off distributes the
drop evenly. Hi-res files also get a separate 25 kHz check; missing energy
there is reported as a likely upsample, not as a lossy transcode.

### Why asplit=N

A single ffmpeg call with `asplit=N` in a `filter_complex` graph decodes the
file once instead of N times — roughly N× faster than sequential calls.

### Thread safety

`_process_one()` touches no shared mutable state.  `subprocess.run` releases
the GIL.  `ThreadPoolExecutor.map()` preserves input ordering in the output
iterator — results print in file-system order.

### Multi-point slope profile

The detector measures short-window RMS peak at five frequency bands (15k, 17k,
19k, 20.5k, 21.5k Hz) plus an unfiltered overall level.  A single two-point
drop (e.g. 18→21.5 kHz) misses 128 kbps MP3 brickwalls at 16 kHz.  Five bands
catch brickwalls at any common encoder cutoff — the steepest adjacent drop is
the smoking gun. Using RMS peak-hold avoids diluting a full-bandwidth passage
with a sparse intro while still ignoring isolated sample peaks.

### Verdict thresholds

All in `verdict.py` as module-level constants, overridable via CLI flags on `scan`:

| Constant | Flag | Default | Meaning |
|---|---|---|---|
| `BRICKWALL_DROP_DB` | `--brickwall-threshold` | 25 dB | Min drop between adjacent HF bands to flag a codec brickwall |
| `LOW_ENERGY_DB` | `--low-energy-db` | -50 dB | Overall RMS below this = track too quiet to analyse |
| `NO_HF_DB` | `--no-hf-db` | -72 dB | RMS in 15 kHz band below this = no HF content at all |
| `HI_RES_NO_HF_DB` | `--hi-res-no-hf-db` | -50 dB | RMS above 25 kHz below this = likely 44.1/48 kHz upsample |

### TODO

- Build a calibration corpus: known lossless tracks, the same tracks transcoded
  to MP3/AAC/Opus at common bitrates and converted back to FLAC, plus a few
  known 44.1/48 kHz files upsampled to 96 kHz. Use it to tune
  `BRICKWALL_DROP_DB`, `LOW_ENERGY_DB`, `NO_HF_DB`, and `HI_RES_NO_HF_DB`
  against measured band profiles instead of intuition.

### Tag flow

```
tag.main()
  └─ --read?  → _print_tags()  (local only, exit)
  └─ read_tags()               # check if metadata already complete; skip unless -f
  └─ get_audio_fingerprint()   # fpcalc → (duration, fingerprint)
  └─ fetch_acoustid_metadata() # AcoustID API → results
  └─ interactive_select()      # arrow keys, live diff, Enter=write, s=skip
  └─ extract_metadata()        # AcoustID → {title, artist, album, date}
  └─ _fetch_musicbrainz()      # MusicBrainz API → {tracknumber, genre, albumartist}
  └─ format_diff()             # current vs new diff (no diff → exit)
  └─ write_tags()              # tags.py → transcode if opus/vorbis, then mutagen write
       └─ MutagenFile() is OggOpus/OggVorbis/None?
            └─ transcode_to_aac()  # transcode.py → ffprobe codec → ffmpeg aac 256k → .m4a
            └─ write tags to temp file, os.remove() original, shutil.move() into place
       └─ isinstance FLAC/Ogg? → Vorbis comments
       └─ isinstance MP3?      → EasyID3
       └─ isinstance MP4?      → EasyMP4
```
