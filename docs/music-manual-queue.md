# Music manual queue

The automated passes and the 2026-11-03 manual pass have taken `~/Sync/M` to 874 of 1144
files with durable identity (76.4%), and 952 embed a front cover. This note lists what is
left, why each bucket is stuck, and what was deliberately skipped. Counts are from
2026-11-03.

Regenerate the numbers before starting:

```sh
uv run tool music audit ~/Sync/M
uv run tool music match-album ~/Sync/M --dry-run
uv run tool music art --dry-run ~/Sync/M
```

`audit --json` gives per-file states; `match-album --dry-run` gives a per-folder reason.

## State

| State | Files |
|---|---:|
| complete | 874 |
| missing identity | 264 |
| missing album identity | 0 |
| untagged | 6 |
| partial | 0 |

The remaining 270 split into three groups: coherent albums or folder-level items that
MusicBrainz does not have (100), mixed or playlist folders (164), and six untagged files.
Artwork is down to 43 files whose release has no front cover in the Cover Art Archive, plus
149 files with no release or release-group MBID to fetch from yet.

## What the manual pass settled

- **All 14 missing-album files got a release.** Each recording had 2–142 release groups;
  the edition was chosen from the file's own album tag, date, country, media, and track
  number:
  - Nina Simone — *I Put a Spell on You*, the 2018-11-30 digital edition matching the file's `20181129` date.
  - Trio Ternura — *A gira / Last Tango in Paris*, the 1973 Brazilian 7" the album tag calls *Compacto 1973*.
  - Michael Nyman — *The Very Best of Michael Nyman: Film Music 1980–2001*, GB CD; the two files are consecutive tracks 8 and 9 of disc 2.
  - Mt. Wolf — *Red*, the 2015-04-13 digital edition that has the file's 7 tracks.
  - AVA — *Waves*, the 2019-07-12 digital edition that has the file's 10 tracks.
  - 那英 — *干脆*, the CN 1999 edition already used by the three complete files in the same folder; six tracks mapped by recording MBID.
- **Evan Call — *Evan Call Orchestra Concert 2025*:** 28 of the 30 live tracks, mapped by
  title. The folder had no album artist, so `match-album` treated the varying per-track
  artists as a mixed folder; the two absent tracks were already complete.
