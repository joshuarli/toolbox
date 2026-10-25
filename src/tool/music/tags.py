"""Tag reading and writing via mutagen.  Shared by scan and tag — the single
place that touches mutagen for metadata I/O, and the single authority on how
`Metadata` fields are represented in each container.

Conventions follow MusicBrainz Picard (https://picard-docs.musicbrainz.org/en/appendices/tag_mapping.html).
The tricky one: the *recording* MBID is `MUSICBRAINZ_TRACKID` in Vorbis, `UFID:http://musicbrainz.org`
in ID3 and `MusicBrainz Track Id` in MP4, while the *release track* MBID is `MUSICBRAINZ_RELEASETRACKID` /
`TXXX:MusicBrainz Release Track Id` / `MusicBrainz Release Track Id`.

Writes only touch fields the given `Metadata` actually sets; everything else in the file
(artwork, unrelated frames, other TIPL roles, ...) is left alone.
"""

import json
import os
import re
import subprocess
import sys
from dataclasses import fields
from typing import Any

from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TIPL, TMCL, TPOS, TRCK, TXXX, UFID, Frames, ID3NoHeaderError
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, AtomDataType, MP4FreeForm
from mutagen.oggflac import OggFLAC
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from tool.music.metadata import Metadata

# Mutagen types that support direct tag writing
_WRITABLE_TYPES = (FLAC, OggFLAC, OggVorbis, OggOpus, MP3, MP4)

_FIELDS = tuple(f.name for f in fields(Metadata))

# Map ffprobe tag names (lowercase) to our canonical field names
_FFPROBE_TAG_MAP = {
    "title": "title",
    "artist": "artist",
    "album": "album",
    "date": "date",
    "track": "tracknumber",
    "tracknumber": "tracknumber",
    "track_number": "tracknumber",
    "disc": "discnumber",
    "album_artist": "albumartist",
    "albumartist": "albumartist",
    "genre": "genre",
}

# --- Vorbis comments -------------------------------------------------------
# Key is the upper-cased field name except for the two renamed MusicBrainz IDs.
_VORBIS_KEYS = {
    "musicbrainz_recordingid": "MUSICBRAINZ_TRACKID",
    "musicbrainz_trackid": "MUSICBRAINZ_RELEASETRACKID",
}
_VORBIS_READ_ALIASES = {"totaltracks": "TRACKTOTAL", "totaldiscs": "DISCTOTAL"}

# --- ID3 -------------------------------------------------------------------
_ID3_FRAMES = {
    "title": "TIT2",
    "artist": "TPE1",
    "album": "TALB",
    "albumartist": "TPE2",
    "artistsort": "TSOP",
    "albumartistsort": "TSO2",
    "date": "TDRC",
    "originaldate": "TDOR",
    "genre": "TCON",
    "media": "TMED",
    "label": "TPUB",
    "isrc": "TSRC",
    "composer": "TCOM",
    "lyricist": "TEXT",
    "conductor": "TPE3",
    "remixer": "TPE4",
}
_UFID_OWNER = "http://musicbrainz.org"  # holds the recording MBID
_ID3_PAIRS = {"tracknumber": ("TRCK", TRCK, 0), "totaltracks": ("TRCK", TRCK, 1)} | {
    "discnumber": ("TPOS", TPOS, 0),
    "totaldiscs": ("TPOS", TPOS, 1),
}
_TIPL_ROLES = ("arranger", "producer")  # fields stored as (role, name) pairs in TIPL
_PERFORMER_RE = re.compile(r"^(.*) \(([^()]*)\)$")  # "Name (instrument)" <-> TMCL [instrument, name]

# --- MP4 -------------------------------------------------------------------
_MP4_ATOMS = {
    "title": "\xa9nam",
    "artist": "\xa9ART",
    "album": "\xa9alb",
    "albumartist": "aART",
    "artistsort": "soar",
    "albumartistsort": "soaa",
    "date": "\xa9day",
    "genre": "\xa9gen",
    "composer": "\xa9wrt",
}
_MP4_PAIRS = {
    "tracknumber": ("trkn", 0),
    "totaltracks": ("trkn", 1),
    "discnumber": ("disk", 0),
    "totaldiscs": ("disk", 1),
}
_FREEFORM_PREFIX = "----:com.apple.iTunes:"

