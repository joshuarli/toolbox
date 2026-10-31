"""Tests for the MusicBrainz client — recorded real responses (tests/music/fixtures), no network."""

import json
from unittest.mock import patch

import pytest

from tool.music.api.musicbrainz import (
    MusicBrainzRequestError,
    enrich,
    find_artist,
    search,
)
from tool.music.metadata import Artist, Metadata

KAL_REC = "1db8dba2-0d42-43e2-94d7-0a6a8df7d2d7"
KAL_GROUP = "4cda8183-371b-4189-bcb1-265bd0ff4b5f"
KAL_ARTIST = "7c67fb96-7e24-4893-a020-fcc53a1f7814"
KAL_CD = "490a58ac-6f2d-482e-8915-b703e229794b"
KAL_VINYL = "c0035a48-a3bd-4a66-9a5a-938ffaf67f23"
KAL_WITHDRAWN = "2e58a475-b292-4570-a021-04aa00d29f3e"
BLUE_NOTEBOOKS = "01d9453c-e652-4a3e-9dfd-53555692871a"
DAYLIGHT_REC = "b75c56c6-cbfc-4d1a-83fd-b2e3f97194cf"
TIME_LAPSE = "eafdf4ed-d167-4c37-88b4-3c98dbb97df0"
CORALE_REC = "69af617d-c2b5-46a9-9316-fd1b6d20c81d"
BEVER_REC = "3165fc57-c69a-4316-8cab-339a846e3a70"  # disc 2, track 1 of the time lapse special edition


@pytest.fixture
def mb(load_fixture):
    """Patch the MusicBrainz session to serve one fixture; returns the mock to inspect calls."""

    def serve(name: str):
        return patch("tool.music.api.musicbrainz._session.get_json", return_value=load_fixture(name))

    return serve


def selected(recording: str, release: str | None = None, track: str | None = None) -> Metadata:
    return Metadata(musicbrainz_recordingid=recording, musicbrainz_albumid=release, musicbrainz_trackid=track)


class TestStraightforwardRelease:
    def test_single_request_fills_everything(self, mb):
        with mb("mb_release_kalandra_cd.json") as get:
            m = enrich(selected(KAL_REC, KAL_CD))
        assert get.call_count == 1
        assert get.call_args.args[0].endswith(f"/release/{KAL_CD}")

        assert (m.title, m.artist, m.album, m.albumartist) == ("It Gets Easier", "Kalandra", "The Line", "Kalandra")
        assert (m.tracknumber, m.totaltracks, m.discnumber, m.totaldiscs) == ("11", "11", "1", "1")
        assert (m.date, m.originaldate, m.releasecountry, m.releasestatus) == (
            "2020-10-23",
            "2020-10-23",
            "NO",
            "Official",
        )
        assert (m.releasetype, m.media, m.barcode) == (["album"], "CD", "612608050884")
        assert m.musicbrainz_recordingid == KAL_REC
        assert m.musicbrainz_trackid == "82185c30-f97f-4b1b-8c70-a0bd3d077649"
        assert m.musicbrainz_albumid == KAL_CD
        assert m.musicbrainz_releasegroupid == KAL_GROUP
        assert m.musicbrainz_artistid == [KAL_ARTIST]
        assert m.musicbrainz_albumartistid == [KAL_ARTIST]
        assert m.artistsort == m.albumartistsort == "Kalandra"
        assert m.genre == ["pop", "rock", "folk rock", "alternative pop", "alternative rock"]  # by weight, max five

    def test_acoustid_identity_is_kept(self, mb):
        sel = selected(KAL_REC, KAL_CD)
        sel.acoustid_id = "e8187d3a-71a8-49ef-8f95-2fa590426190"
        with mb("mb_release_kalandra_cd.json"):
            assert enrich(sel).acoustid_id == "e8187d3a-71a8-49ef-8f95-2fa590426190"


