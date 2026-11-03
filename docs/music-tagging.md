# Music tagging and identity

`tool music tag FILE` fingerprints one file with `fpcalc`, sends one lookup request to AcoustID, shows release-aware candidates, and enriches the selected candidate from MusicBrainz. If AcoustID returns no candidates, MusicBrainz search uses existing tags and then a normalized filename. A successful search with no matches is reported as such; a failed request is reported as an error. The selected candidate determines the release, release group, medium, and track when those are supplied; the tool never silently chooses the first release.

The typed `Metadata` model uses MusicBrainz Picard field names. Scalar values use `str | None`; multi-value fields use `list[str]`. MusicBrainz JSON parsing stays in the API modules, while `tags.py` owns the conversion between `Metadata` and audio-container tags.

## Identity fields

| Field | Meaning |
|---|---|
| `musicbrainz_recordingid` | Recording MBID. This is distinct from the release-track ID. |
| `musicbrainz_trackid` | Release-track MBID for the selected medium/track. |
| `musicbrainz_albumid` | Release MBID. |
| `musicbrainz_releasegroupid` | Release-group MBID. |
| `musicbrainz_artistid` | Track artist-credit MBID(s). |
| `musicbrainz_albumartistid` | Release artist-credit MBID(s), distinct from track artists. |
| `musicbrainz_workid` | Associated work MBID(s), when available. |
| `acoustid_id` / `acoustid_fingerprint` | AcoustID result and full Chromaprint fingerprint. |

Picard's on-disk names are easy to confuse. In Vorbis comments, the recording MBID is `MUSICBRAINZ_TRACKID`, while the release-track MBID is `MUSICBRAINZ_RELEASETRACKID`.

| Identity | Vorbis comments | ID3 | MP4 freeform/standard tag |
|---|---|---|---|
| Recording MBID | `MUSICBRAINZ_TRACKID` | `UFID:http://musicbrainz.org` | `MusicBrainz Track Id` |
| Release-track MBID | `MUSICBRAINZ_RELEASETRACKID` | `TXXX:MusicBrainz Release Track Id` | `MusicBrainz Release Track Id` |
| Release MBID | `MUSICBRAINZ_ALBUMID` | `TXXX:MusicBrainz Album Id` | `MusicBrainz Album Id` |
| Release-group MBID | `MUSICBRAINZ_RELEASEGROUPID` | `TXXX:MusicBrainz Release Group Id` | `MusicBrainz Release Group Id` |
| Track artist MBIDs | `MUSICBRAINZ_ARTISTID` | `TXXX:MusicBrainz Artist Id` | `MusicBrainz Artist Id` |
| Release artist MBIDs | `MUSICBRAINZ_ALBUMARTISTID` | `TXXX:MusicBrainz Album Artist Id` | `MusicBrainz Album Artist Id` |
| AcoustID | `ACOUSTID_ID` / `ACOUSTID_FINGERPRINT` | `TXXX:Acoustid Id` / `TXXX:Acoustid Fingerprint` | `Acoustid Id` / `Acoustid Fingerprint` |

Tag I/O supports FLAC, Ogg Vorbis/FLAC, Opus, MP3, and MP4/M4A. It writes only fields present in the selected `Metadata`, preserving artwork and unrelated tags. Genre, artist/work IDs, credits, labels, catalog numbers, and ISRCs retain their multi-value form where the container supports it.

A file is complete when title, artist, and recording MBID are present; if an album is claimed, release and release-group MBIDs are required too. Optional fields such as label or ISRC do not trigger repeated lookups. `--force` refreshes the tags. `--read` prints current tags without network access.

`music autotag DIR` applies the same unambiguous-match rule to a whole directory: it skips complete files, writes recording-level identity when the release is ambiguous, and leaves the rest for `music tag`. It resolves conflicting fingerprint candidates by exact title and artist, and falls back to a MusicBrainz name search only for an exact title/artist whose length matches the file. A file with no title or artist tag uses an `Artist - Title` filename for that same guarded search. `music resolve DIR` then revisits files with recording identity and chooses a release edition when the album and track point at one release group.

`music match-album DIR` identifies a folder as a unit when per-file lookups have nothing to work with. It requires one album title and one artist across the pending files, searches releases by that pair, and keeps only releases whose track count fits the pending files: equal to the pending count, or larger for a partial folder, where every file must also agree with its track title. Complete files in the directory are ignored, so a playlist folder that mixes unrelated albums does not block a single pending track. Files map to tracks by normalized title first and by disc/track number second. The folder is written only when one release group survives and every file maps to a distinct track; otherwise nothing in the folder is touched and the reason is reported. `--dry-run` prints the chosen release and proposed mapping after the lookups, without writing.

The MusicBrainz enrichment makes one release lookup, or one recording lookup if no release was selected. If enrichment fails, the selected candidate can still be written with a warning. `tag` and `autotag` require `fpcalc` and an `ACOUSTID_API_KEY`; when the variable is unset, the key is read with `vault get ACOUSTID_API_KEY`. MusicBrainz needs no key.

The implementation is split across [metadata.py](../src/tool/music/metadata.py), [tags.py](../src/tool/music/tags.py), [acoustid.py](../src/tool/music/api/acoustid.py), and [musicbrainz.py](../src/tool/music/api/musicbrainz.py).

Current library state, remaining queue composition, and the next techniques under consideration are in [music-tagging-handoff.md](music-tagging-handoff.md).