- **Anan Ryoko — *Usubenifuji*:** nine of twelve tracks, mapped by title with the YouTube-ID suffix removed. The folder is partial.
- **王嘯 — *黑馬河的兒子*:** four of five tracks, mapped by title. The digital release orders the two bonus tracks differently, so track numbers could not be used.
- **Keith Jarrett — *The Melody at Night, With You*:** four untagged files, each confirmed by fingerprint, mapped to tracks 1, 4, 6, and 7.
- **Single tracks:** 齊秦 — 大約在冬季 (from *齊秦精選*), Jeremiah Chiu & Marta Sofia Honer — Snåcko (the Åland folder's other seven files already used the 2022 digital edition), and 齊秦 — 外面的世界 at recording level only because the recording appears on several albums.
- 57 covers were embedded for the releases those files joined.
- Verified absent from MusicBrainz under every plausible spelling, so nothing else was
  written for them: *Breathing* and *Breathing II* (Jeon Jin Hee / 전진희), *isos* (Koji
  Maruyama), *Fragments* and *Altered States* (Uväll), *Light* (HAYA樂團 / HAYA), *LUMINA*
  (Jane Antonia Cornish), *Lungs* (ARIGTO), *Clarity & Stimulus* (Junya Nishimura), and the
  Sibelius piano compilation (Ralf Gothóni).

## 1. Coherent albums absent from MusicBrainz

| Folder | Files | Album — artist |
|---|---:|---|
| `Music/Albums/Album - Sibelius, J.： Piano Music` | 23 | Sibelius, J.: Piano Music — Ralf Gothoni |
| `Albums/Breathing II` | 14 | Breathing II — Jeon Jin Hee |
| `Albums/Breathing` | 13 | Breathing — Jeon Jin Hee |
| `WIP/Fragments` | 8 | Fragments — Uväll |
| `Staging/LUMINA` | 8 | LUMINA — Jane Antonia Cornish |
| `Staging/Light` | 8 | Light — HAYA樂團 |
| `Music/bandcamp/Arigto - Lungs` | 7 | Lungs — ARIGTO |
| `Albums/isos` | 7 | isos — Koji Maruyama |
| `Albums/Clarity & Stimulus` | 6 | Clarity & Stimulus — Junya Nishimura |
| `Music/bandcamp/Altered States` | 4 | Altered States — Uväll |
| `Music/bandcamp/黑馬河的兒子 The Son of Black Horse River` | 1 | bonus track 塔里木河 (Live version), only on a physical edition |
| `Playlists/japan-countryside-road-trip` | 1 | 위키드 Part 1 — 오연준 |

The only remaining path that also produces a cover is to submit the release to MusicBrainz
and rerun `match-album` on the folder. The alternative is to accept the current state:
`autotag` can still identify individual tracks when AcoustID has them, and those files keep
whatever identity the fingerprint gave them.

## 2. Mixed and playlist folders

The queue is dominated by directories whose files belong to different albums, or whose
`album` tag is a playlist name. These are a legitimate end state for playlist material;
`autotag` has already written every unambiguous fingerprint match it could find.

Largest examples:

| Folder | Files | Shape |
|---|---:|---|
| `WIP/agobay/keep/2024-may` | 16 | 16 album names |
| `WIP/agobay/keep/2024-june` | 10 | 10 album names |
| `WIP/agobay/keep/2024-july` | 10 | 10 album names |
| `WIP/agobay/2024-july` | 10 | 10 album names |
| `Staging` | 10 | mixed singles |
| `WIP/agobay/keep/2024-aug` | 9 | 9 album names |
| `WIP/agobay/2024-aug` | 9 | 9 album names |
| `WIP/agobay/keep/2023-dec` | 7 | date batch |
| `WIP/agobay/2023-nov` | 7 | 7 album names |
| `misc` | 7 | 6 album names |
| `WIP/anime` | 6 | 2 album names |
| `WIP/agobay/2024-feb` | 6 | date batch |
| `archive/agobay` | 6 | date batch |

The `agobay` date folders are download batches, not albums. To curate one, tag the
interesting tracks one at a time with `uv run tool music tag <file>`; the interactive picker
chooses the release and writes the album. To match a folder as a unit, first fix its `album`
and `albumartist` tags, then run `match-album`.

## 3. Untagged

Six files have no title or artist. Five are date-named radio recordings (`evan-kinori-radio`,
`Staging/evan-kinori-radio`) and one is `WIP/Here’s “Those Eyes” …`, whose fingerprint has no
AcoustID match. They cannot be parsed from the filename; rename one to `Artist - Title` and
rerun `autotag`, or tag it by hand.

## 4. Artwork

43 files across nine directories have a release MBID but no front cover in the Cover Art
Archive. `music art` reports them as `miss`; nothing can be fetched until a cover is uploaded
to the archive or dropped in as `cover.jpg` beside the album. They are in ELEMENTS (16),
dreamage (10), Over the Bakken Records (8), the Sibelius piano compilation (3), `misc` (2),
and one each in *Continuum*, *interstellar-vibes*, the Japan playlist, and Civilization VII.

Another 149 files have no release or release-group MBID, so there is nothing to fetch for
them until bucket 1 or 2 is resolved.

## 5. Duplicate copies

The library fingerprints into 94 duplicate groups with 108 redundant copies. No group mixes
a complete file with a pending one, so duplicates cannot supply missing identity. Deleting
them is a storage decision. Group by fingerprint plus duration before deleting anything:

```sh
uv run python - <<'PY'
from tool.music.api.acoustid import get_audio_fingerprint
from tool.music.scan import collect_files
for path, display in collect_files(["$HOME/Sync/M"]):
    duration, fingerprint = get_audio_fingerprint(str(path))
    print(fingerprint, duration, display)
PY
```

## Suggested order

1. Decide whether the `agobay` batches and the `WIP` mixes stay as playlist material or get
   curated track by track.
2. Submit the coherent albums that matter to MusicBrainz, then rerun `match-album`.
3. Add missing covers for the nine directories, or upload them to the Cover Art Archive so
   `music art` can fetch them.
4. Delete duplicates only if storage matters.