class TestSelectedReleaseControlsIdentity:
    """One recording, several releases: the chosen release decides release/track/medium."""

    def test_cd_versus_vinyl(self, mb):
        with mb("mb_release_kalandra_cd.json"):
            cd = enrich(selected(KAL_REC, KAL_CD))
        with mb("mb_release_kalandra_vinyl.json"):
            vinyl = enrich(selected(KAL_REC, KAL_VINYL))

        assert cd.musicbrainz_recordingid == vinyl.musicbrainz_recordingid == KAL_REC
        assert (cd.musicbrainz_albumid, vinyl.musicbrainz_albumid) == (KAL_CD, KAL_VINYL)
        assert cd.musicbrainz_trackid != vinyl.musicbrainz_trackid
        assert vinyl.musicbrainz_trackid == "2aabd1c8-3793-4848-ba05-acf7ddd4b9ce"
        assert (vinyl.media, vinyl.discnumber, vinyl.totaldiscs) == ('12" Vinyl', "2", "2")
        # printed position is C3, the position on the medium is 3 of 3
        assert (vinyl.tracknumber, vinyl.totaltracks) == ("3", "3")
        assert vinyl.catalognumber == ["BNM017LPB"]
        assert cd.catalognumber == ["BNM017CD"]
        assert vinyl.barcode != cd.barcode

    def test_track_found_by_release_track_id_when_recording_id_is_stale(self, mb):
        # e.g. an AcoustID recording MBID that has since been merged on MusicBrainz
        stale = selected("00000000-0000-0000-0000-000000000000", KAL_VINYL, "2aabd1c8-3793-4848-ba05-acf7ddd4b9ce")
        with mb("mb_release_kalandra_vinyl.json"):
            m = enrich(stale)
        assert m.musicbrainz_recordingid == KAL_REC  # canonical id comes from the release
        assert m.discnumber == "2"

    def test_recording_missing_from_release_keeps_release_identity(self, mb, capsys):
        with mb("mb_release_kalandra_cd.json"):
            m = enrich(selected("00000000-0000-0000-0000-000000000000", KAL_CD))
        assert m.musicbrainz_albumid == KAL_CD
        assert m.musicbrainz_releasegroupid == KAL_GROUP
        assert m.musicbrainz_trackid is None
        assert m.tracknumber is None
        assert "not found on the selected release" in capsys.readouterr().err


class TestMultipleArtists:
    def test_track_credit_vs_album_credit(self, mb):
        with mb("mb_release_time_lapse.json"):
            m = enrich(selected(BEVER_REC, TIME_LAPSE))
        assert m.artist == "Ludovico Einaudi & Greta Svabo Bech"
        assert m.artists == ["Ludovico Einaudi", "Greta Svabo Bech"]
        assert m.artistsort == "Einaudi, Ludovico & Bech, Greta Svabo"
        assert m.albumartist == "Ludovico Einaudi"
        assert m.albumartistsort == "Einaudi, Ludovico"
        assert m.musicbrainz_artistid == [
            "fa34b363-79df-434f-a5b8-be4e6898543f",
            "448ce1b7-4efc-4aca-b793-174745cc4e68",
        ]
        assert m.musicbrainz_albumartistid == ["fa34b363-79df-434f-a5b8-be4e6898543f"]


class TestMultiDisc:
    def test_second_disc_track(self, mb):
        with mb("mb_release_time_lapse.json"):
            m = enrich(selected(BEVER_REC, TIME_LAPSE))
        assert (m.discnumber, m.totaldiscs) == ("2", "2")
        assert (m.tracknumber, m.totaltracks) == ("1", "13")  # disc 2 has 13 tracks, disc 1 has 14

    def test_first_disc_track_of_same_release(self, mb):
        with mb("mb_release_time_lapse.json"):
            m = enrich(selected(CORALE_REC, TIME_LAPSE))
        assert (m.discnumber, m.tracknumber, m.totaltracks) == ("1", "1", "14")

    def test_vinyl_sides_are_media(self, mb):
        with mb("mb_release_kalandra_vinyl.json"):
            m = enrich(selected(KAL_REC, KAL_VINYL))
        assert (m.discnumber, m.totaldiscs) == ("2", "2")


class TestLabelsCatalogNumbersIsrcs:
    def test_label_catno_barcode_isrc(self, mb):
        with mb("mb_release_blue_notebooks.json"):
            m = enrich(selected(DAYLIGHT_REC, BLUE_NOTEBOOKS))
        assert m.label == ["Deutsche Grammophon"]
        assert m.catalognumber == ["479 4443"]
        assert m.barcode == "028947944430"
        assert m.isrc == ["GBDCA0300279"]
        assert m.releasecountry == "XE"


