"""Artwork I/O and batch command regression coverage; no real network calls."""

import argparse
import base64
from pathlib import Path
from unittest.mock import patch

import pytest
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, PictureType
from mutagen.mp4 import MP4

from tool.music import art
from tool.music.api.coverart import Cover, CoverArtError
from tool.music.artwork import embed_art, has_embedded_art
from tool.music.metadata import Metadata
from tool.music.tags import read_tags, write_tags

from .conftest import needs_libvorbis

_JPEG = b"\xff\xd8\xffJPEG-front-cover"
_PNG = b"\x89PNG\r\n\x1a\nPNG-front-cover"
_RELEASE = "490a58ac-6f2d-482e-8915-b703e229794b"
_GROUP = "4cda8183-371b-4189-bcb1-265bd0ff4b5f"
_FORMATS = ["flac_file", "mp3_file", "m4a_file", "opus_file", pytest.param("ogg_file", marks=needs_libvorbis)]


@pytest.mark.parametrize("fixture_name", _FORMATS)
def test_embed_and_skip_existing(fixture_name, request):
    path = request.getfixturevalue(fixture_name)
    write_tags(path, Metadata(title="Keep title", musicbrainz_albumid=_RELEASE))
    assert not has_embedded_art(path)
    assert embed_art(path, _JPEG, "image/jpeg") is True
    assert has_embedded_art(path)
    assert embed_art(path, _PNG, "image/png") is False
    assert read_tags(path).title == "Keep title"
    assert embed_art(path, _PNG, "image/png", force=True) is True
    assert has_embedded_art(path)
    assert read_tags(path).musicbrainz_albumid == _RELEASE


@pytest.mark.parametrize("fixture_name", _FORMATS)
def test_embed_round_trip_data(fixture_name, request):
    path = request.getfixturevalue(fixture_name)
    embed_art(path, _JPEG, "image/jpeg")
    if fixture_name == "flac_file":
        assert FLAC(path).pictures[0].data == _JPEG
    elif fixture_name == "mp3_file":
        assert ID3(path).getall("APIC")[0].data == _JPEG
    elif fixture_name == "m4a_file":
        assert bytes(MP4(path)["covr"][0]) == _JPEG
    else:
        from mutagen import File as MutagenFile
        pic = Picture(base64.b64decode(MutagenFile(path)["metadata_block_picture"][0]))
        assert pic.data == _JPEG


@pytest.mark.parametrize("fixture_name", ["flac_file", "mp3_file", "opus_file"])
def test_force_keeps_back_cover(fixture_name, request):
    path = request.getfixturevalue(fixture_name)
    if fixture_name == "flac_file":
        audio = FLAC(path)
        pic = Picture()
        pic.type = PictureType.COVER_BACK
        pic.mime = "image/png"
        pic.data = _PNG
        audio.add_picture(pic)
        audio.save()
    elif fixture_name == "mp3_file":
        tags = ID3(path)
        tags.add(APIC(encoding=3, mime="image/png", type=PictureType.COVER_BACK, desc="back", data=_PNG))
        tags.save(path)
    else:
        from mutagen.oggopus import OggOpus
        audio = OggOpus(path)
        pic = Picture()
        pic.type = PictureType.COVER_BACK
        pic.mime = "image/png"
        pic.data = _PNG
        audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode()]
        audio.save()
    assert embed_art(path, _JPEG, "image/jpeg") is False
    assert embed_art(path, _JPEG, "image/jpeg", force=True)
    if fixture_name == "flac_file":
        pictures = FLAC(path).pictures
    elif fixture_name == "mp3_file":
        pictures = ID3(path).getall("APIC")
    else:
        from mutagen.oggopus import OggOpus
        pictures = [Picture(base64.b64decode(s)) for s in OggOpus(path)["metadata_block_picture"]]
    assert {p.type: p.data for p in pictures} == {3: _JPEG, 4: _PNG}


