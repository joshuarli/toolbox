# Music tagging handoff

State of the `~/Sync/M` tagging pass, what is left, and the techniques worth testing next.
The working commands are `music audit`, `music autotag`, `music resolve`, and
`music art`; see [music.md](music.md), [music-tagging.md](music-tagging.md), and
[music-art.md](music-art.md) for their contracts.

## Where the library stands

| State | Before | Now |
|---|---:|---:|
| complete | 272 | 813 (71.1%) |
| missing identity | 806 | 306 |
| missing album identity | 40 | 14 |
| untagged | 34 | 11 |
| partial | 2 | 0 |
| embedded front cover | 0 | 895 (78.2%) |

The library has 1144 audio files. The 274 WebM sources were replaced by lossless Opus
remuxes and then deleted after each output probed as valid. No source files remain
untagged only because of their container. Forty Syncthing conflict files were removed
after hashing them against their same-stem sibling; 26 of them were the only copy of the
Sibelius album folder, recovered from the `.webm` sources on `/Volumes/UNTITLED/moved`
and remuxed back to Opus.

The final audit is `uv run tool music audit ~/Sync/M`; `--json` returns the per-file
states. Artwork coverage comes from `tool music art --dry-run` plus the Cover Art
Archive: 895 files embed a front cover, 11 album directories use `cover.jpg` sidecars, and
the remaining 249 split into 207 files with no album MBID and 42 files in nine releases
that have no front cover in the archive.

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
- `music art` over the library, twice: 311 covers embedded (48.9% → 75.8%). One archive
  item (`Is It or Isn't It Happening？`) returned HTTP 500 on the first pass and worked on
  a later one; the fault was the archive node, not the client.
- Removed malformed numeric MusicBrainz IDs from 30 files (`Albums/Breathing` ×13,
  `Albums/Breathing II` ×14, plus three playlist files). They came from another tagger and
  are not UUIDs; `music art` now reports them as skips instead of aborting the run.
- `music autotag` again: 15 tagged recording-only, 350 left for manual review.
- `music resolve` again: 1 release (Dire Straits — Sultans of Swing), 14 ambiguous.
- `music match-album` over the library: 5 folders matched (17 files written) across two
  passes, 15 ambiguous, 3 unmapped, 40 without album identity, 54 already complete. The
  second pass sized matches by pending files only, because complete files in a playlist
  folder belong to unrelated albums and were inflating the expected track count.
- `music art` over the matched folders: 16 covers embedded. The Civilization VII release
  has no front cover in the archive.
- Removed 40 Syncthing conflict files that were byte-identical to a sibling; 26 were the
  only copy of the Sibelius album and were restored from WebM sources before re-tagging.
- Filename fallback in `autotag`: 15 untagged files tagged at recording level from
  `Artist - Title` filenames, all matching the strict name + duration guard.
- Fingerprinted the whole library to re-measure duplicates: 94 duplicate groups and 108
  redundant copies, but **no group mixes a complete file with a pending one**, so
  duplicate propagation would have identified nothing.
- `music resolve` now accepts a sole release group when the file has no album tag. It
  added albums to 23 files (dreamage, HULDA, Keith Jarrett, and others); 6 album-less
  complete files stayed ambiguous. `music art` embedded 13 of the new covers; 10 belong
  to releases with no Cover Art Archive front.

## What remains

331 files: 306 missing identity, 14 missing album identity, 11 untagged. The per-bucket
plan for finishing them is in [music-manual-queue.md](music-manual-queue.md).

- 295 of the 331 still carry an album tag. The queue is concentrated in album folders:
  `WIP/evan call/Evan Call Orchestra Concert 2025` (28),
  `Music/Albums/Album - Sibelius, J.: Piano Music` (23),
  `WIP/agobay/keep/2024-may` (16), `Albums/Breathing` + `Breathing II` (27),
  `Music/bandcamp/Dreamage` (10), `Staging` (10),
  `WIP/agobay` date folders (about 50 across 2023–2024),
  `Albums/anan ryoko - usubenifuji` (9), `Albums/isos` (7).
