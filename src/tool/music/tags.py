"""Tag reading and writing via mutagen.  Shared by scan and tag — the single
place that touches mutagen for metadata I/O.
"""

import json
import os
import subprocess
import sys

from mutagen import File as MutagenFile
from mutagen.easyid3 import EasyID3
from mutagen.easymp4 import EasyMP4
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggflac import OggFLAC
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

# Fields we care about, in display order
TAG_FIELDS = ("title", "artist", "album", "date", "tracknumber", "albumartist", "genre")

# Mutagen types that support direct tag writing
_WRITABLE_TYPES = (FLAC, OggFLAC, OggVorbis, OggOpus, MP3, MP4)

# Map ffprobe tag names (lowercase) to our canonical field names
_FFPROBE_TAG_MAP = {
    "title": "title",
    "artist": "artist",
    "album": "album",
    "date": "date",
    "track": "tracknumber",
    "tracknumber": "tracknumber",
    "track_number": "tracknumber",
    "album_artist": "albumartist",
    "albumartist": "albumartist",
    "genre": "genre",
}


def _read_tags_ffprobe(filepath: str) -> dict[str, str]:
    """Read tags via ffprobe as a fallback for containers mutagen can't handle."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_entries",
                "format_tags",
                filepath,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return {}
        data = json.loads(result.stdout)
    except Exception:
        return {}

    tags = data.get("format", {}).get("tags", {})
    if not tags:
        return {}

    result: dict[str, str] = {}
    for ff_key, val in tags.items():
        field = _FFPROBE_TAG_MAP.get(ff_key.lower())
        if field and val:
            result[field] = str(val)

    return result


def read_tags(filepath: str) -> dict[str, str]:
    """Read tags from *filepath* using mutagen, with ffprobe fallback.

    Returns a dict mapping field name to string value.  Fields that are
    absent or empty are omitted from the dict.
    """
    try:
        audio = MutagenFile(filepath)
    except Exception:
        return _read_tags_ffprobe(filepath)

    if audio is None:
        return _read_tags_ffprobe(filepath)

    result: dict[str, str] = {}

    if isinstance(audio, (FLAC, OggVorbis, OggOpus)):
        tags = audio.tags
        if tags is None:
            return result
        for key in TAG_FIELDS:
            vals = tags.get(key)
            if vals:
                result[key] = vals[0] if isinstance(vals, list) else str(vals)
    elif isinstance(audio, MP3):
        try:
            easy = EasyID3(filepath)
        except Exception:
            return result
        for key in TAG_FIELDS:
            vals = easy.get(key)
            if vals:
                result[key] = vals[0] if isinstance(vals, list) else str(vals)
    elif isinstance(audio, MP4):
        try:
            easy = EasyMP4(filepath)
        except Exception:
            return result
        for key in TAG_FIELDS:
            vals = easy.get(key)
            if vals:
                result[key] = vals[0] if isinstance(vals, list) else str(vals)

    return result


def write_tags(filepath: str, metadata: dict[str, str]) -> str:
    """Write *metadata* to *filepath* using mutagen.

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


def _write_tags_direct(filepath: str, metadata: dict[str, str]) -> None:
    """Write *metadata* to *filepath* using mutagen.  The file must already be
    in a tag-writable container (FLAC, MP3, MP4, OggVorbis, OggOpus).
    """
    audio = MutagenFile(filepath)
    if audio is None:
        print(f"error: unsupported audio format: {filepath}", file=sys.stderr)
        sys.exit(1)

    if isinstance(audio, (FLAC, OggVorbis, OggOpus)):
        if audio.tags is None:
            audio.add_tags()
        for key in TAG_FIELDS:
            if key in metadata:
                audio.tags[key] = [metadata[key]]
        audio.save()
    elif isinstance(audio, MP3):
        easy = EasyID3(filepath)
        for key in TAG_FIELDS:
            if key in metadata:
                easy[key] = [metadata[key]]
        easy.save()
    elif isinstance(audio, MP4):
        easy = EasyMP4(filepath)
        for key in TAG_FIELDS:
            if key in metadata:
                easy[key] = [metadata[key]]
        easy.save()
    else:
        print(f"error: unsupported audio format: {filepath}", file=sys.stderr)
        sys.exit(1)