def test_invalid_file_and_mime(wav_file):
    with pytest.raises(ValueError, match="cannot embed"):
        embed_art(wav_file, _JPEG, "image/jpeg")
    with pytest.raises(ValueError, match="unsupported image"):
        embed_art(wav_file, b"bad", "text/html")


def _args(path, **kwargs):
    defaults = {
        "paths": [str(path)], "sidecar": False, "embed": False, "dry_run": False,
        "force": False, "size": 1200, "no_group_fallback": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _album(path):
    write_tags(str(path), Metadata(title="Song", artist="Artist", album="Album", musicbrainz_albumid=_RELEASE, musicbrainz_releasegroupid=_GROUP))


def test_cli_embed_fetches_once_per_album(tmp_path, flac_file):
    first = Path(flac_file)
    _album(first)
    second = first.with_name("other.flac")
    second.write_bytes(first.read_bytes())
    with patch("tool.music.art.fetch_cover", return_value=Cover(_JPEG, "image/jpeg", "jpg", "release")) as fetch:
        art.run(_args(first.parent))
    assert fetch.call_count == 1
    assert has_embedded_art(str(first))
    assert has_embedded_art(str(second))


def test_cli_existing_art_does_not_fetch(flac_file):
    _album(Path(flac_file))
    embed_art(flac_file, _JPEG, "image/jpeg")
    with patch("tool.music.art.fetch_cover") as fetch:
        art.run(_args(flac_file))
    fetch.assert_not_called()


def test_dry_run_never_fetches_or_writes(flac_file):
    _album(Path(flac_file))
    with patch("tool.music.art.fetch_cover") as fetch:
        art.run(_args(flac_file, dry_run=True))
    fetch.assert_not_called()
    assert not has_embedded_art(flac_file)


def test_sidecar_dedupes_directory(tmp_path, flac_file):
    first = Path(flac_file)
    _album(first)
    second = first.with_name("second.flac")
    second.write_bytes(first.read_bytes())
    with patch("tool.music.art.fetch_cover", return_value=Cover(_JPEG, "image/jpeg", "jpg", "release")) as fetch:
        art.run(_args(first.parent, sidecar=True))
    assert fetch.call_count == 1
    assert (first.parent / "cover.jpg").read_bytes() == _JPEG
    assert not has_embedded_art(str(first))
    with patch("tool.music.art.fetch_cover") as fetch:
        art.run(_args(first.parent, sidecar=True))
    fetch.assert_not_called()


def test_sidecar_rejects_multiple_releases(flac_file):
    first = Path(flac_file)
    _album(first)
    second = first.with_name("second.flac")
    second.write_bytes(first.read_bytes())
    write_tags(str(second), Metadata(musicbrainz_albumid="82185c30-f97f-4b1b-8c70-a0bd3d077649"))
    with patch("tool.music.art.fetch_cover") as fetch:
        art.run(_args(first.parent, sidecar=True))
    fetch.assert_not_called()
    assert not (first.parent / "cover.jpg").exists()


def test_cli_missing_cover_is_nonfatal(flac_file):
    _album(Path(flac_file))
    with patch("tool.music.art.fetch_cover", return_value=None):
        art.run(_args(flac_file))
    assert not has_embedded_art(flac_file)


def test_cli_invalid_mbid_is_skipped(flac_file, capsys):
    write_tags(flac_file, Metadata(title="Song", album="Album", musicbrainz_albumid="197524302"))
    with patch("tool.music.api.coverart._get") as fetch:
        art.run(_args(flac_file))
    fetch.assert_not_called()
    assert "invalid MusicBrainz ID: 197524302" in capsys.readouterr().out
    assert not has_embedded_art(flac_file)


def test_cli_network_errors_exit_nonzero(flac_file):
    _album(Path(flac_file))
    with patch("tool.music.art.fetch_cover", side_effect=CoverArtError("HTTP 503")):
        with pytest.raises(SystemExit) as ex:
            art.run(_args(flac_file))
    assert ex.value.code == 1
