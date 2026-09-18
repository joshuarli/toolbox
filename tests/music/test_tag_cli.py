from unittest.mock import patch

import pytest

from tool.music.tag import (
    _fallback_search,
    _fallback_select,
    _normalize_filename,
    _print_tags,
    format_diff,
    format_match_line,
)


class TestFormatMatchLine:
    def test_full_line(self):
        result = {
            "score": 0.95,
            "recordings": [
                {
                    "title": "The Song",
                    "artists": [{"name": "The Artist"}],
                    "releases": [{"title": "The Album", "date": {"year": 2023}}],
                }
            ],
        }
        line = format_match_line(1, result)
        assert "95%" in line
        assert "The Song" in line
        assert "The Artist" in line
        assert "The Album" in line
        assert "2023" in line

    def test_minimal(self):
        result = {"score": 0.5, "recordings": [{"title": "Song"}]}
        line = format_match_line(1, result)
        assert "50%" in line
        assert "Song" in line
        assert "?" in line  # no artist


class TestFormatDiff:
    def test_added_field(self):
        current = {}
        new = {"title": "New Title"}
        lines = format_diff(current, new)
        assert len(lines) == 2
        assert "(none)" in lines[0]
        assert "+" in lines[1]

    def test_changed_field(self):
        current = {"title": "Old"}
        new = {"title": "New"}
        lines = format_diff(current, new)
        assert len(lines) == 2
        assert "Old" in lines[0]
        assert "→" in lines[1]

    def test_no_changes(self):
        current = {"title": "Same"}
        new = {"title": "Same"}
        lines = format_diff(current, new)
        assert len(lines) == 0

    def test_mixed(self):
        current = {"title": "Old Title", "artist": "Same Artist"}
        new = {"title": "New Title", "artist": "Same Artist", "album": "New Album"}
        lines = format_diff(current, new)
        assert len(lines) == 4
        # title changed (2 lines), album added (2 lines), artist unchanged


class TestFallbackSelect:
    def test_valid_selection(self):
        results = [{"score": 0.9, "recordings": [{"title": "Song"}]}]
        with patch("builtins.input", return_value="1"):
            idx = _fallback_select(results)
            assert idx == 0

    def test_quit(self):
        results = [{"score": 0.9, "recordings": [{"title": "Song"}]}]
        with patch("builtins.input", return_value="q"), pytest.raises(SystemExit):
            _fallback_select(results)

    def test_invalid_selection_exits(self):
        results = [{"score": 0.9, "recordings": [{"title": "Song"}]}]
        with patch("builtins.input", return_value="not_a_number"), pytest.raises(SystemExit):
            _fallback_select(results)

    def test_out_of_range(self):
        results = [{"score": 0.9, "recordings": [{"title": "Song"}]}]
        with patch("builtins.input", return_value="99"), pytest.raises(SystemExit):
            _fallback_select(results)


class TestPrintTags:
    def test_prints_tags(self, capsys):
        with patch("tool.music.tag.read_tags", return_value={"title": "T", "artist": "A"}):
            _print_tags("test.flac")
        captured = capsys.readouterr()
        assert "title" in captured.out
        assert "artist" in captured.out

    def test_no_tags(self, capsys):
        with patch("tool.music.tag.read_tags", return_value={}):
            _print_tags("test.flac")
        captured = capsys.readouterr()
        assert "no tags" in captured.out


class TestNormalizeFilename:
    def test_removes_youtube_id(self):
        result = _normalize_filename(
            "/path/to/09 - 5 Pieces, Op. 75, The Trees： No. 5. Kuusi (The Spruce) [visNHcEI2XM].webm"
        )
        assert "[visNHcEI2XM]" not in result
        assert ".webm" not in result

    def test_removes_track_number(self):
        result = _normalize_filename("09 - Song Title [abc123def45].mp3")
        assert not result.startswith("09")
        assert "Song Title" in result

    def test_normalizes_fullwidth_punctuation(self):
        # NFKC normalization converts fullwidth characters to ASCII
        result = _normalize_filename("Song ＂Title＂： Subtitle.flac")
        assert "＂" not in result  # fullwidth quotation mark
        assert "：" not in result  # fullwidth colon

    def test_collapses_whitespace(self):
        result = _normalize_filename("01 -   Multiple   Spaces  .mp3")
        assert "  " not in result

    def test_simple_filename(self):
        result = _normalize_filename("Bohemian Rhapsody.flac")
        assert result == "Bohemian Rhapsody"

    def test_filename_without_youtube_id(self):
        result = _normalize_filename("05 - My Song.mp3")
        assert result == "My Song"


class TestFallbackSearch:
    def test_metadata_search_succeeds(self):
        """When file has metadata, searches MusicBrainz with recording + artist."""
        mb_response = {"recordings": [{"id": "mb-1", "title": "Found Song", "score": 95}]}
        with patch("tool.music.api.musicbrainz._session.get_json", return_value=mb_response):
            tags = {
                "title": "Found Song",
                "artist": "Test Artist",
            }
            results = _fallback_search("/some/file.mp3", tags)
            assert len(results) == 1
            assert results[0]["recordings"][0]["title"] == "Found Song"

    def test_falls_back_to_filename(self):
        """When metadata search returns nothing, tries normalized filename."""
        call_count = [0]

        def side_effect(url, **kwargs):
            call_count[0] += 1
            if call_count[0] == 2:
                return {"recordings": [{"id": "mb-2", "title": "Filename Song", "score": 90}]}
            return {"recordings": []}

        with patch("tool.music.api.musicbrainz._session.get_json", side_effect=side_effect):
            tags = {
                "title": "Some Title",
                "artist": "Some Artist",
            }
            results = _fallback_search("/path/01 - Actual Song Name.mp3", tags)
            assert len(results) == 1
            assert results[0]["recordings"][0]["title"] == "Filename Song"

    def test_no_metadata_and_bad_filename(self):
        """When nothing works, returns empty list."""
        with patch("tool.music.api.musicbrainz._session.get_json", return_value={"recordings": []}):
            results = _fallback_search("/path/01 - .mp3", {})
            assert results == []

    def test_empty_tags_no_filename_match(self):
        """No metadata in file, filename normalizes to empty/nothing useful."""
        with patch("tool.music.api.musicbrainz._session.get_json", return_value={"recordings": []}):
            results = _fallback_search("/path/01 - .mp3", {"title": "Unknown"})
            assert results == []

    def test_fallback_search_preserves_recording_id(self):
        """MB search results include the recording id so fetch_recording
        can later look up track numbers, genre, and album artist."""
        mb_response = {
            "recordings": [
                {
                    "id": "rec-mb-1",
                    "title": "Song",
                    "score": 95,
                    "releases": [{"title": "Album", "date": "2024"}],
                }
            ],
        }
        with patch("tool.music.api.musicbrainz._session.get_json", return_value=mb_response):
            results = _fallback_search("/some/file.mp3", {"title": "Song"})
            assert len(results) == 1
            assert results[0]["recordings"][0]["id"] == "rec-mb-1"
