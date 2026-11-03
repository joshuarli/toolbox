# Music manual queue

The automated passes have taken `~/Sync/M` as far as they can: 813 of 1144 files carry
durable identity and 895 embed a front cover. This note lists what is left, why automation
stops at each bucket, and the concrete way to finish it. Counts are from 2026-11-02.

Regenerate the numbers before starting:

```sh
uv run tool music audit ~/Sync/M
uv run tool music match-album ~/Sync/M --dry-run
```

`audit --json` gives per-file states; `match-album --dry-run` gives a per-folder reason.

## Buckets

| Bucket | Files | Why automation stops |
|---|---:|---|
| Mixed or missing album tags | ~208 | The folder is not one album, so there is no folder identity to match |
| Coherent album absent from MusicBrainz | 107 | No release exists under the tagged album and artist |
| Coherent album, ambiguous release | 16 | Several release groups or no track-count match |
| Recording identified, release ambiguous | 14 | Each recording appears on 2–4 release groups |
| Untagged | 11 | Date-named radio recordings with no title or artist |

The first three rows partition the 331 files. The last two are cross-cuts inside them: the
files in mixed folders that already have a recording MBID, or no tags at all.

## 1. Mixed and playlist folders

The queue is dominated by directories whose files belong to different albums, or whose
`album` tag is a playlist name. Folder matching needs one album and one artist, so these
never get a folder-level match.

Largest examples:

| Folder | Files | Shape |
|---|---:|---|
| `WIP/evan call/Evan Call Orchestra Concert 2025` | 28 | one album name, but every track has a different artist and none has an album artist |
| `WIP/agobay/keep/2024-may` | 16 | 16 album names |
| `WIP/agobay/keep/2024-june` | 10 | 10 album names |
| `WIP/agobay/keep/2024-july` | 10 | 10 album names |
| `WIP/agobay/keep/2024-aug` | 10 | 10 album names |
| `WIP/agobay/2024-july` | 10 | 10 album names |
| `WIP/agobay/2024-aug` | 10 | 10 album names |
| `Staging` | 10 | 4 album names |
| `misc` | 7 | 6 album names |
| `WIP/anime` | 7 | 2 album names |
| `WIP/agobay/2023-nov` | 7 | 7 album names |

The `agobay` date folders are download batches, not albums. Options, in order of effort:

- Keep them as batches and leave the files at whatever identity the fingerprint or name
  search gives them. This is a legitimate end state for playlist material.
- Tag the interesting tracks one at a time with `uv run tool music tag <file>`. The
  interactive picker chooses the release and writes the album.
- If a directory really is one album, fix its `album` and `albumartist` tags first, then
  run `match-album` on it. A folder only matches when every pending file agrees.

`Playlists/Chinese Heart/Album - Totally Giving Up` (6 files) is the clearest repair: six
files claim the playlist name while three neighbours are tagged `干脆`. Set the album to
`干脆` and `match-album` has a 10-track release to map them into.

## 2. Coherent albums absent from MusicBrainz

These folders agree on album and artist, but a release search returns nothing:

| Folder | Files | Album — artist |
|---|---:|---|
| `Music/Albums/Album - Sibelius, J.： Piano Music` | 23 | Sibelius, J.: Piano Music — Ralf Gothoni |
| `Albums/Breathing II` | 14 | Breathing II — Jeon Jin Hee |
| `Albums/Breathing` | 13 | Breathing — Jeon Jin Hee |
| `WIP/Fragments` | 8 | Fragments — Uväll |
| `Staging/Light` | 8 | Light — HAYA樂團 |
| `Staging/LUMINA` | 8 | LUMINA — Jane Antonia Cornish |
| `Music/bandcamp/Arigto - Lungs` | 7 | Lungs — ARIGTO |
| `Albums/isos` | 7 | isos — Koji Maruyama |
| `Albums/Clarity & Stimulus` | 6 | Clarity & Stimulus — Junya Nishimura |
| `Playlists/Chinese Heart/Album - Totally Giving Up` | 6 | Totally Giving Up — 那英 (the real album is `干脆`) |
| `Music/bandcamp/Altered States` | 4 | Altered States — Uväll |
| `Playlists/japan-countryside-road-trip` | 1 | 위키드 Part 1 — 오연준 |
| `Playlists/europe-trip` | 1 | Trio Ternura (Compacto 1973) — Trio Ternura |
| `Playlists/ambiant/Album - Recordings from the Åland Islands` | 1 | Recordings from the Åland Islands — Jeremiah Chiu, Marta Sofia Honer |

