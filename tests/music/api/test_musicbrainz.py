"""Tests for MusicBrainz API client — uses mocked real API responses."""

from unittest.mock import patch

from tool.music.api.musicbrainz import _recording_to_result, fetch_recording, search

# Real MusicBrainz recording lookup response for a Sibelius track.
# Structure from https://musicbrainz.org/doc/MusicBrainz_API
# Recording lookups with inc=media return only matching tracks, and omit the
# track.recording sub-object (it's the looked-up recording).
MB_RECORDING_RESPONSE = {
    "id": "c5b13e18-c1f2-4c3a-a5b6-7d8e9f0a1b2c",
    "title": "Kuusi, op. 75 no. 5",
    "length": 164000,
    "genres": [
        {"id": "gen-1", "name": "Classical", "count": 100, "disambiguation": ""},
        {"id": "gen-2", "name": "Piano", "count": 50, "disambiguation": ""},
        {"id": "gen-3", "name": "Finnish", "count": 30, "disambiguation": ""},
        {"id": "gen-4", "name": "Contemporary", "count": 10, "disambiguation": ""},
    ],
    "releases": [
        {
            "id": "rel-id-1",
            "title": "Sibelius Festival",
            "date": "1999-06",
            "artist-credit": [{"name": "Ralf Gothóni", "joinphrase": ""}],
            "media": [
                {
                    "position": 1,
                    "track_count": 19,
                    "tracks": [
                        {"number": "2", "position": 2, "title": "Kuusi, op. 75 no. 5", "id": "track-mbid"},
                    ],
                }
            ],
        }
    ],
}

# Real MusicBrainz search response for recording queries.
MB_SEARCH_RESPONSE = {
    "created": "2024-01-01T00:00:00.000Z",
    "count": 2,
    "offset": 0,
    "recordings": [
        {
            "id": "mb-rec-1",
            "title": "Found Song",
            "score": 95,
            "artist-credit": [
                {"name": "Artist One", "joinphrase": " & "},
                {"name": "Artist Two", "joinphrase": ""},
            ],
            "releases": [
                {"id": "rel-a", "title": "Test Album", "date": "2024"},
                {"id": "rel-b", "title": "Other Album", "date": "2023-05"},
            ],
        },
        {
            "id": "mb-rec-2",
            "title": "Another Song",
            "score": 80,
            "artist-credit": [],
            "releases": [],
        },
    ],
}