class TestRelationshipCredits:
    def test_work_and_composer(self, mb):
        with mb("mb_release_blue_notebooks.json"):
            m = enrich(selected(DAYLIGHT_REC, BLUE_NOTEBOOKS))
        assert m.musicbrainz_workid == ["51c1a82f-61fc-4c77-9429-0f4d82d29a71"]
        assert m.composer == ["Max Richter"]
        assert m.producer == ["Max Richter", "Matthias Schneider"]
        assert m.performer == [
            "Natalia Bonner (violin)",
            "Louisa Fuller (violin)",
            "John Metcalfe (viola)",
            "Max Richter (Moog)",
            "Philip Sheppard (cello)",
            "Chris Worsey (cello)",
        ]
        assert m.remixer == m.conductor == []

    def test_arranger_and_composer_from_recording_and_work(self, mb):
        with mb("mb_release_time_lapse.json"):
            m = enrich(selected(CORALE_REC, TIME_LAPSE))
        assert m.composer == ["Ludovico Einaudi"]
        assert m.arranger == ["Ludovico Einaudi"]
        assert m.musicbrainz_workid == ["e9d70be0-19b2-4255-b8c9-72df579bbae8"]

    def test_multiple_writers(self, mb):
        with mb("mb_release_kalandra_cd.json"):
            m = enrich(selected(KAL_REC, KAL_CD))
        assert m.writer == [
            "Jogeir Daae Maeland",
            "Oskar Johnsen Rydh",
            "Katrine Stenbekk",
            "Florian Bernhard Döderlein Winter",
        ]
        assert m.musicbrainz_workid == ["9382d3f9-eff6-41ac-88d7-bdf06dd827c1"]

    def test_only_conventional_credits_are_kept(self, mb):
        # engineer / mix / recording-location relations exist but are not tag fields here
        with mb("mb_release_kalandra_cd.json"):
            m = enrich(selected(KAL_REC, KAL_CD))
        assert "Steve Parr" not in " ".join(m.to_values().get("performer", []))


class TestIncompleteMetadata:
    def test_withdrawn_release_without_country_or_catno(self, mb):
        with mb("mb_release_kalandra_withdrawn.json"):
            m = enrich(selected(KAL_REC, KAL_WITHDRAWN))
        assert m.releasecountry is None
        assert m.catalognumber == []
        assert m.label == ["By Norse Music"]
        assert m.releasestatus == "Withdrawn"
        assert m.musicbrainz_albumid == KAL_WITHDRAWN
        assert m.is_complete()  # identity is all that counts; missing optional fields don't matter

    def test_label_without_catalog_number_and_unknown_country(self, mb):
        with mb("mb_release_time_lapse.json"):
            m = enrich(selected(CORALE_REC, TIME_LAPSE))
        assert m.label == ["Decca Records"]
        assert m.catalognumber == []
        assert m.releasecountry is None

    def test_no_tags_to_write_for_absent_fields(self, mb):
        with mb("mb_release_kalandra_withdrawn.json"):
            values = enrich(selected(KAL_REC, KAL_WITHDRAWN)).to_values()
        assert "releasecountry" not in values
        assert "catalognumber" not in values
        assert "remixer" not in values


class TestRecordingOnly:
    def test_recording_lookup_when_no_release_selected(self, mb):
        with mb("mb_recording_kalandra.json") as get:
            m = enrich(selected(KAL_REC))
        assert get.call_count == 1
        assert get.call_args.args[0].endswith(f"/recording/{KAL_REC}")
        assert m.title == "It Gets Easier"
        assert m.artist == "Kalandra"
        assert m.isrc == ["NONAT2001110"]
        assert m.musicbrainz_artistid == [KAL_ARTIST]
        assert m.musicbrainz_workid == ["9382d3f9-eff6-41ac-88d7-bdf06dd827c1"]
        assert m.musicbrainz_albumid is None
        assert m.musicbrainz_trackid is None
        assert m.writer
        assert m.producer


class TestFailure:
    def test_lookup_failure_keeps_selection(self, capsys):
        sel = selected(KAL_REC, KAL_CD)
        with patch("tool.music.api.musicbrainz._session.get_json", side_effect=TimeoutError("boom")):
            assert enrich(sel) == sel
        assert "boom" in capsys.readouterr().err

    def test_nothing_to_look_up(self):
        with patch("tool.music.api.musicbrainz._session.get_json") as get:
            assert enrich(Metadata(title="T")) == Metadata(title="T")
        get.assert_not_called()


