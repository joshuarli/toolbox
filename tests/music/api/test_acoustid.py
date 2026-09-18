"""Tests for AcoustID API client — uses mocked real API responses."""

import json
from unittest.mock import MagicMock, patch

import pytest

from tool.music.api.acoustid import extract_metadata, fetch_acoustid_metadata, get_audio_fingerprint

# Real AcoustID v2/lookup response for a classical track (fingerprint redacted).
# Structure from https://acoustid.org/webservice
ACOUSTID_RESPONSE = {
    "status": "ok",
    "results": [
        {
            "score": 0.953,
            "id": "abc123-fingerprint-id",
            "recordings": [
                {
                    "id": "c5b13e18-c1f2-4c3a-a5b6-7d8e9f0a1b2c",
                    "title": "Kuusi, op. 75 no. 5",
                    "duration": 164,
                    "artists": [
                        {"id": "art-id-1", "name": "Ralf Gothóni"},
                    ],
                    "releases": [
                        {
                            "id": "rel-id-1",
                            "title": "Sibelius Festival",
                            "date": {"year": 1999, "month": 6},
                            "country": "FI",
                            "media": [
                                {
                                    "position": 1,
                                    "track_count": 19,
                                    "tracks": [
                                        {
                                            "id": "c5b13e18-c1f2-4c3a-a5b6-7d8e9f0a1b2c",
                                            "position": 5,
                                            "title": "Kuusi, op. 75 no. 5",
                                        },
                                    ],
                                }
                            ],
                        },
                        {
                            "id": "rel-id-2",
                            "title": "Sibelius, J.: Piano Music",
                            "date": {"year": 2014, "month": 9, "day": 24},
                            "country": "GB",
                        },
                    ],
                }
            ],
        },
        {
            "score": 0.781,
            "id": "def456-fingerprint-id",
            "recordings": [
                {
                    "id": "d6c24f29-d2f3-4d4b-b6c7-8e9f0a1b2c3d",
                    "title": '5 Pieces, Op. 75, "The Trees": No. 5. Kuusi (The Spruce)',
                    "duration": 166,
                    "artists": [
                        {"id": "art-id-1", "name": "Ralf Gothóni"},
                    ],
                    "releases": [
                        {
                            "id": "rel-id-2",
                            "title": "Sibelius, J.: Piano Music",
                            "date": {"year": 2014, "month": 9, "day": 24},
                            "country": "GB",
                        }
                    ],
                }
            ],
        },
    ],
}

# Real AcoustID response with no results
ACOUSTID_RESPONSE_EMPTY = {
    "status": "ok",
    "results": [],
}

# fpcalc JSON output
FPCALC_OUTPUT = {"duration": 164.5, "fingerprint": "AQADtImS..."}


class TestGetAudioFingerprint:
    def test_success(self):
        stdout = json.dumps(FPCALC_OUTPUT)
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.stdout = stdout
            mock_run.return_value = mock_result

            duration, fp = get_audio_fingerprint("test.flac")
            assert duration == 164.5
            assert fp == "AQADtImS..."

    def test_fpcalc_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError), pytest.raises(SystemExit):
            get_audio_fingerprint("test.flac")

    def test_fpcalc_error(self):
        import subprocess

        err = subprocess.CalledProcessError(1, "fpcalc")
        err.stderr = "fpcalc error message"
        with patch("subprocess.run", side_effect=err), pytest.raises(SystemExit):
            get_audio_fingerprint("test.flac")

    def test_fpcalc_json_error(self):
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.stdout = "not valid json"
            mock_run.return_value = mock_result
            with pytest.raises(SystemExit):
                get_audio_fingerprint("test.flac")


