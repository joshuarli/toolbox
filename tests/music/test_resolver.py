from unittest.mock import MagicMock

import pytest

from tool.music.resolver import (
    _artist_from_credit,
    _extract_distinctive_words,
    _normalize,
    _score,
    resolve_track,
    resolve_tracks,
)


class TestNormalize:
    def test_lowercases(self):
        assert _normalize("HELLO World") == "hello world"

    def test_removes_punctuation(self):
        assert _normalize("Hello, World!") == "hello world"

    def test_collapses_whitespace(self):
        assert _normalize("a   b\t\tc") == "a b c"

    def test_strips(self):
        assert _normalize("  x  ") == "x"


class TestArtistFromCredit:
    def test_single_artist(self):
        credit = [{"name": "Artist Name", "joinphrase": ""}]
        assert _artist_from_credit(credit) == "Artist Name"

    def test_multiple_artists(self):
        credit = [
            {"name": "First", "joinphrase": ", "},
            {"name": "Second", "joinphrase": ""},
        ]
        assert _artist_from_credit(credit) == "First, Second"

    def test_empty(self):
        assert _artist_from_credit([]) == ""

    def test_missing_name(self):
        assert _artist_from_credit([{"joinphrase": ""}]) == ""

    def test_non_dict_entries_ignored(self):
        credit = [{"name": "Real", "joinphrase": ""}, "not_a_dict"]
        assert _artist_from_credit(credit) == "Real"


class TestExtractDistinctiveWords:
    def test_simple_title(self):
        result = _extract_distinctive_words("Bohemian Rhapsody", "Queen")
        assert "Bohemian Rhapsody" in result

    def test_classical_with_colon(self):
        result = _extract_distinctive_words("Symphony No. 5: Allegro con brio", "Beethoven")
        assert "Allegro" in result
        assert "brio" in result

    def test_multi_word_quoted(self):
        result = _extract_distinctive_words("Dark Side of the Moon", "Pink Floyd")
        assert '"Dark Side of the Moon"' in result
        assert '"Pink Floyd"' in result

    def test_filters_common_words(self):
        result = _extract_distinctive_words("Title: for the and with", "Artist")
        # "for", "the", "and", "with" should be filtered from movement
        assert "for" not in result.split()


class TestScore:
    def test_perfect_title_match(self):
        result = {"title": "Bohemian Rhapsody", "artist-credit": [{"name": "Queen"}], "length": 354000}
        score = _score(result, "Bohemian Rhapsody", "Queen", 354000)
        assert score == pytest.approx(85.0)

    def test_title_fuzzy_match(self):
        result = {"title": "Bohemian Rhapsody", "artist-credit": [{"name": "Queen"}], "length": 354000}
        score = _score(result, "Bohemian Rhapsody (Remastered)", "Queen", 354000)
        assert score >= 50

    def test_artist_individual_name_match(self):
        result = {"title": "Song", "artist-credit": [{"name": "First"}, {"name": "Second"}]}
        score = _score(result, "Song", "First, Other", 100000)
        # One name matches
        assert score >= 10

    def test_duration_close_match(self):
        result = {"title": "Song", "artist-credit": [{"name": "Artist"}], "length": 100000}
        score = _score(result, "Song", "Artist", 101000)
        assert score >= 55  # title (60) + duration (10) = 70

    def test_no_match(self):
        result = {"title": "Something Else", "artist-credit": [{"name": "Other"}], "length": 999999}
        score = _score(result, "Completely Different", "Nope", 1)
        assert score < 10

    def test_missing_length(self):
        result = {"title": "Song", "artist-credit": [{"name": "Artist"}]}
        score = _score(result, "Song", "Artist", 100000)
        assert score >= 60  # title match only


class TestResolveTrack:
    def test_resolve_finds_best_match(self):
        session = MagicMock()
        session.get_json.return_value = {
            "recordings": [
                {"id": "1", "title": "Foo", "artist-credit": [{"name": "Bar"}], "length": 100},
                {"id": "2", "title": "Exact Match", "artist-credit": [{"name": "Artist"}], "length": 100000},
            ]
        }
        result = resolve_track(session, "Exact Match", "Artist", 100000)
        assert result is not None
        assert result["id"] == "2"
        assert result["title"] == "Exact Match"

    def test_resolve_no_recordings(self):
        session = MagicMock()
        session.get_json.return_value = {"recordings": []}
        result = resolve_track(session, "Title", "Artist", 100)
        assert result is None

    def test_resolve_low_score(self):
        session = MagicMock()
        session.get_json.return_value = {
            "recordings": [{"id": "1", "title": "Something Else", "artist-credit": [{"name": "Other"}], "length": 999}]
        }
        result = resolve_track(session, "Title", "Artist", 100)
        assert result is None

    def test_resolve_http_error(self):
        session = MagicMock()
        session.get_json.side_effect = RuntimeError("network error")
        result = resolve_track(session, "Title", "Artist", 100)
        assert result is None


class TestResolveTracks:
    def test_resolves_all_tracks(self):
        session = MagicMock()
        session.get_json.return_value = {
            "recordings": [{"id": "mb-1", "title": "Song", "artist-credit": [{"name": "Artist"}], "length": 100000}]
        }
        tracks = [
            {"name": "Song", "artists": [{"name": "Artist"}], "duration_ms": 100000, "id": "sp-1"},
        ]
        resolved, unresolved = resolve_tracks(session, tracks)
        assert len(resolved) == 1
        assert len(unresolved) == 0
        assert resolved[0]["spotify_id"] == "sp-1"
        assert resolved[0]["mb_recording"]["id"] == "mb-1"

    def test_unresolved_tracks(self, capsys):
        session = MagicMock()
        session.get_json.return_value = {"recordings": []}
        tracks = [
            {"name": "Unknown", "artists": [{"name": "?"}], "duration_ms": 0, "id": "sp-x"},
        ]
        resolved, unresolved = resolve_tracks(session, tracks)
        assert len(resolved) == 0
        assert len(unresolved) == 1
        assert unresolved[0]["spotify_id"] == "sp-x"

    def test_mixed_results(self):
        session = MagicMock()

        def side_effect(url, params=None):
            query = params.get("query", "")
            if "Good" in query:
                return {
                    "recordings": [
                        {"id": "mb-good", "title": "Good Song", "artist-credit": [{"name": "Artist"}], "length": 100000}
                    ]
                }
            return {"recordings": []}

        session.get_json.side_effect = side_effect
        tracks = [
            {"name": "Good Song", "artists": [{"name": "Artist"}], "duration_ms": 100000, "id": "sp-1"},
            {"name": "Bad Song", "artists": [{"name": "?"}], "duration_ms": 0, "id": "sp-2"},
        ]
        resolved, unresolved = resolve_tracks(session, tracks)
        assert len(resolved) == 1
        assert len(unresolved) == 1
