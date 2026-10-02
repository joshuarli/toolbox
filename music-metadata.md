Improve the `music` tool according to the goal.

The project is deliberately small and elegant. Preserve that character. Do not introduce a database, daemon, ORM, metadata cache, plugin framework, large abstraction hierarchy, or new dependency unless there is a compelling reason. Continue using the existing AcoustID → MusicBrainz flow, `mutagen`, the existing HTTP/rate-limiting infrastructure, and the current module boundaries.

For ACOUSTID_API_KEY, use `vault ACOUSTID_API_KEY -- cmd ...`

## Goal

Upgrade `tag` so that tagging does not merely write human-readable title/artist/album fields. A successfully tagged file should preserve as much useful, stable, interoperable identity and release metadata as reasonably available from AcoustID + MusicBrainz.

The key motivation is that these files will later be usable directly as seeds for graph-based music discovery via MusicBrainz and ListenBrainz. In particular, a tagged file should retain enough MusicBrainz identity that we never again need to fuzzy-match `{artist, album, title}` merely to determine what recording/release/release-group it is.

Use MusicBrainz Picard's established tag conventions wherever they exist. Do not invent private names for data that already has a conventional MusicBrainz/Picard representation.

Do **not** implement recommendations/discovery in this pass. This pass builds the durable metadata foundation for that work.

## Current shortcomings

Inspect the existing implementation before changing it.

At present:

- `TAG_FIELDS` contains only:
  - title
  - artist
  - album
  - date
  - tracknumber
  - albumartist
  - genre
- AcoustID returns an AcoustID result ID and a MusicBrainz recording MBID, but these identities are discarded after lookup.
- the Chromaprint fingerprint is computed but not persisted.
- the chosen MusicBrainz release identity/release-group identity is not persisted.
- artist MBIDs are discarded.
- useful release structure such as disc number, total tracks, media, country, label, catalog number, barcode, ISRC, etc. is discarded.
- the current code often effectively takes the first release returned for a recording. That is not sufficient once release-level identity matters.
- tag I/O assumes scalar strings and therefore cannot correctly represent all MusicBrainz multi-value metadata.

Fix those limitations cleanly rather than layering special cases on top of them.

## Metadata model

Introduce a small typed internal representation for enriched tag metadata rather than continuing to pass an increasingly ad-hoc `dict[str, str]`.

Keep it lightweight: a dataclass or similarly simple stdlib type is enough. Do not add Pydantic or another modeling dependency.

It must support both scalar and multi-value fields.

Persist, whenever the selected MusicBrainz data actually supplies them:

### Core descriptive metadata

- title
- artist / artist credit
- album
- albumartist / album artist credit
- track number
- total tracks
- disc number
- total discs
- release date
- original release date
- genre(s)
- artist sort name
- album artist sort name
- release country
- release status
- release type / primary release-group type
- media format
- label(s)
- catalog number(s)
- barcode
- ISRC(s)

Preserve full dates where MusicBrainz provides them; do not unnecessarily reduce `YYYY-MM-DD` to a year.

### Stable MusicBrainz identity

This is the most important part.

Persist:

- MusicBrainz recording MBID
- MusicBrainz release-track MBID
- MusicBrainz release MBID
- MusicBrainz release-group MBID
- track artist MBID(s)
- album/release artist MBID(s)
- MusicBrainz work MBID(s), when available and meaningfully associated

Use Picard-compatible semantic names and on-disk mappings, including its distinction between:

- recording ID
- release track ID
- release ID
- release group ID

Do not conflate `musicbrainz_recordingid` with `musicbrainz_trackid`; Picard's historical on-disk naming is slightly confusing, so follow the documented mappings rather than guessing from field names.

### Acoustic identity

Persist:

- AcoustID
- Chromaprint/AcoustID fingerprint

The fingerprint is already computed during tagging, so throwing it away is pointless.

Use Picard-compatible AcoustID tag names.

### Relationship-derived credits

Where MusicBrainz can provide them without turning this into an unbounded crawler, retain useful conventional credits such as:

- composer
- lyricist/writer
- arranger
- producer
- conductor
- remixer
- performers/instruments

Prefer standard/Picard-compatible names where available.

Do not attempt to serialize the entire MusicBrainz relationship graph into the audio file. The rule is “retain useful durable music metadata,” not “embed MusicBrainz JSON.”

Never store raw API response JSON blobs in tags.

## Correct release selection

This deserves special care.

A recording can occur on many releases, and release identity now matters. The current approach of implicitly using the first release is no longer acceptable.

Refactor the lookup/selection flow so that the object ultimately selected by the user identifies an exact:

- recording
- release
- medium
- release track

where that information exists.

It is fine to normalize raw AcoustID/MusicBrainz responses into small candidate objects before presenting them.

If one AcoustID recording appears on several releases, the UI must make the distinction visible enough that the user can select the correct edition. Useful distinguishing information includes album/release title, date, country, medium, label/catalog number, etc., but keep the selector compact.

Fallback MusicBrainz search must ultimately resolve to the same release-aware representation.

Do not silently choose an arbitrary release simply because MusicBrainz returned it first.

Once the exact release is known, enrich against MusicBrainz using the minimum sensible number of API calls.

The project already respects MusicBrainz's 1 request/sec requirement and AcoustID's 3 requests/sec recommendation. Preserve that behavior.

## AcoustID lookup

Expand the existing AcoustID request metadata to collect the useful identifiers AcoustID already exposes, including release-group and release IDs and ISRC information where appropriate.

Capture the AcoustID result ID itself.

