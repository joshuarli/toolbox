# `tool movies` — Notes & Nuances

## What this is

Personal movie tooling. It has two main jobs:

- encode and audit local video files
- maintain a local Letterboxd-backed SQLite movie library, with search, rename, scraping, image download, and a small web UI

All user-facing commands go through `uv run tool movies ...`.

## Commands

```bash
uv run tool movies encode hq /path/to/file.mkv
uv run tool movies encode uhq /path/to/file.mkv
uv run tool movies loudnorm /path/to/file.mp4
uv run tool movies mkvtomp4 /path/to/file.mkv        # dry run by default
uv run tool movies mkvtomp4 file.mkv --apply         # actually remux
uv run tool movies scan /path/to/movies /path/to/file.mkv
uv run tool movies search "disorder huang weikai"
uv run tool movies rename /path/to/movies            # dry run by default
uv run tool movies rename file.mkv --apply            # actually rename
uv run tool movies scrape --concurrency 2
uv run tool movies login                              # optional: login adds rating stats; scrape works logged-out
uv run tool movies generate-library generate /path/to/movies
uv run tool movies generate-library fetch "movie title"
uv run pytest tests/movies
```

Runtime Letterboxd data defaults to `var/letterboxd` (relative to the repo
root) and can be overridden with `MOVIES_DATA_DIR`.

External tools remain external prerequisites for the relevant commands:
`ffmpeg`, `ffprobe`, `cwebp`, and Chromium (driven via `shadowdriver`).

## Layout

| Module | Purpose |
|---|---|
| `src/tool/movies/cli.py` | `movies` subcommand tree, embedded under `tool.cli` |
| `src/tool/movies/paths.py` | Runtime paths rooted at `var/letterboxd` or `MOVIES_DATA_DIR` |
| `src/tool/movies/probe.py` | Shared ffprobe utilities, stream analysis, quality scoring |
| `src/tool/movies/encode.py` | Transcode files to HEVC/MP4 (hvc1, faststart) |
| `src/tool/movies/loudnorm.py` | Normalize audio loudness to -16 LUFS; stream-copies video/subs |
| `src/tool/movies/mkvtomp4.py` | Remux MKV to MP4 with `-c copy` (no re-encoding) |
| `src/tool/movies/crop.py` | Black-border detection/cropping for encode |
| `src/tool/movies/merge_srt.py` | Merge sidecar `.srt` files into `.mp4`/`.mkv` containers |
| `src/tool/movies/scan_encodes.py` | Audit a directory for quality issues, flag problems |
| `src/tool/movies/search.py` | Letterboxd DB search, filename parsing, match scoring |
| `src/tool/movies/rename.py` | Rename local media from Letterboxd DB matches |
| `src/tool/movies/scraper.py` | Scrape Letterboxd metadata into SQLite via Chromium (`shadowdriver`) |
| `src/tool/movies/generate_movie_library.py` | Legacy static movie-library generator |
| `src/tool/movies/legacy_assets/` | Old JS assets retained for reference |

## Letterboxd

Letterboxd-specific runtime paths, scraping, DB, search, and rename notes live in [movies-letterboxd.md](movies-letterboxd.md).

## Tests and checks

```bash
uv run ruff format .
uv run ruff check .
uv run ty check
uv run pytest tests/movies
```

The parser/search tests do not need the DB. Letterboxd-specific smoke checks are documented in [movies-letterboxd.md](movies-letterboxd.md).

---

## Encoder choice: VideoToolbox vs libx265

Two presets exist for a reason.

**`hq` (hevc_videotoolbox)** — daily driver. Apple's hardware media engine encodes in real time at near-zero CPU cost, and the output is decoded in hardware on every Apple Silicon Mac. The quality-per-bit is good, not great — VT's rate-distortion optimisation is simpler than x265's. At low bitrates (≤1 Mbps) the difference becomes visible as slightly softer textures and occasional blocking on motion.

**`x265-hq` (libx265 CRF 22 slow)** — archival maximum. libx265 at preset `slow` runs a much deeper RD search (more reference frames, better motion estimation, RDOQ). At CRF 22 with 10-bit output the perceptual quality is noticeably higher at the same or lower bitrate. Tradeoff: 10–30× slower than VT. Use for films you'll watch more than once on a proper screen.

Both produce standard HEVC Main 10 that macOS hardware-decodes identically at playback. The hardware advantage only applies at encode time.

---

## Quality knob (VT only)

