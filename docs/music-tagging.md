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

The MusicBrainz enrichment makes one release lookup, or one recording lookup if no release was selected. If enrichment fails, the selected candidate can still be written with a warning. `tag` requires `ACOUSTID_API_KEY` and `fpcalc`; MusicBrainz needs no key.

The implementation is split across [metadata.py](../src/tool/music/metadata.py), [tags.py](../src/tool/music/tags.py), [acoustid.py](../src/tool/music/api/acoustid.py), and [musicbrainz.py](../src/tool/music/api/musicbrainz.py).