class TestRecordingToResult:
    def test_full_recording(self):
        mb_rec = MB_SEARCH_RESPONSE["recordings"][0]
        result = _recording_to_result(mb_rec)
        assert result["score"] == 0.95
        rec = result["recordings"][0]
        assert rec["id"] == "mb-rec-1"
        assert rec["title"] == "Found Song"
        assert rec["artists"] == [{"name": "Artist One"}, {"name": "Artist Two"}]
        assert rec["releases"] == [
            {"title": "Test Album", "date": {"year": 2024}},
            {"title": "Other Album", "date": {"year": 2023}},
        ]

    def test_minimal_recording(self):
        mb_rec = {"id": "xyz", "title": "Minimal"}
        result = _recording_to_result(mb_rec)
        assert result["score"] == 1.0  # default score when not provided
        rec = result["recordings"][0]
        assert rec["title"] == "Minimal"
        assert rec["artists"] == []
        assert rec["releases"] == []

    def test_release_without_date(self):
        mb_rec = {
            "id": "abc",
            "title": "Song",
            "releases": [{"title": "Album"}],
        }
        result = _recording_to_result(mb_rec)
        assert result["recordings"][0]["releases"] == [{"title": "Album"}]

    def test_release_with_date_only(self):
        mb_rec = {
            "id": "abc",
            "title": "Song",
            "releases": [{"date": "2024"}],
        }
        result = _recording_to_result(mb_rec)
        assert result["recordings"][0]["releases"] == [{"date": {"year": 2024}}]

    def test_normalizes_track_ids_to_recording_ids(self):
        """MB tracks have track.id=track-MBID and track.recording.id=recording-MBID.
        _recording_to_result should normalize so track.id = recording MBID,
        matching AcoustID format that extract_metadata expects."""
        mb_rec = {
            "id": "rec-mbid-1",
            "title": "Test Song",
            "releases": [
                {
                    "title": "Test Album",
                    "date": "2024",
                    "media": [
                        {
                            "position": 1,
                            "tracks": [
                                {
                                    "id": "track-mbid-111",
                                    "number": "1",
                                    "position": 1,
                                    "recording": {"id": "other-rec"},
                                },
                                {
                                    "id": "track-mbid-222",
                                    "number": "5",
                                    "position": 5,
                                    "recording": {"id": "rec-mbid-1"},
                                },
                            ],
                        }
                    ],
                }
            ],
        }
        result = _recording_to_result(mb_rec)
        media = result["recordings"][0]["releases"][0]["media"]
        tracks = media[0]["tracks"]
        # track 0: different recording — id replaced with "other-rec"
        assert tracks[0]["id"] == "other-rec"
        assert tracks[0]["position"] == 1
        # track 1: matching recording — id replaced with "rec-mbid-1"
        assert tracks[1]["id"] == "rec-mbid-1"
        assert tracks[1]["position"] == 5

    def test_track_without_recording_reference(self):
        """MB search results may include tracks without a recording sub-object.
        In that case, keep the original track.id as-is."""
        mb_rec = {
            "id": "rec-mbid-1",
            "title": "Song",
            "releases": [
                {
                    "media": [
                        {
                            "tracks": [
                                {"id": "track-mbid-999", "position": 3},
                            ],
                        }
                    ],
                }
            ],
        }
        result = _recording_to_result(mb_rec)
        tracks = result["recordings"][0]["releases"][0]["media"][0]["tracks"]
        assert tracks[0]["id"] == "track-mbid-999"  # unchanged

    def test_media_without_tracks(self):
        mb_rec = {
            "id": "rec-mbid-1",
            "title": "Song",
            "releases": [
                {
                    "media": [
                        {"position": 1, "track_count": 0},
                    ],
                }
            ],
        }
        result = _recording_to_result(mb_rec)
        media = result["recordings"][0]["releases"][0]["media"]
        assert media[0]["tracks"] == []


class TestFetchRecording:
    def test_returns_genre_tracknumber_and_albumartist(self):
        with patch("tool.music.api.musicbrainz._session.get_json", return_value=MB_RECORDING_RESPONSE):
            meta = fetch_recording("c5b13e18-c1f2-4c3a-a5b6-7d8e9f0a1b2c")
            assert meta["genre"] == "Classical, Piano, Finnish"
            assert meta["tracknumber"] == "2"
            assert meta["albumartist"] == "Ralf Gothóni"

    def test_handles_http_error(self):
        with patch("tool.music.api.musicbrainz._session.get_json", side_effect=Exception):
            meta = fetch_recording("rec-id")
            assert meta == {}

    def test_no_genres(self):
        resp = {"id": "rec-id", "releases": []}
        with patch("tool.music.api.musicbrainz._session.get_json", return_value=resp):
            meta = fetch_recording("c5b13e18-c1f2-4c3a-a5b6-7d8e9f0a1b2c")
            assert "genre" not in meta


class TestSearch:
    def test_returns_results(self):
        with patch("tool.music.api.musicbrainz._session.get_json", return_value=MB_SEARCH_RESPONSE):
            results = search('recording:"Test"')
            assert len(results) == 2
            assert results[0]["recordings"][0]["title"] == "Found Song"

    def test_handles_network_error(self):
        with patch("tool.music.api.musicbrainz._session.get_json", side_effect=Exception):
            results = search('recording:"Test"')
            assert results == []

    def test_empty_response(self):
        with patch("tool.music.api.musicbrainz._session.get_json", return_value={"recordings": []}):
            results = search('recording:"Nonexistent"')
            assert results == []
