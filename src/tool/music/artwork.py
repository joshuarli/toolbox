"""Embedded cover-art I/O for the containers supported by music tagging."""

import base64

from mutagen import File as MutagenFile
from mutagen import MutagenError
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, PictureType
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggflac import OggFLAC
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

_OGG_TYPES = (OggFLAC, OggOpus, OggVorbis)
_ART_TYPES = (FLAC, MP3, MP4, *_OGG_TYPES)


def _has_art(audio) -> bool:
    if isinstance(audio, FLAC):
        return bool(audio.pictures)
    if isinstance(audio, MP3):
        return bool(audio.tags and audio.tags.getall("APIC"))
    if isinstance(audio, MP4):
        return bool(audio.tags and audio.tags.get("covr"))
    if isinstance(audio, _OGG_TYPES):
        return bool(audio.tags and (audio.tags.get("metadata_block_picture") or audio.tags.get("coverart")))
    return False


def has_embedded_art(path: str) -> bool:
    """Detect FLAC pictures, ID3 APIC, MP4 covr and Ogg picture comments."""
    try:
        return _has_art(MutagenFile(path))
    except (MutagenError, OSError, ValueError):
        return False


def _picture(data: bytes, mime: str) -> Picture:
    pic = Picture()
    pic.type = PictureType.COVER_FRONT
    pic.mime = mime
    pic.data = data
    return pic


def embed_art(path: str, data: bytes, mime: str, *, force: bool = False) -> bool:
    """Embed front artwork without touching other tags or non-front images.

    Returns False if artwork is already present and force is not set.
    """
    if mime not in {"image/jpeg", "image/png"}:
        raise ValueError(f"unsupported image type: {mime}")
    audio = MutagenFile(path)
    if not isinstance(audio, _ART_TYPES):
        raise ValueError(f"cannot embed artwork in {path}; use music transcode first")
    if _has_art(audio) and not force:
        return False

    if isinstance(audio, FLAC):
        previous = [p for p in audio.pictures if p.type != PictureType.COVER_FRONT]
        audio.clear_pictures()
        for p in previous:
            audio.add_picture(p)
        audio.add_picture(_picture(data, mime))
    elif isinstance(audio, MP3):
        if audio.tags is None:
            audio.add_tags()
        for frame in audio.tags.getall("APIC"):
            if frame.type == PictureType.COVER_FRONT:
                audio.tags.delall(frame.HashKey)
        audio.tags.add(APIC(encoding=3, mime=mime, type=PictureType.COVER_FRONT, desc="Cover", data=data))
    elif isinstance(audio, MP4):
        if audio.tags is None:
            audio.add_tags()
        fmt = MP4Cover.FORMAT_JPEG if mime == "image/jpeg" else MP4Cover.FORMAT_PNG
        audio.tags["covr"] = [MP4Cover(data, imageformat=fmt)]
    elif isinstance(audio, _OGG_TYPES):
        if audio.tags is None:
            audio.add_tags()
        retained = []
        for encoded in audio.tags.get("metadata_block_picture", []):
            try:
                pic = Picture(base64.b64decode(encoded, validate=True))
            except (ValueError, TypeError):
                retained.append(encoded)
                continue
            if pic.type != PictureType.COVER_FRONT:
                retained.append(encoded)
        retained.append(base64.b64encode(_picture(data, mime).write()).decode("ascii"))
        audio.tags["metadata_block_picture"] = retained
        if force:
            for key in ("coverart", "coverartmime"):
                if key in audio.tags:
                    del audio.tags[key]
    audio.save()
    return True
