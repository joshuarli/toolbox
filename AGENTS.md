# tool

A monolith of personal command-line tools, invoked as `tool <name> ...` (`uv run tool <name> ...`).

Each tool lives at `src/tool/<name>.py` or `src/tool/<name>/` (subpackage) and
registers itself as a subcommand in `src/tool/cli.py`. Tests for a tool live
at `tests/test_<name>*.py` or `tests/<name>/`. Implementation notes, nuances,
and design rationale for a given tool live in `docs/<name>.md` — read the
relevant one before working on that tool.

Current tools:

- `sd` — destructively benchmark/verify a removable disk with fio. See `docs/sd.md`.
- `movies` — encode/audit video files and manage a Letterboxd-backed movie library. See `docs/movies.md`.
- `music` — scan/tag/transcode a local audio library, and suggest artists for a playlist. See `docs/music.md`.
- `ig` — log in to, scan, and archive Instagram profiles and posts. See `docs/ig.md`.
- `notes` — clean unreferenced `z-images/` files and localize remote images. See `docs/notes.md`.

Lint, format, and type-check with `make fmtlint`. Run tests with `uv run pytest`.

## `music tag` metadata

`tag` writes durable MusicBrainz identity, not just title/artist/album, so files can seed MusicBrainz /
ListenBrainz graph work without fuzzy-matching. Details in `docs/music.md`; the invariants:

- Flow: fpcalc → AcoustID (`api/acoustid.py`, one request) → release-aware `Candidate`s (one per
  recording × release × medium × track; MB search fallback yields the same shape) → user picks one →
  `api/musicbrainz.py::enrich` (one `release/{id}` lookup, or `recording/{id}` if no release) → `tags.py`.
  Never take "the first release"; the selected candidate decides release, release group and track.
- `metadata.py::Metadata` is the typed model (scalar `str | None`, multi-value `list[str]`), named with
  Picard's internal tag names. `tags.py` is the only place that knows on-disk representation
  (Vorbis / ID3 / MP4, Picard conventions) and only writes fields the `Metadata` sets, so unrelated tags
  and artwork survive. All MusicBrainz JSON parsing stays in `api/`.
- Identifier distinctions (Picard): `musicbrainz_recordingid` = recording MBID, written as Vorbis
  `MUSICBRAINZ_TRACKID`, ID3 `UFID:http://musicbrainz.org`, MP4 `MusicBrainz Track Id`.
  `musicbrainz_trackid` = *release track* MBID (`MUSICBRAINZ_RELEASETRACKID` / `TXXX:MusicBrainz Release
  Track Id`). `musicbrainz_albumid` = release, `musicbrainz_releasegroupid` = release group; artist IDs
  (`musicbrainz_artistid`, track credit) are distinct from `musicbrainz_albumartistid` (release credit).
  `acoustid_id` / `acoustid_fingerprint` are stored too; `--read` abbreviates the fingerprint.
- "Already complete" means identity (recording + release + release group, when an album is claimed) —
  never optional fields like label or ISRC. `--force` re-fetches.
- Tests are offline; `tests/music/fixtures/` holds recorded AcoustID/MusicBrainz responses. `tag` needs
  `ACOUSTID_API_KEY` (`vault ACOUSTID_API_KEY -- uv run tool music tag ...`); MusicBrainz needs no key.

`music suggest <playlist-dir>` prints suggestions; its only writes are auto-tags (`tag.autotag`, skipped with
`--no-tag`) on playlist files lacking artist identity, and only for unambiguous fingerprint matches (never
guesses a release). Seeds: playlist artists (MBIDs from tags, else resolved by name,
refusing ambiguity) → ListenBrainz similar artists (one request, scores normalised per seed) → minus
what `--library` already has. See `docs/music.md`.
