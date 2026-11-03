# Music tagging handoff

State of the `~/Sync/M` tagging pass, what is left, and the techniques worth testing next.
The working commands are `music audit`, `music autotag`, and `music resolve`; see
[music.md](music.md) and [music-tagging.md](music-tagging.md) for their contracts.

## Where the library stands

| State | Before | Now |
|---|---:|---:|
| complete | 272 | 789 (68.4%) |
| missing identity | 806 | 295 |
| missing album identity | 40 | 44 |
| untagged | 34 | 26 |
| partial | 2 | 0 |

The library has 1154 audio files. The 274 WebM sources were replaced by lossless Opus
remuxes and then deleted after each output probed as valid. No source files remain
untagged only because of their container.

Every command below ran with zero failures. The final audit is
`uv run tool music audit ~/Sync/M`; `--json` returns the per-file states.

## What was run

- `music audit` to build the queue and track progress.
- `music autotag` over all 882 incomplete files: 479 fingerprint matches written, 403
  left for manual review, 274 lossless remuxes.
- `music resolve` in six passes: 431 releases chosen. It also replaced a mismatched
  album tag on 102 files where the recording had exactly one release group; those lines
  were marked `(album tag replaced)`.
- A final `music autotag` pass with the guarded fallback: 51 tagged (21 by
  fingerprint + exact name, 10 by name + duration, 20 by fingerprint), 321 left.
- Deleted 274 WebM sources, each after its `.opus` sibling probed with an audio stream.

## What remains

365 files: 295 missing identity, 44 missing album identity, 26 untagged.

- 313 of the 365 still carry an album tag. The queue is concentrated in album folders:
  `WIP/evan call/Evan Call Orchestra Concert 2025` (28),
  `Music/Albums/Album - Sibelius, J.: Piano Music` (23),
  `Albums/Breathing` + `Breathing II` (27),
  `Staging/LUMINA` + `Light` (16),
  `WIP/agobay` date folders (about 80 across 2023–2024),
  `Albums/anan ryoko - usubenifuji` (9), `Albums/isos` (7),
  `Music/bandcamp/Dreamage` (10), `Music/bandcamp/Arigto - Lungs` (7).
- 40 of the 365 are redundant copies of another pending file: 36 Chromaprint-identical
  groups covering 76 files. Tagging one copy would cover the rest.
- 44 missing-album files split into 30 whose recording has no release linked in
  MusicBrainz and 14 with several plausible release groups.
- 26 untagged files are mostly Bandcamp and playlist-folder rips with no descriptive tags.

## Techniques to investigate

### 1. Album/folder-level matching (largest expected gain)

Match a folder as a unit instead of one fingerprint at a time. This is how a track that
AcoustID cannot identify gets tagged from its neighbours.

Inputs already on disk: album tag, album artist, track/disc numbers, titles, and
durations. Search MusicBrainz releases by album title and album artist, then require:

- album title match using the normalization in `resolve._album_score`;
- total track count equal to the folder's audio-file count (or a subset when the folder
  is partial);
- every file mapped to a distinct track by disc/track number, or by normalized title
  when numbers are missing;
- one release group across the candidate releases; otherwise treat it as ambiguous.

On success, write `musicbrainz_recordingid`, `musicbrainz_albumid`,
`musicbrainz_releasegroupid`, and `musicbrainz_trackid` per file, then let `enrich` fill
the rest. `resolve.choose_release` already handles edition preference and album
normalization; the new part is choosing a release from an album query and mapping a
whole tracklist.

Risks: two different albums with the same title. Require the album artist match and a
complete tracklist match, and treat a partial match as no match.

Expected: the dominant lever; roughly 100–150 files by folder size, but unmeasured.
Suggested command shape: `music match-album DIR` with `--dry-run`, or a `--folder` mode on
`resolve`. A review-first mode that prints the proposed tracklist and applies on
confirmation would remove most of the false-positive risk for ~50 folders.

### 2. Duplicate propagation (safe, small)

36 fingerprint groups, 76 files, 40 redundant copies. `fpcalc` fingerprints are already
available through `get_audio_fingerprint`. For each group, if one file is complete, copy
its identity and descriptive tags to the rest with `read_tags` → `write_tags`.

Guard with duration equality and skip groups with no complete member. This carries no
identification risk, only the normal risk of sharing a fingerprint between different
masters.