class TestFetchAcoustidMetadata:
    def test_success(self, monkeypatch):
        monkeypatch.setenv("ACOUSTID_API_KEY", "test-key")
        with patch("tool.music.api.acoustid._session.get_json", return_value=ACOUSTID_RESPONSE):
            results = fetch_acoustid_metadata(200.0, "fp123")
            assert len(results) == 2
            assert results[0]["score"] == 0.953
            assert results[0]["recordings"][0]["title"] == "Kuusi, op. 75 no. 5"

    def test_no_api_key(self, monkeypatch):
        monkeypatch.delenv("ACOUSTID_API_KEY", raising=False)
        with pytest.raises(SystemExit):
            fetch_acoustid_metadata(200.0, "fp123")

    def test_api_error_status(self, monkeypatch):
        monkeypatch.setenv("ACOUSTID_API_KEY", "test-key")
        with patch(
            "tool.music.api.acoustid._session.get_json",
            return_value={"status": "error", "error": {"message": "Bad fingerprint"}},
        ):
            results = fetch_acoustid_metadata(200.0, "fp123")
            assert results is None

    def test_http_error(self, monkeypatch):
        monkeypatch.setenv("ACOUSTID_API_KEY", "test-key")
        with (
            patch("tool.music.api.acoustid._session.get_json", side_effect=Exception("Connection error")),
            pytest.raises(SystemExit),
        ):
            fetch_acoustid_metadata(200.0, "fp123")

    def test_empty_results(self, monkeypatch):
        monkeypatch.setenv("ACOUSTID_API_KEY", "test-key")
        with patch("tool.music.api.acoustid._session.get_json", return_value=ACOUSTID_RESPONSE_EMPTY):
            results = fetch_acoustid_metadata(200.0, "fp123")
            assert results == []


class TestExtractMetadata:
    def test_full_record(self):
        """Parses a complete AcoustID result with releases, media, and tracks."""
        result = ACOUSTID_RESPONSE["results"][0]
        meta = extract_metadata(result)
        assert meta["title"] == "Kuusi, op. 75 no. 5"
        assert meta["artist"] == "Ralf Gothóni"
        assert meta["album"] == "Sibelius Festival"
        assert meta["date"] == "1999"
        assert meta["tracknumber"] == "5"

    def test_track_number_from_mb_fallback_format(self):
        """extract_metadata works with results normalized by _recording_to_result,
        where MB track.recording.id has been remapped to track.id."""
        result = {
            "score": 0.95,
            "recordings": [
                {
                    "id": "rec-mbid-1",
                    "title": "Test Song",
                    "artists": [{"name": "Test Artist"}],
                    "releases": [
                        {
                            "title": "Test Album",
                            "date": {"year": 2024},
                            "media": [
                                {
                                    "tracks": [
                                        {"id": "other-rec", "position": 1},
                                        {"id": "rec-mbid-1", "position": 5},
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        meta = extract_metadata(result)
        assert meta["title"] == "Test Song"
        assert meta["artist"] == "Test Artist"
        assert meta["album"] == "Test Album"
        assert meta["date"] == "2024"
        assert meta["tracknumber"] == "5"

    def test_record_without_tracks(self):
        """Result with releases but no media/track info."""
        result = ACOUSTID_RESPONSE["results"][1]
        meta = extract_metadata(result)
        assert meta["title"] == '5 Pieces, Op. 75, "The Trees": No. 5. Kuusi (The Spruce)'
        assert meta["artist"] == "Ralf Gothóni"
        assert meta["album"] == "Sibelius, J.: Piano Music"
        assert meta["date"] == "2014"
        assert "tracknumber" not in meta

    def test_no_recordings(self):
        assert extract_metadata({}) == {}
        assert extract_metadata({"recordings": []}) == {}

    def test_minimal_record(self):
        result = {"recordings": [{"title": "Just Title"}]}
        meta = extract_metadata(result)
        assert meta == {"title": "Just Title"}

    def test_artist_no_name(self):
        result = {"recordings": [{"artists": [{"role": "composer"}]}]}
        meta = extract_metadata(result)
        assert meta["artist"] == ""