class TestSearch:
    def test_one_candidate_per_release(self, mb):
        with mb("mb_search_kalandra.json"):
            cands = search("query")
        assert len(cands) == 7  # 5 releases of the studio recording + 2 of the live one
        studio = [c for c in cands if c.meta.musicbrainz_recordingid == KAL_REC]
        assert {c.meta.musicbrainz_albumid for c in studio} == {
            KAL_CD,
            KAL_VINYL,
            KAL_WITHDRAWN,
            "61eb596b-7a13-4894-9d20-5942218d2329",
            "6095c733-476b-4b4f-8258-6efa16eaeded",
        }
        assert all(c.meta.musicbrainz_releasegroupid == KAL_GROUP for c in studio)

    def test_candidate_carries_medium_and_track(self, mb):
        with mb("mb_search_kalandra.json"):
            vinyl = next(c.meta for c in search("q") if c.meta.musicbrainz_albumid == KAL_VINYL)
        assert vinyl.media == '12" Vinyl'
        assert vinyl.musicbrainz_trackid == "2aabd1c8-3793-4848-ba05-acf7ddd4b9ce"
        assert (vinyl.discnumber, vinyl.tracknumber, vinyl.totaltracks) == ("2", "3", "3")
        assert (vinyl.date, vinyl.releasecountry, vinyl.releasestatus) == ("2020-10-23", "NO", "Official")
        assert vinyl.totaldiscs is None  # unknown from search; filled by enrichment
        assert vinyl.releasetype == ["album"]

    def test_scores_and_secondary_types(self, mb):
        with mb("mb_search_kalandra.json"):
            cands = search("q")
        live = [c for c in cands if "Live" in (c.meta.title or "")]
        assert {c.score for c in live} == {0.78}
        assert live[0].meta.releasetype == ["album", "live"]

    def test_credited_artists(self, mb):
        with mb("mb_search_daylight.json"):
            cands = search("q")
        assert len(cands) == 2
        assert cands[0].meta.artist.startswith("Natalia Bonner, Louisa Fuller")
        assert len(cands[0].meta.musicbrainz_artistid) == 6
        assert cands[0].meta.musicbrainz_releasegroupid != cands[1].meta.musicbrainz_releasegroupid

    def test_recording_without_releases(self):
        resp = {"recordings": [{"id": "rec-1", "title": "Orphan", "score": 80, "artist-credit": []}]}
        with patch("tool.music.api.musicbrainz._session.get_json", return_value=resp):
            (cand,) = search("q")
        assert cand.score == 0.8
        assert cand.meta.musicbrainz_albumid is None

    def test_network_error_is_not_an_empty_result(self):
        cause = TimeoutError("slow response")
        with (
            patch("tool.music.api.musicbrainz._session.get", side_effect=cause),
            pytest.raises(MusicBrainzRequestError) as exc,
        ):
            search('recording:"Test"')
        assert exc.value.__cause__ is cause

    @pytest.mark.parametrize(
        ("body", "error_type"),
        [(b"{", json.JSONDecodeError), (b"\xff", UnicodeDecodeError)],
    )
    def test_response_decoding_error_is_a_request_error(self, body, error_type):
        with (
            patch("tool.music.api.musicbrainz._session.get", return_value=body),
            pytest.raises(MusicBrainzRequestError) as exc,
        ):
            search('recording:"Test"')
        assert isinstance(exc.value.__cause__, error_type)

    def test_unexpected_programming_error_is_not_wrapped(self):
        bug = AssertionError("unexpected internal state")
        with (
            patch("tool.music.api.musicbrainz._session.get_json", side_effect=bug),
            pytest.raises(AssertionError) as exc,
        ):
            search('recording:"Test"')
        assert exc.value is bug

    def test_empty_response(self):
        with patch("tool.music.api.musicbrainz._session.get_json", return_value={"recordings": []}):
            assert search('recording:"Nonexistent"') == []

    def test_search_then_enrich_resolves_same_release_representation(self, mb):
        with mb("mb_search_kalandra.json"):
            vinyl = next(c for c in search("q") if c.meta.musicbrainz_albumid == KAL_VINYL)
        with mb("mb_release_kalandra_vinyl.json"):
            m = enrich(vinyl.meta)
        assert m.musicbrainz_trackid == vinyl.meta.musicbrainz_trackid
        assert (m.discnumber, m.tracknumber) == (vinyl.meta.discnumber, vinyl.meta.tracknumber)


class TestFindArtist:
    def test_clear_top_hit_resolves(self, mb):
        with mb("mb_artist_search_corre.json") as get:
            artist = find_artist("Corre")
        assert artist == Artist(
            "0850da7c-c769-48c1-a626-cfc2b73d4863", "Corre", "Henry Green's instrumental side project"
        )
        assert get.call_args.kwargs["params"]["query"] == 'artist:"Corre"'

    def test_ambiguous_name_is_not_guessed(self, mb):
        # "Air" scores 100 but "Air Supply" scores 97
        with mb("mb_artist_search_air.json"):
            assert find_artist("Air") is None

    def test_weak_match_is_rejected(self):
        resp = {"artists": [{"id": "x", "name": "Something", "score": 80}]}
        with patch("tool.music.api.musicbrainz._session.get_json", return_value=resp):
            assert find_artist("Something Else") is None

    def test_no_hits_and_network_error_are_distinct(self):
        with patch("tool.music.api.musicbrainz._session.get_json", return_value={"artists": []}):
            assert find_artist("Nobody") is None
        cause = TimeoutError("slow response")
        with (
            patch("tool.music.api.musicbrainz._session.get_json", side_effect=cause),
            pytest.raises(MusicBrainzRequestError) as exc,
        ):
            find_artist("Nobody")
        assert exc.value.__cause__ is cause

    def test_quotes_are_escaped(self):
        with patch("tool.music.api.musicbrainz._session.get_json", return_value={"artists": []}) as get:
            find_artist('The "Quoted" One')
        assert get.call_args.kwargs["params"]["query"] == 'artist:"The \\"Quoted\\" One"'