Expected: up to 40 files; fewer if no member of a group is complete.

### 3. Filename fallback (small, strict)

26 files have no usable title/artist. Parse `Artist - Title` from the filename (strip
track numbers and `[youtube-id]`), then reuse the existing name-search guard: exact
title and artist, MusicBrainz score 100, and recording length within the larger of 3
seconds or 2%. Files such as `2024-12.webm` and date-only radio recordings are not
parseable and should stay out.

Expected: some of the 10 Dreamage files and a few playlist rips. The strict name
measurement suggests single-digit to low-double-digit yield.

### 4. Manual queue

`music tag` still resolves one file interactively. For the 44 missing-album files and the
hard remainder, this is the fallback. A useful addition would be a command that prints a
compact manual queue grouped by folder, excluding files already handled.

### 5. External identity sources (last resort)

The repository already has a Spotify client. ISRCs from another provider could be
searched against MusicBrainz, which might identify recordings AcoustID lacks. This needs
credentials, rate-limit handling, and a mapping from the external result to an MB
recording; treat it as a research spike, not the next pass.

## False-positive policy already in place

- Fingerprint matches require one recording among all candidates scoring at least 0.9.
- Conflicting fingerprint recordings are narrowed only by exact normalized title and
  artist; an album match is an additional tie-breaker.
- Name search requires an exact title and artist, a top MusicBrainz search score, one
  distinct recording, and a duration within `max(3 s, 2%)`.
- Release resolution writes only when candidates share one release group; a sole release
  group may override a mismatched album tag, and the run reports it.
- A name match writes recording-level identity only; `resolve` chooses the release later.

Measured on 231 files with no AcoustID match at 0.8 and usable tags: 16 had an exact
title/artist top hit, 13 agreed within 5 seconds, 12 within 3 seconds. That is why the
name fallback is deliberately narrow.

## Reproduce the measurements

```sh
uv run tool music audit ~/Sync/M --json > /tmp/audit.json
uv run tool music autotag --dry-run ~/Sync/M
uv run tool music resolve --dry-run ~/Sync/M
```

Group pending files by folder and album presence:

```sh
jaq -r '.files[] | select(.state != "complete") | .path' /tmp/audit.json \
  | sed 's|/[^/]*$||' | sort | uniq -c | sort -rn
```

Find duplicate pending audio with `get_audio_fingerprint` from
`tool.music.api.acoustid`; group by the returned fingerprint, then keep groups longer
than one. The earlier pass found 36 groups over 365 files.

## Environment notes

- `ACOUSTID_API_KEY` is read from the environment, falling back to
  `vault get ACOUSTID_API_KEY`. If `vault` reports a cancelled Secure Enclave prompt,
  approve the Touch ID/password prompt and rerun; a bare `vault get ACOUSTID_API_KEY`
  confirms access.
- `fpcalc` is required (`chromaprint`); `ffprobe`/`ffmpeg` are required for remuxing and
  scan.
- This Python build uses a Rustls-backed `ssl` module. `tool.music.http.Session` must
  keep passing an explicit `ssl_context`; without it urllib3 sets an unsupported option
  and every HTTPS request fails.
- Transcode treats taggability as the goal: taggable sources are skipped, remuxable
  codecs are copied losslessly, and `--encode` forces AAC.

## Code map and commits

| Area | File |
|---|---|
| queue audit and state buckets | `src/tool/music/audit.py` |
| batch tagging and fallback reporting | `src/tool/music/autotag.py` |
| fingerprint/name decision and write | `src/tool/music/tag.py` |
| release resolution and album matching | `src/tool/music/resolve.py` |
| MusicBrainz search, browse, enrich | `src/tool/music/api/musicbrainz.py` |
| lossless remux and taggability | `src/tool/music/transcode.py` |

Relevant commits: `08991b3` audit, `2e8c613` SSL context, `407f89e` autotag and remux,
`ff02f09` key guidance, `394c24a` transcode default, `92ed3c9` release resolution,
`4627191` sole release groups and edition titles, `7952dbb` guarded name fallback.

## Open decisions

1. Album matching: apply automatically when the tracklist matches uniquely, or review one
   proposal per folder before writing.
2. Duplicate propagation: copy full tags, or identity fields only so playlist-specific
   album tags survive.
3. Whether a sole release group may continue to replace a mismatched album tag. This was
   accepted for 83 files and is reported per file.