# Name used for ID3 `TXXX:<name>` and MP4 `----:com.apple.iTunes:<name>` when the field has no
# standard frame/atom.  Defaults to the upper-cased field name (ARTISTS, BARCODE, LABEL, ...).
_CUSTOM_NAMES = {
    "musicbrainz_recordingid": "MusicBrainz Track Id",
    "musicbrainz_trackid": "MusicBrainz Release Track Id",
    "musicbrainz_albumid": "MusicBrainz Album Id",
    "musicbrainz_releasegroupid": "MusicBrainz Release Group Id",
    "musicbrainz_artistid": "MusicBrainz Artist Id",
    "musicbrainz_albumartistid": "MusicBrainz Album Artist Id",
    "musicbrainz_workid": "MusicBrainz Work Id",
    "releasecountry": "MusicBrainz Album Release Country",
    "releasestatus": "MusicBrainz Album Status",
    "releasetype": "MusicBrainz Album Type",
    "acoustid_id": "Acoustid Id",
    "acoustid_fingerprint": "Acoustid Fingerprint",
}


def _custom_name(name: str) -> str:
    return _CUSTOM_NAMES.get(name, name.upper())


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _read_tags_ffprobe(filepath: str) -> Metadata:
    """Read tags via ffprobe as a fallback for containers mutagen can't handle."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_entries", "format_tags", filepath],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return Metadata()
        data = json.loads(result.stdout)
    except Exception:
        return Metadata()

    values: dict[str, list[str]] = {}
    for ff_key, val in data.get("format", {}).get("tags", {}).items():
        name = _FFPROBE_TAG_MAP.get(ff_key.lower())
        if name is not None and name and val:
            values[name] = [str(val)]
    return Metadata.from_values(values)


def read_tags(filepath: str) -> Metadata:
    """Read tags from *filepath* using mutagen, with ffprobe fallback.

    Absent or empty fields are left unset on the returned `Metadata`.
    """
    try:
        audio = MutagenFile(filepath)
    except Exception:
        return _read_tags_ffprobe(filepath)
    if audio is None:
        return _read_tags_ffprobe(filepath)

    values: dict[str, list[str]] = {}
    if isinstance(audio, (FLAC, OggFLAC, OggVorbis, OggOpus)):
        if audio.tags is not None:
            values = _read_vorbis(audio.tags)
    elif isinstance(audio, MP3):
        try:
            values = _read_id3(ID3(filepath))
        except ID3NoHeaderError:
            pass
    elif isinstance(audio, MP4) and audio.tags is not None:
        values = _read_mp4(audio.tags)
    return Metadata.from_values(values)


def _read_vorbis(tags) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for name in _FIELDS:
        vals = tags.get(_VORBIS_KEYS.get(name, name.upper())) or (
            tags.get(_VORBIS_READ_ALIASES[name]) if name in _VORBIS_READ_ALIASES else None
        )
        if vals:
            values[name] = list(vals)
    return values


def _id3_text(frame: Any) -> list[str]:
    return [str(t) for t in (frame.genres if frame.FrameID == "TCON" else frame.text)]


def _read_id3(id3: ID3) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for name in _FIELDS:
        if name in _ID3_FRAMES:
            frame = id3.get(_ID3_FRAMES[name])
            vals = _id3_text(frame) if frame else []
        elif name == "musicbrainz_recordingid":
            ufid = id3.get(f"UFID:{_UFID_OWNER}")
            vals = [ufid.data.decode("ascii", "replace")] if ufid else []
        elif name in _ID3_PAIRS:
            frame = id3.get(_ID3_PAIRS[name][0])
            parts = str(frame.text[0]).partition("/") if frame and frame.text else ()
            vals = [parts[_ID3_PAIRS[name][2] * 2]] if parts else []
        elif name in _TIPL_ROLES:
            frame = id3.get("TIPL")
            vals = [n for role, n in (frame.people if frame else []) if role.lower() == name]
        elif name == "performer":
            frame = id3.get("TMCL")
            vals = [f"{n} ({role})" if role else n for role, n in (frame.people if frame else [])]
        else:
            frame = id3.get(f"TXXX:{_custom_name(name)}")
            vals = [str(t) for t in frame.text] if frame else []
        if vals:
            values[name] = vals
    return values


def _read_mp4(tags) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for name in _FIELDS:
        if name in _MP4_PAIRS:
            atom, idx = _MP4_PAIRS[name]
            pair = (tags.get(atom) or [(0, 0)])[0]
            vals = [str(pair[idx])] if pair[idx] else []
        elif name in _MP4_ATOMS:
            vals = [str(v) for v in tags.get(_MP4_ATOMS[name]) or []]
        else:
            vals = [bytes(v).decode("utf-8", "replace") for v in tags.get(_FREEFORM_PREFIX + _custom_name(name)) or []]
        if vals:
            values[name] = vals
    return values


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def is_writable(filepath: str) -> bool:
    """Whether *filepath* is in a container `write_tags` can write."""
    try:
        return isinstance(MutagenFile(filepath), _WRITABLE_TYPES)
    except Exception:
        return False


def write_tags(filepath: str, metadata: Metadata) -> str:
    """Write the fields *metadata* sets to *filepath* using mutagen.

    Returns the filepath written to.  Exits with an error if the file
    format does not support tag writing — run 'transcode' first to
    convert it to a writable format.
    """
    audio = MutagenFile(filepath)

    if not isinstance(audio, _WRITABLE_TYPES):
        ext = os.path.splitext(filepath)[1].lower() or "unknown"
        print(f"error: cannot write tags to {ext} files", file=sys.stderr)
        print("  Run 'transcode' first to convert to a writable format.", file=sys.stderr)
        sys.exit(1)

    _write_tags_direct(filepath, metadata)
    return filepath


def _write_tags_direct(filepath: str, metadata: Metadata) -> None:
    """Write *metadata* to *filepath*.  The file must already be in a tag-writable
    container (FLAC, MP3, MP4, OggVorbis, OggOpus).
    """
    audio = MutagenFile(filepath)
    new = metadata.to_values()

    if isinstance(audio, (FLAC, OggFLAC, OggVorbis, OggOpus)):
        if audio.tags is None:
            audio.add_tags()
        for name, vals in new.items():
            audio.tags[_VORBIS_KEYS.get(name, name.upper())] = vals
        audio.save()
    elif isinstance(audio, MP3):
        try:
            id3 = ID3(filepath)
        except ID3NoHeaderError:
            id3 = ID3()
        _write_id3(id3, new)
        id3.save(filepath)
    elif isinstance(audio, MP4):
        if audio.tags is None:
            audio.add_tags()
        _write_mp4(audio.tags, new)
        audio.save()
    else:
        print(f"error: unsupported audio format: {filepath}", file=sys.stderr)
        sys.exit(1)


def _write_id3(id3: ID3, new: dict[str, list[str]]) -> None:
    current = _read_id3(id3)
    for name, vals in new.items():
        if name in _ID3_FRAMES:
            frame_id = _ID3_FRAMES[name]
            id3.setall(frame_id, [Frames[frame_id](encoding=3, text=vals)])
        elif name == "musicbrainz_recordingid":
            id3.setall(f"UFID:{_UFID_OWNER}", [UFID(owner=_UFID_OWNER, data=vals[0].encode("ascii"))])
        elif name in _ID3_PAIRS:
            frame_id, cls, _ = _ID3_PAIRS[name]
            num, total = ((current | new).get(n, [""])[0] for n in _pair_names(frame_id))
            if not num:  # a total alone is not a valid TRCK/TPOS
                continue
            id3.setall(frame_id, [cls(encoding=3, text=[f"{num}/{total}" if total else num])])
        elif name in _TIPL_ROLES:
            kept = [p for p in _people(id3, "TIPL") if p[0].lower() not in new]
            roles = [[r, n] for r in _TIPL_ROLES if r in new for n in new[r]]
            id3.setall("TIPL", [TIPL(encoding=3, people=kept + roles)])
        elif name == "performer":
            people = [[m[2], m[1]] if (m := _PERFORMER_RE.match(v)) else ["", v] for v in vals]
            id3.setall("TMCL", [TMCL(encoding=3, people=people)])
        else:
            desc = _custom_name(name)
            id3.setall(f"TXXX:{desc}", [TXXX(encoding=3, desc=desc, text=vals)])


def _pair_names(frame_id: str) -> list[str]:
    return [n for n, (fid, _, _) in _ID3_PAIRS.items() if fid == frame_id]


def _people(id3: ID3, frame_id: str) -> list[list[str]]:
    frame = id3.get(frame_id)
    return [list(p) for p in frame.people] if frame else []


def _write_mp4(tags, new: dict[str, list[str]]) -> None:
    current = _read_mp4(tags)
    for name, vals in new.items():
        if name in _MP4_PAIRS:
            atom = _MP4_PAIRS[name][0]
            # trkn/disk hold integers only; a non-numeric value cannot be represented and is skipped.
            nums = [(current | new).get(n, [""])[0] for n, (a, _) in _MP4_PAIRS.items() if a == atom]
            if nums[0].isdigit():
                tags[atom] = [(int(nums[0]), int(nums[1]) if nums[1].isdigit() else 0)]
        elif name in _MP4_ATOMS:
            tags[_MP4_ATOMS[name]] = vals
        else:
            tags[_FREEFORM_PREFIX + _custom_name(name)] = [
                MP4FreeForm(v.encode("utf-8"), dataformat=AtomDataType.UTF8) for v in vals
            ]