- The duplicate measurement from the previous pass (40 redundant copies in 36
  Chromaprint-identical groups) predates the latest autotag/resolve passes; re-measure
  with `fpcalc` before acting on it.
- 14 files have recording identity but no release: 13 with 2–4 plausible release groups
  and one (Nina Simone — I Put a Spell On You) with 142.
- 26 untagged files are mostly Bandcamp and playlist-folder rips with no descriptive tags.

## Techniques

### 1. Album/folder-level matching (implemented, low ceiling on this queue)

`music match-album` is built and has run. It requires one album and artist across the
pending files, keeps releases whose track count fits the folder, maps files by normalized
title first and disc/track number second, and writes only when one release group and one
complete, distinct mapping survive. A partial folder may match a longer release, but only
when every position also agrees on the track title.

Measured on the 114 folders: 3 matched (15 files: *Moon Shines At Night*, *Into Silence*,
one Sibelius Festival track), 16 ambiguous, 4 unmapped, 40 without album identity, 51
already complete. The 100–150-file estimate was wrong: most remaining folders are
playlist or date dumps with mixed album tags, and the coherent albums that did fail
(*Breathing*, *isos*, *LUMINA*, *Light*, *Fragments*, the Sibelius piano compilation)
failed because no release exists under the tagged title and artist. The 那英 folder that
looked like the best candidate has six files tagged with the playlist name and three with
the album name, so folder identity is already mixed.

The next gain here is not more matching code but cleaning those folder-level album tags,
or relying on sole-release-group resolution, which `resolve` already does.

### 2. Duplicate propagation (measured, not useful here)

The whole library was fingerprinted: 94 duplicate groups, 108 redundant copies. No group
contains both a complete and a pending file, so there is nothing to propagate. The
duplicates are copies of already-tagged files or copies of files that are all pending;
deleting redundant copies is a storage decision, not a tagging one.

### 3. Filename fallback for untagged files (implemented)

`autotag` now parses `Artist - Title` from the filename when a file has neither tag and
runs the existing `_name_search` guard on it (exact title/artist, one recording, duration
within `max(3 s, 2%)`). It tagged 15 files: `dreamage` ×10 and `Felicia Sjögren - HULDA`
×5, all at recording level. The other 11 untagged files are date-named radio recordings
and do not parse.

Those files then have no album tag, so nothing claims a release and they never get
artwork. All 15 recordings have exactly one release group, so a sole-release-group rule
for files with no album tag would give them an album and a cover; `resolve` currently
refuses when the album tag is absent, by policy.

### 4. Manual queue

`music tag` still resolves one file interactively. For the 14 missing-album files and the
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
| folder-level release matching | `src/tool/music/album.py` |
| cover fetch, batch command | `src/tool/music/art.py`, `src/tool/music/api/coverart.py` |
| embedded/sidecar artwork I/O | `src/tool/music/artwork.py` |
| MusicBrainz search, browse, enrich | `src/tool/music/api/musicbrainz.py` |
| lossless remux and taggability | `src/tool/music/transcode.py` |

Relevant commits: `08991b3` audit, `2e8c613` SSL context, `407f89e` autotag and remux,
`ff02f09` key guidance, `394c24a` transcode default, `92ed3c9` release resolution,
`4627191` sole release groups and edition titles, `7952dbb` guarded name fallback,
`24190ca` Cover Art Archive embedding, `ad3407e` malformed-ID skips, `34ecb0c`
folder-level album matching, `f61a7fe` pending-file match sizing, `c93575e` filename
fallback, `690209e` sole release groups for album-less files.

## Open decisions

1. Whether a sole release group may continue to replace a mismatched album tag. This was
   accepted for 83 files and is reported per file.
2. Artwork in the 33 directories holding multiple releases: `--sidecar` skips them by
   design, so they currently rely on per-file embedding. Splitting sidecars by release, or
   keeping embedding as the only answer there, is unresolved.