`-q:v` maps to `kVTCompressionPropertyKey_Quality` (0.0–1.0). ffmpeg divides by 100, so `-q:v 85` → 0.85.

The knob controls target perceptual quality, not bitrate directly. VT will use as many bits as needed to hit the quality target, subject to the BPP ceiling (`-maxrate`). Two things interact:

- **Below the ceiling**: quality setting is the binding constraint. Higher q → higher bitrate → better quality.
- **At the ceiling**: the BPP cap kicks in and quality degrades gracefully rather than the file growing unboundedly.

Current settings: q=85, ceiling=0.150 BPP. For a 1080p 24fps film this caps at ~7.1 Mbps; for 704×352 at ~891 kbps. Raising q above 90 yields diminishing returns for most content (VT's quality curve plateaus).

For libx265 the analogous knob is CRF (Constant Rate Factor). Lower CRF = better quality. CRF 22 is "transparent for most content"; CRF 18 is "visually lossless." There is no BPP ceiling applied for x265 — the CRF curve is self-limiting.

---

## 10-bit (Main 10 profile)

Both presets encode to 10-bit even when the source is 8-bit. Why it matters:

- **Banding**: 8-bit HEVC must round DCT coefficients to 8-bit precision internally. Gradients (skies, fog, dark scenes) develop false contour lines — "banding." 10-bit halves the quantisation step size, pushing banding below the noise floor.
- **No file-size penalty**: at the same perceptual quality, 10-bit HEVC is typically the same size or smaller than 8-bit HEVC. The encoder has more precision to represent smooth areas cheaply.
- **Hardware decode**: HEVC Main 10 is hardware-decoded by the Apple Silicon media engine (M1 and later) and by Intel Macs with HEVC hardware support (2017+).

VT uses `-pix_fmt p010le` (NV12 10-bit) for 4:2:0 sources and `p210le` with
`main42210` for 4:2:2 sources, preserving the source chroma layout.  4:4:4 and
over-10-bit sources are rejected by the VT presets rather than silently reduced;
use a format-preserving software encoder when those formats must be retained.
libx265 uses `yuv420p10le` (planar).  The 4:2:0 VT path and libx265 path are
standard Main 10 at playback; the 4:2:2 VT path uses the HEVC range-extensions
profile.

---

## Spatial AQ (`-spatial_aq 1`, VT only)

VideoToolbox exposes spatial adaptive quantisation via `-spatial_aq 1`. Disabled by default. It analyses each frame for spatial complexity and reallocates bits within the frame: more bits to edges, textures, and faces; fewer bits to flat skies and walls.

Effect is most visible at constrained bitrates (≤1.5 Mbps) where the encoder is making hard tradeoffs. On a 704×352 film like _The Silent Holy Stones_ — a lot of static wide shots with textured rock and sky — spatial AQ prevents the sky from going blocky while faces stay sharp.

Not exposed in libx265 via ffmpeg's `-spatial_aq` flag; libx265 has its own AQ modes controlled via `-x265-params aq-mode=2:aq-strength=1.0`.

---

## Color space: the SD/HD/HDR split

This is the most error-prone part of encoding. Three cases:

### Color range (limited vs. full)

`color_range` is a fourth independent metadata field (`tv` = limited, 16–235 luma; `pc` = full, 0–255). Getting it wrong causes crushed blacks (full→limited) or lifted shadows/milky image (limited→full). macOS is colour-managed and AVFoundation respects this tag, so the effect is more visible here than on a TV.

`movies.encode` now reads `color_range` from the source and passes it through (defaulting to `tv` for untagged SDR content). The HDR override path also explicitly sets `tv`.

### SD content (width ≤ 720, or explicitly tagged smpte170m/bt601)
Standard definition film and video uses BT.601 / SMPTE 170M. Color primaries differ from BT.709 — reds are slightly more orange, blues slightly more purple. Tagging SD content as BT.709 causes a visible hue shift on colour-managed displays (macOS is colour-managed). Older encode pipelines blindly forced bt709 everywhere, which is why so many SD rips look slightly off.

`movies.encode` now reads `color_transfer`, `color_primaries`, and `color_space` from the source stream and passes them through unchanged for SDR content. If the source is untagged (unknown/unspecified), it defaults to bt709.

### HD content (BT.709)
Standard HD is bt709. Pass through as-is.

### HDR content (smpte2084 / HLG)
HDR masters use PQ (smpte2084 / HDR10 / Dolby Vision) or HLG (arib-std-b67). When transcoding to SDR 10-bit, those metadata tags must be replaced with bt709. If you don't, the player reads the EOTF curve from the stream, applies the wrong tone-map, and the image looks pale and washed out.

`movies.encode` detects `smpte2084` and `arib-std-b67` transfer functions and forces bt709 on the output. It does **not** do HDR→SDR tone mapping (that would require `-vf tonemap=hable` or similar). The pixel values are simply re-tagged as BT.709. This is correct if the source's actual pixel range is SDR (common with "HDR" web rips that have SDR pixel values with wrong tags), but will look wrong for true HDR masters. If you're encoding a genuine HDR Blu-ray, add a tone-map filter.

---

## Deinterlacing

`movies.scan_encodes` flags interlaced content; `movies.encode` now also deinterlaces it rather than passing fields through. The filter is `bwdif=mode=send_frame` — motion-adaptive, uses 5 fields (vs. yadif's 3), and outputs one progressive frame per field. A 29.97i source becomes 59.94p.

Consequences for the BPP ceiling: output fps doubles, so the maxrate cap doubles proportionally. This is intentional — 59.94p content genuinely has more data to encode.

---

## Container: MP4

All output is `.mp4`. Key flags:

**`-tag:v hvc1`** — critical for Apple compatibility. ffmpeg's default HEVC tag for MP4 is `hev1` (out-of-band parameter sets), which AVFoundation routes to software decode and QuickTime Player refuses to open. `hvc1` (in-band parameter sets) signals the structure AVFoundation expects, enabling hardware decode in QuickTime, IINA, Infuse, and AirPlay.

**`-movflags +faststart`** — rewrites the output to place the `moov` atom at the front of the file. Without this, the moov atom is at the end: local players seek to read it before starting, and AirPlay/HTTP streaming must download the whole file before playback begins. With faststart, playback starts immediately. The rewrite adds a few seconds to the encode for large files and temporarily requires extra disk space.

### Subtitle caveats

MP4 has one subtitle format: `mov_text` (QuickTime text). `movies.encode` handles each source type as follows:

| Source subtitle type | Output |
|---|---|
| SRT / WebVTT / mov_text | Converted to `mov_text` — text preserved |
| ASS / SSA | Converted to `mov_text` — **text preserved, styling lost** (fonts, colour, positioning dropped) |
| PGS (`hdmv_pgs_subtitle`) | **Dropped** — cannot be stored in MP4 |
| VobSub (`dvd_subtitle`) | **Dropped** — cannot be stored in MP4 |

Image-based subtitles (PGS, VobSub) are common on Blu-ray rips. If a source has these, they will not appear in the output. `movies.scan_encodes` labels subtitle stream types in its output so you can see what a file has before encoding.

Font attachments (embedded in MKV for ASS/SSA rendering) are also dropped — MP4 has no attachment stream type.

**`movies.loudnorm`** preserves the source container: run it on a `.mp4` file, get a `.mp4` back; run it on a `.mkv`, get a `.mkv`. This means it is safe to run on source MKV files (before encoding) as well as encoded MP4 files (after encoding).

---

## Audio: aac_at bitrate metadata

`aac_at` (AudioToolbox AAC) is the best AAC encoder on macOS — it produces hardware-decoded AAC-LC that sounds transparent at 192k stereo. However it does not write per-stream bitrate metadata into MP4 or MKV containers. ffprobe will return `bit_rate = null` for these streams.

**Loudness normalization** is handled by `movies.loudnorm` as a separate pass after encoding. It stream-copies video and subtitles untouched and only re-encodes audio. See `movies.loudnorm` section below for the two-pass design and the `aac_at`/`fltp` incompatibility. Target is −16 LUFS, chosen for home viewing (louder than broadcast −23). This is a CPU-side filter applied before `aac_at` encoding. If the source is already close to the target, the effect is inaudible.

**Surround bitrate scaling**: stereo encodes at 256 kbps; surround scales at ~64 kbps/channel (5.1 → 384 kbps, 7.1 → 448 kbps). If the source audio bitrate is known and below the target, the source bitrate is used instead to avoid inflating a low-bitrate source into a larger file for no quality gain.

`movies.scan_encodes` works around missing `bit_rate` with `probe_audio_br()`: it reads a short audio packet window and computes bitrate from actual packet sizes. If that sample looks suspiciously low (for example, leading silence in a VBR Opus stream), it falls back to a full-stream packet sum. This keeps normal scans fast while avoiding false `[LOW_AUDIO_BR]` flags.

The stale BPS tags you'll see in `ffprobe -show_entries stream_tags` are copied from the source file's stream metadata by ffmpeg's `-map_metadata 0`. They reflect the original file's audio bitrate, not the re-encoded stream. Don't trust them.

---

## movies loudnorm — audio loudness normalisation

`uv run tool movies loudnorm <file>` normalises to −16 LUFS / −1.5 dBTP (EBU R128). It stream-copies video and subtitles, re-encodes only audio. Run it on already-encoded files as a post-processing step.

Two passes:
1. **Measure** — `ffmpeg -map 0:a:0 -af loudnorm=print_format=json -f null /dev/null`. Audio-only decode, no encoding. The JSON output gives `target_offset`: the net dB gain capped by the TP ceiling.
2. **Encode** — applies `volume={offset}dB` with `aac_at`. The `volume=` filter preserves source sample format; `aac_at` initialises cleanly with it.

Why not `loudnorm` directly in the encode pass? `aac_at` (AudioToolbox) returns `kAudioCodecUnsupportedFormatError` (`fmt?` / `0x666D743F`) when a filtergraph is attached that outputs `fltp` (float planar). Adding `aformat=sample_fmts=s16` after `loudnorm` does not fix this — the AudioToolbox format negotiation fails before any frames are processed. The `volume=` filter avoids the issue entirely.

---

## BPP ceiling (VT only)

The ceiling (`H265_BPP_CEILING = 0.150`) is implemented as `-maxrate` + `-bufsize 4×maxrate`. It scales with resolution and frame rate:

```
max_bitrate = BPP_ceiling × width × height × fps
```

This prevents the quality knob from causing bloat when re-encoding a lossy source. A high-quality q=85 encode of a heavily compressed internet rip would faithfully preserve compression artifacts at high bitrate — the ceiling prevents that.

`movies.scan_encodes` uses the same BPP tiers for the `[BLOATED]` and `[THIN]` flags. The tier-A green zone is 0.010–0.070, yellow is 0.006–0.010 and 0.070–0.200. Our ceiling at 0.150 means expensive VT outputs can sit in yellow without being flagged as bloated; this is intentional because high BPP is acceptable but should not receive full quality-score credit.

---

## Re-encode quality guard

Before starting an encode, `tool movies encode` scores the source file (0–100) using `movies.probe.quality_score()`:

| Component | Points |
|---|---|
| Codec (AV1 = 50, HEVC/VP9 = 35, H.264 = 30) | 50 |
| BPP health (green = 40, acceptable = 20) | 40 |
| Bit depth (10-bit = 20, 8-bit = 10) | 20 |

Audio quality is reported separately with flags such as `[LOW_AUDIO_BR]`; it is not part of the quality score.
Scores are capped at 100.

If the score reaches 72 the user is shown the score and its breakdown and asked to confirm before encoding. ≥85 uses slightly stronger language. This prevents silent generation loss when re-encoding something that's already a good encode.

The scoring logic lives in `movies.probe.quality_score()` so it can be reused by other tooling.

---

## Possible improvements

**Tone mapping for true HDR sources**
Add a `hdr` or `hdr-sdr` preset that applies a proper tone-map filter before the VT encode:
```
-vf zscale=t=linear,tonemap=hable,zscale=t=bt709:p=bt709:m=bt709,format=p010le
```
This is a CPU-side filter pass — expensive but correct for Blu-ray HDR rips.

**Multi-pass for x265**
CRF already gives consistent quality across scenes. 2-pass (`-b:v` target) would give more predictable file sizes if that matters more than quality consistency.

**Chapter-aware encoding** ✓ done — `-force_key_frames chapters` on both presets. Also added `-g {fps×10}` (max 10-second GOP) so scrubbing anywhere in the film hits a keyframe within ~10 seconds, not just at chapter marks. x265 gets `keyint` in x265-params for the same effect.

**Subtitle burn-in detection**
If the source has hard-coded subs (baked into video pixels), there is no separate track — `movies.scan_encodes` labels these as `hard`. Could add an explicit `[HARD_SUBS]` flag. Note: PGS/VobSub image tracks are now dropped rather than copied, since MP4 cannot store them.

**AQ for libx265** ✓ done — `x265-hq` uses `aq-mode=3:aq-strength=0.8:psy-rd=2.0:psy-rdoq=1.0:no-open-gop=1:keyint={fps×10}`.