Keep AcoustID as the fingerprint/identity resolver; MusicBrainz should remain the authoritative source for the richer canonical metadata.

## MusicBrainz enrichment

Expand `api/musicbrainz.py` cleanly.

Use MusicBrainz lookup `inc=` capabilities instead of issuing lots of tiny searches.

For the selected entities, retrieve the useful combination of:

- artist credits
- releases / release groups
- media and tracks
- labels
- ISRCs
- genres/tags where useful
- appropriate relationships for works and credits

Do not crawl unrelated entities.

Keep all MusicBrainz parsing in the API/domain layer; `tag.py` should remain an orchestrator rather than accumulating raw JSON traversal logic.

## On-disk tag compatibility

Follow MusicBrainz Picard's mappings wherever possible.

In particular, correctly support the project’s writable formats:

- FLAC / Vorbis comments
- Ogg Vorbis / Opus
- MP3 / ID3
- MP4 / M4A

Examples of conventions that should interoperate with Picard include:

- `MUSICBRAINZ_TRACKID` in Vorbis for the **recording MBID**
- `MUSICBRAINZ_RELEASETRACKID` for the release-track MBID
- `MUSICBRAINZ_ALBUMID`
- `MUSICBRAINZ_RELEASEGROUPID`
- `MUSICBRAINZ_ARTISTID`
- `MUSICBRAINZ_ALBUMARTISTID`
- `ACOUSTID_ID`
- `ACOUSTID_FINGERPRINT`

For ID3 and MP4 use Picard's corresponding UFID/TXXX/freeform-atom conventions rather than just uppercasing field names.

The current EasyID3/EasyMP4-only approach will probably be insufficient for all custom MusicBrainz tags. Refactor `tags.py` as needed, but keep it the **single authority** for on-disk metadata representation.

Do not scatter ID3/MP4/Vorbis implementation details into `tag.py` or API modules.

### Multi-value semantics

Do not flatten genuinely multi-value identifiers or metadata into an ambiguous comma-separated string merely to fit the old `dict[str, str]` model.

Where the container supports multiple values, preserve them properly.

Where a format convention specifies a particular representation, follow that convention.

Reading after writing must reconstruct the same logical metadata.

## Preserve unrelated tags

Writing enriched metadata must not destroy unrelated existing tags.

Only update fields owned by this tool. Preserve artwork and arbitrary unrelated metadata already present in the file.

This needs explicit regression coverage.

## Read/diff UI

Extend `tag --read` and the existing diff display to understand the richer metadata model.

Keep normal output readable. Stable IDs should be inspectable, but do not let a multi-kilobyte Chromaprint fingerprint make ordinary output useless; abbreviating its display while retaining the full stored value is fine.

The interactive selection UI should continue to feel as minimal as it does now.

## “Already complete” behavior

The current skip logic treats the old seven descriptive fields as completeness.

Change this.

A legacy file with `title`, `artist`, `album`, etc. but no MusicBrainz identity should **not** be considered fully tagged.

Define a sensible notion of core completeness centered around durable identity. At minimum, when the upstream match supplies them, we should retain recording/release/release-group identity.

Do not make optional MusicBrainz fields that legitimately may not exist cause `tag` to re-fetch the same track forever.

`--force` should continue to force a refresh.

## Architecture

Maintain the current boundaries:

- `api/acoustid.py`: fingerprint lookup / AcoustID parsing
- `api/musicbrainz.py`: MusicBrainz lookup and enrichment
- `tags.py`: canonical metadata schema ↔ actual audio-container tags
- `tag.py`: CLI orchestration and selection UI

A small dedicated metadata/model module is reasonable if that makes the boundaries cleaner.

Do not merge the APIs together and do not introduce generic “provider” machinery. There are only two relevant providers here and explicit code is preferable.

Avoid new dependencies unless genuinely necessary.

## Testing

All automated tests must be offline and deterministic. Never make real AcoustID or MusicBrainz requests in the test suite.

Add realistic fixture responses representing at least:

1. a straightforward single-release recording;
2. one recording present on multiple releases, proving that the selected release controls the resulting release/release-group/track IDs;
3. multiple credited artists;
4. multi-disc media;
5. labels/catalog numbers and ISRCs;
6. relationship-derived work/composer metadata where present;
7. incomplete MusicBrainz metadata where optional fields are absent.

Add broad tag round-trip tests for the writable formats where practical:

- write rich metadata;
- read it back;
- verify IDs and multi-values survive;
- verify unrelated pre-existing tags survive.

Especially test the tricky mapping distinctions:

- recording MBID vs release-track MBID;
- release MBID vs release-group MBID;
- artist IDs vs album-artist IDs;
- AcoustID ID and fingerprint.

Prefer integration-style tests around the actual metadata flow over dozens of trivial helper unit tests.

No network access should be required.

## Documentation

Update `AGENTS.md` to reflect the richer tagging flow, metadata model, and the important identifier distinctions.

Keep the documentation concise enough that it remains useful as repository guidance.

## Verification

Run the complete existing suite plus:

```bash
uv run ruff format .
uv run ruff check .
uv run ty check
uv run pytest
```

Fix all failures rather than weakening lint/type settings.

Finally perform a manual code review looking specifically for:

- arbitrary “first release wins” behavior that survived the refactor;
- stable IDs that are still fetched but discarded;
- accidental flattening of multi-value fields;
- format-specific tag mappings that disagree with Picard conventions;
- writes that destroy unrelated existing tags;
- unnecessary API requests;
- unnecessary abstractions or dependencies.

The end state should still feel like the same tiny `music` project, just with files that are now richly and durably identified.