Options:

- Submit the release to MusicBrainz, then rerun `match-album` on the folder. This is the
  only path that also produces a cover.
- Accept recording-level identity: `autotag` can still identify individual tracks, and
  `resolve` can supply an album when a recording has exactly one release group.
- Tag by hand with `music tag` and pick a release from another edition.

## 3. Coherent folder, ambiguous release

| Folder | Files | Reason |
|---|---:|---|
| `Albums/anan ryoko - usubenifuji` | 9 | one release hit, no track-count match |
| `Music/bandcamp/黑馬河的兒子 The Son of Black Horse River` | 5 | one release hit, no track-count match |
| `Playlists/Driving America` | 1 | 10 hits across several release groups |
| `Playlists/Blues Soul Jazz` | 1 | 10 hits across several release groups |

For the first two, open the release in MusicBrainz and check the tracklist against the
files; if the folder is partial in a way the title check cannot confirm, tag those tracks
with `music tag`. For the singles, pick the edition interactively.

## 4. Recording identified, release ambiguous

Fourteen files have a recording MBID and an album tag but no unique release group: 13 have
2–4 groups and `Nina Simone — I Put a Spell On You` has 142. Run
`uv run tool music tag <file>` and choose the edition; `resolve` will keep refusing them.

## 5. Untagged

Eleven files have no title or artist. They are date-named radio recordings
(`evan-kinori-radio`, `Staging/evan-kinori-radio`, the remaining `kabuki-yusuke` files) and
cannot be parsed from the filename. Rename one to `Artist - Title` and rerun `autotag`, or
tag it by hand.

## 6. Artwork

- **42 files in 9 releases have no front cover in the Cover Art Archive**: ELEMENTS (16),
  dreamage (10), Over the Bakken Records (8), Sibelius and the Kalevala (3), and one each
  for Constellations, Traditional Folk Songs of Taiwan, Lume d'Lenha, Righteously, and
  Sid Meier's Civilization VII. Drop a `cover.jpg` into the album directory, or embed one
  with your tagger. `music art` never overwrites an existing sidecar without `--force`.
- **207 files have no album MBID**, so there is nothing to fetch until bucket 1, 2, or 3 is
  resolved.

## 7. Duplicate copies

The whole library fingerprints into 94 duplicate groups with 108 redundant copies. No group
mixes a complete file with a pending one, so duplicates cannot supply missing identity.
Deleting them is a storage decision:

```sh
uv run python - <<'PY'
from tool.music.api.acoustid import get_audio_fingerprint
from tool.music.scan import collect_files
for path, display in collect_files(["$HOME/Sync/M"]):
    duration, fingerprint = get_audio_fingerprint(str(path))
    print(fingerprint, duration, display)
PY
```

Group by fingerprint plus duration before deleting anything.

## Suggested order

1. Resolve the 16 ambiguous releases and 14 recording-only files with `music tag`; they are
   small and certain.
2. Repair the 那英 folder's album tags, rerun `match-album`, and add a cover for it.
3. Decide whether the `agobay` batches and `WIP` concert folders stay as playlist material
   or get curated track by track.
4. Submit the coherent albums that matter to MusicBrainz, then rerun `match-album`.
5. Add missing covers for the nine releases.
6. Delete duplicates only if storage matters.
