"""Tests for AcoustID API client — uses mocked real API responses."""

import json
from unittest.mock import MagicMock, patch

import pytest

from tool.music.api.acoustid import candidates, fetch_acoustid_metadata, get_audio_fingerprint

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
    def test_success(self, monkeypatch, load_fixture):
        monkeypatch.setenv("ACOUSTID_API_KEY", "test-key")
        with patch(
            "tool.music.api.acoustid._session.get_json", return_value=load_fixture("acoustid_corale.json")
        ) as get:
            results = fetch_acoustid_metadata(200.0, "fp123")
        assert len(results) == 2
        assert results[0]["recordings"][0]["title"] == "Corale"
        # asks for release groups and releases/tracks so candidates are release-aware
        assert get.call_args.kwargs["params"]["meta"] == "recordings releasegroups releases tracks"

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
        with patch("tool.music.api.acoustid._session.get_json", return_value={"status": "ok", "results": []}):
            results = fetch_acoustid_metadata(200.0, "fp123")
            assert results == []


def _by_release(cands):
    return {c.meta.musicbrainz_albumid: c for c in cands}


class TestCandidates:
    """Shapes are real AcoustID responses (meta=recordings releasegroups releases tracks)."""

    def test_one_candidate_per_release_of_a_recording(self, load_fixture):
        cands = candidates(load_fixture("acoustid_kalandra.json")["results"])
        assert len(cands) == 5
        assert {c.meta.musicbrainz_recordingid for c in cands} == {"1db8dba2-0d42-43e2-94d7-0a6a8df7d2d7"}
        assert {c.meta.musicbrainz_releasegroupid for c in cands} == {"4cda8183-371b-4189-bcb1-265bd0ff4b5f"}
        assert {c.meta.acoustid_id for c in cands} == {"e8187d3a-71a8-49ef-8f95-2fa590426190"}

    def test_edition_details_distinguish_releases(self, load_fixture):
        by = _by_release(candidates(load_fixture("acoustid_kalandra.json")["results"]))
        cd = by["490a58ac-6f2d-482e-8915-b703e229794b"].meta
        vinyl = by["c0035a48-a3bd-4a66-9a5a-938ffaf67f23"].meta
        assert (cd.media, cd.date, cd.releasecountry) == ("CD", "2020-10-23", "NO")
        assert cd.musicbrainz_trackid == "82185c30-f97f-4b1b-8c70-a0bd3d077649"
        assert (cd.discnumber, cd.totaldiscs, cd.tracknumber, cd.totaltracks) == ("1", "1", "11", "11")
        assert vinyl.media == '12" Vinyl'
        assert vinyl.musicbrainz_trackid == "2aabd1c8-3793-4848-ba05-acf7ddd4b9ce"
        assert (vinyl.discnumber, vinyl.totaldiscs, vinyl.tracknumber, vinyl.totaltracks) == ("2", "2", "3", "3")
        assert vinyl.releasetype == ["album"]

    def test_same_match_under_several_results_keeps_best_score(self, load_fixture):
        results = load_fixture("acoustid_corale.json")["results"]
        assert [r["score"] for r in results] == [0.9947587, 0.9074697]
        cands = candidates(results)
        assert len(cands) == 6  # 6 releases, not 12
        assert {c.score for c in cands} == {0.9947587}
        assert {c.meta.acoustid_id for c in cands} == {"46a154e0-0450-4c2f-9319-b3b3e5a85d4a"}

    def test_partial_dates(self, load_fixture):
        cands = candidates(load_fixture("acoustid_corale.json")["results"])
        year_only = [c.meta for c in cands if c.meta.releasecountry == "IT"]
        assert [m.date for m in year_only] == ["2013"]

    def test_missing_release_country(self, load_fixture):
        by = _by_release(candidates(load_fixture("acoustid_kalandra.json")["results"]))
        assert by["2e58a475-b292-4570-a021-04aa00d29f3e"].meta.releasecountry is None

    def test_releases_without_release_groups(self):
        result = {
            "id": "ac-1",
            "score": 0.9,
            "recordings": [
                {
                    "id": "rec-1",
                    "title": "Song",
                    "artists": [{"id": "a1", "name": "A", "joinphrase": " & "}, {"id": "a2", "name": "B"}],
                    "releases": [{"id": "rel-1", "title": "Album", "date": {"year": 2001, "month": 5}}],
                }
            ],
        }
        (cand,) = candidates([result])
        assert cand.meta.artist == "A & B"
        assert cand.meta.musicbrainz_artistid == ["a1", "a2"]
        assert (cand.meta.musicbrainz_albumid, cand.meta.musicbrainz_releasegroupid) == ("rel-1", None)
        assert cand.meta.date == "2001-05"
        assert cand.meta.musicbrainz_trackid is None

    def test_recording_without_releases(self):
        (cand,) = candidates([{"id": "ac-1", "score": 0.5, "recordings": [{"id": "rec-1", "title": "Just Title"}]}])
        assert cand.meta.title == "Just Title"
        assert cand.meta.musicbrainz_albumid is None

    def test_no_results(self):
        assert candidates([]) == []
        assert candidates([{"id": "x", "score": 1.0}]) == []
