import pytest

from tool.music.tags import _WRITABLE_TYPES, TAG_FIELDS, _write_tags_direct, read_tags, write_tags


class TestReadTags:
    def test_flac_empty(self, flac_file):
        tags = read_tags(flac_file)
        assert isinstance(tags, dict)
        # Fresh FLAC has no tags
        for key in TAG_FIELDS:
            assert key not in tags

    def test_mp3_empty(self, mp3_file):
        tags = read_tags(mp3_file)
        assert isinstance(tags, dict)

    def test_m4a_empty(self, m4a_file):
        tags = read_tags(m4a_file)
        assert isinstance(tags, dict)

    def test_nonexistent_file(self):
        tags = read_tags("/nonexistent/path.ogg")
        assert tags == {}

    def test_returns_str_values(self, flac_file):
        # After writing tags, values should be strings
        write_tags(flac_file, {"title": "Test Title", "artist": "Test Artist"})
        tags = read_tags(flac_file)
        assert tags["title"] == "Test Title"
        assert tags["artist"] == "Test Artist"


class TestWriteTagsSuccess:
    def test_write_flac(self, flac_file):
        path = write_tags(flac_file, {"title": "My Title", "artist": "My Artist"})
        assert path == flac_file
        tags = read_tags(flac_file)
        assert tags["title"] == "My Title"
        assert tags["artist"] == "My Artist"

    def test_write_mp3(self, mp3_file):
        write_tags(mp3_file, {"album": "Test Album", "date": "2024"})
        tags = read_tags(mp3_file)
        assert tags["album"] == "Test Album"
        assert tags["date"] == "2024"

    def test_write_m4a(self, m4a_file):
        write_tags(m4a_file, {"title": "AAC Track", "genre": "Electronic"})
        tags = read_tags(m4a_file)
        assert tags["title"] == "AAC Track"
        assert tags["genre"] == "Electronic"

    def test_write_all_fields(self, flac_file):
        metadata = {
            "title": "Title",
            "artist": "Artist",
            "album": "Album",
            "date": "2024",
            "tracknumber": "5",
            "albumartist": "Various",
            "genre": "Rock",
        }
        write_tags(flac_file, metadata)
        tags = read_tags(flac_file)
        for key, val in metadata.items():
            assert tags[key] == val

    def test_write_overwrites_existing(self, flac_file):
        write_tags(flac_file, {"title": "First"})
        write_tags(flac_file, {"title": "Second"})
        tags = read_tags(flac_file)
        assert tags["title"] == "Second"

    def test_write_partial_fields_does_not_clear_others(self, flac_file):
        write_tags(flac_file, {"title": "T", "artist": "A", "album": "B"})
        write_tags(flac_file, {"title": "New Title"})
        tags = read_tags(flac_file)
        assert tags["title"] == "New Title"
        assert tags["artist"] == "A"
        assert tags["album"] == "B"


class TestWriteTagsErrors:
    def test_wav_unsupported(self, wav_file):
        with pytest.raises(SystemExit) as exc:
            write_tags(wav_file, {"title": "Test"})
        assert exc.value.code == 1

    def test_nonexistent_file(self):
        # mutagen raises MutagenError for nonexistent files before we can check
        # writability — we treat this as an error (exit 1) too.
        from mutagen import MutagenError

        with pytest.raises((SystemExit, MutagenError)):
            write_tags("/nonexistent/file.xyz", {"title": "Test"})

    def test_wav_error_message(self, wav_file, capsys):
        with pytest.raises(SystemExit):
            write_tags(wav_file, {"title": "Test"})
        captured = capsys.readouterr()
        assert "cannot write tags" in captured.err.lower()
        assert "transcode" in captured.err.lower()


class TestWriteTagsDirect:
    def test_unsupported_format_errors(self, wav_file):
        # WAV is not in _WRITABLE_TYPES, so it should exit
        with pytest.raises(SystemExit) as exc:
            _write_tags_direct(wav_file, {"title": "Test"})
        assert exc.value.code == 1

    def test_writable_types_are_comprehensive(self):
        # Verify that types handled by _write_tags_direct match _WRITABLE_TYPES
        from mutagen.flac import FLAC as M_FLAC
        from mutagen.mp3 import MP3 as M_MP3
        from mutagen.mp4 import MP4 as M_MP4
        from mutagen.oggflac import OggFLAC as M_OggFLAC
        from mutagen.oggopus import OggOpus as M_OggOpus
        from mutagen.oggvorbis import OggVorbis as M_OggVorbis

        expected = {M_FLAC, M_OggFLAC, M_OggVorbis, M_OggOpus, M_MP3, M_MP4}
        assert set(_WRITABLE_TYPES) == expected
