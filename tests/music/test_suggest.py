import argparse
import json
import re
import shutil
from unittest.mock import patch

import pytest

from tool.music.api.listenbrainz import Similar
from tool.music.metadata import Artist, Metadata
from tool.music.suggest import Seeds, _credits, _rank, run
from tool.music.tags import read_tags, write_tags

CORRE = "0850da7c-c769-48c1-a626-cfc2b73d4863"
EMANCIPATOR = "aa1d4315-5246-42b2-b62b-f997d046d8b2"
ALBUM_LEAF = "ffb18e19-64a4-4a65-b4ce-979e00c3c69d"


def _args(playlist, library, no_tag=False):
    return argparse.Namespace(playlist=str(playlist), library=str(library), limit=30, no_tag=no_tag)


@pytest.fixture(autouse=True)
def no_autotag_env():
    """Autotagging needs an AcoustID key and fpcalc; tests opt in explicitly."""
    with patch("tool.music.suggest._can_autotag", return_value=False):
        yield


@pytest.fixture
def lb(load_fixture):
    """Serve the recorded rows for a Corre-only playlist (ListenBrainz only returns requested seeds)."""
    rows = [r for r in load_fixture("lb_similar_artists.json") if r["reference_mbid"] == CORRE]
    return patch("tool.music.api.listenbrainz._session.get_text", return_value=json.dumps(rows))


@pytest.fixture
def playlist(tmp_path, flac_file):
    """A one-track playlist whose file is tagged with Corre's artist MBID."""
    d = tmp_path / "playlist"
    d.mkdir()
    track = d / "proceed.flac"
    shutil.copy(flac_file, track)
    write_tags(str(track), Metadata(title="Proceed", artist="Corre", artists=["Corre"], musicbrainz_artistid=[CORRE]))
    return d


def _output(capsys) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)


class TestCredits:
    def test_ids_paired_with_names(self):
        meta = Metadata(artist="A & B", artists=["A", "B"], musicbrainz_artistid=["1", "2"])
        assert _credits(meta) == [("1", "A"), ("2", "B")]

    def test_ids_without_matching_names_fall_back_to_credit(self):
        assert _credits(Metadata(artist="A & B", musicbrainz_artistid=["1", "2"])) == [("1", "A & B"), ("2", "A & B")]

    def test_untagged_artist_has_name_only(self):
        assert _credits(Metadata(artist="Corre")) == [(None, "Corre")]
        assert _credits(Metadata()) == []


class TestRun:
    def test_ranks_similar_artists_excluding_seeds(self, playlist, tmp_path, lb, capsys):
        with lb:
            run(_args(playlist, tmp_path / "nonexistent-library"))
        out = _output(capsys)
        assert "Seeds: Corre" in out
        rows = [line for line in out.splitlines() if " via " in line]
        assert "Emancipator" in rows[0]
        assert "100" in rows[0]
        assert "via Corre" in rows[0]
        assert all("Corre " not in r.split("via")[0] for r in rows)

    def test_library_artists_are_excluded_by_mbid_and_by_name(self, playlist, tmp_path, flac_file, lb, capsys):
        library = tmp_path / "library"
        library.mkdir()
        by_mbid = library / "a.flac"
        by_name = library / "b.flac"
        for p in (by_mbid, by_name):
            shutil.copy(flac_file, p)
        write_tags(str(by_mbid), Metadata(artist="Whoever", musicbrainz_artistid=[EMANCIPATOR]))
        write_tags(str(by_name), Metadata(artist="the album leaf"))  # no MBID, different case

        with lb:
            run(_args(playlist, library))
        out = _output(capsys)
        assert "Emancipator" not in out
        assert "The Album Leaf" not in out
        assert "Shallou" in out

    def test_untagged_playlist_resolves_names_on_musicbrainz(self, tmp_path, flac_file, lb, load_fixture, capsys):
        d = tmp_path / "pl"
        d.mkdir()
        track = d / "t.flac"
        shutil.copy(flac_file, track)
        write_tags(str(track), Metadata(title="Proceed", artist="Corre"))  # no artist MBID

        with (
            lb,
            patch(
                "tool.music.api.musicbrainz._session.get_json", return_value=load_fixture("mb_artist_search_corre.json")
            ),
        ):
            run(_args(d, tmp_path / "nolib"))
        out = _output(capsys)
        assert "Corre → Corre (Henry Green's instrumental side project)" in out
        assert "Emancipator" in out

    @staticmethod
    def untagged(tmp_path, flac_file, name="t.flac"):
        d = tmp_path / "pl"
        d.mkdir()
        shutil.copy(flac_file, d / name)
        write_tags(str(d / name), Metadata(title="Proceed", artist="Corre"))
        return d

    def test_untagged_files_are_autotagged_and_seed_by_mbid(self, tmp_path, flac_file, lb, capsys):
        d = self.untagged(tmp_path, flac_file)

        def fake_autotag(path):
            meta = Metadata(title="Proceed", artist="Corre", artists=["Corre"], musicbrainz_artistid=[CORRE])
            write_tags(path, meta)
            return meta

        with (
            lb,
            patch("tool.music.suggest._can_autotag", return_value=True),
            patch("tool.music.suggest.autotag", side_effect=fake_autotag) as at,
            patch("tool.music.suggest.find_artist") as find,
        ):
            run(_args(d, tmp_path / "nolib"))
        out = _output(capsys)
        assert at.call_count == 1
        find.assert_not_called()  # the tags now carry the MBID, so no name lookup
        assert "tagged Corre — Proceed (recording only" in out
        assert "Emancipator" in out
        assert read_tags(str(d / "t.flac")).musicbrainz_artistid == [CORRE]

    def test_already_identified_files_are_not_retagged(self, playlist, tmp_path, lb):
        with lb, patch("tool.music.suggest._can_autotag", return_value=True), patch("tool.music.suggest.autotag") as at:
            run(_args(playlist, tmp_path / "nolib"))
        at.assert_not_called()

    def test_no_tag_flag_skips_autotag(self, tmp_path, flac_file, lb, load_fixture):
        d = self.untagged(tmp_path, flac_file)
        with (
            lb,
            patch("tool.music.suggest._can_autotag", return_value=True),
            patch("tool.music.suggest.autotag") as at,
            patch(
                "tool.music.api.musicbrainz._session.get_json", return_value=load_fixture("mb_artist_search_corre.json")
            ),
        ):
            run(_args(d, tmp_path / "nolib", no_tag=True))
        at.assert_not_called()

    def test_ambiguous_fingerprint_falls_back_to_name_lookup(self, tmp_path, flac_file, lb, load_fixture, capsys):
        d = self.untagged(tmp_path, flac_file)
        with (
            lb,
            patch("tool.music.suggest._can_autotag", return_value=True),
            patch("tool.music.suggest.autotag", return_value=None),
            patch(
                "tool.music.api.musicbrainz._session.get_json", return_value=load_fixture("mb_artist_search_corre.json")
            ),
        ):
            run(_args(d, tmp_path / "nolib"))
        out = _output(capsys)
        assert "t.flac: no unambiguous fingerprint match, left untagged" in out
        assert "Corre → Corre" in out

    def test_missing_key_notes_once_and_still_works(self, tmp_path, flac_file, lb, load_fixture, capsys):
        d = self.untagged(tmp_path, flac_file)
        shutil.copy(flac_file, d / "u.flac")
        write_tags(str(d / "u.flac"), Metadata(artist="Corre"))
        with (
            lb,
            patch(
                "tool.music.api.musicbrainz._session.get_json", return_value=load_fixture("mb_artist_search_corre.json")
            ),
        ):
            run(_args(d, tmp_path / "nolib"))
        out = _output(capsys)
        assert out.count("set ACOUSTID_API_KEY") == 1
        assert "Emancipator" in out

    def test_ambiguous_name_is_reported_not_guessed(self, tmp_path, flac_file, load_fixture, capsys):
        d = tmp_path / "pl"
        d.mkdir()
        track = d / "t.flac"
        shutil.copy(flac_file, track)
        write_tags(str(track), Metadata(artist="Air"))

        with (
            patch(
                "tool.music.api.musicbrainz._session.get_json", return_value=load_fixture("mb_artist_search_air.json")
            ),
            pytest.raises(SystemExit) as exc,
        ):
            run(_args(d, tmp_path / "nolib"))
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "Air: no clear MusicBrainz match" in captured.out
        assert "no artists to seed from" in captured.err

    def test_empty_playlist(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc:
            run(_args(tmp_path, tmp_path))
        assert exc.value.code == 1
        assert "no audio files" in capsys.readouterr().err

    def test_listenbrainz_failure(self, playlist, tmp_path, capsys):
        with (
            patch("tool.music.api.listenbrainz._session.get_text", side_effect=Exception("down")),
            pytest.raises(SystemExit) as exc,
        ):
            run(_args(playlist, tmp_path / "nolib"))
        assert exc.value.code == 1
        assert "down" in capsys.readouterr().err


class TestRank:
    """Scores are only comparable within one seed; shared candidates and heavier seeds rank higher."""

    @staticmethod
    def sim(rows):
        return [Similar(Artist(m, n), score, seed) for m, n, score, seed in rows]

    @staticmethod
    def seeds():
        seeds = Seeds()
        seeds.add("seed-a", "A", tracks=1)
        seeds.add("seed-b", "B", tracks=1)
        return seeds

    def test_each_seed_is_normalised_to_its_best_match(self):
        rows = self.sim(
            [
                ("x", "X", 4000, "seed-a"),
                ("y", "Y", 2000, "seed-a"),
                ("z", "Z", 100, "seed-b"),
                ("w", "W", 10, "seed-b"),
            ]
        )
        ranked = _rank(self.seeds(), rows, set(), set())
        # X and Z are each their seed's best (1.0); Y is 0.5; W is 0.1
        assert {s.artist.name: s.score for s in ranked} == {"X": 1.0, "Z": 1.0, "Y": 0.5, "W": 0.1}
        assert [s.artist.name for s in ranked][2:] == ["Y", "W"]

    def test_artist_similar_to_several_seeds_accumulates(self):
        rows = self.sim(
            [("x", "X", 100, "seed-a"), ("y", "Y", 100, "seed-a"), ("x", "X", 50, "seed-b"), ("q", "Q", 100, "seed-b")]
        )
        ranked = _rank(self.seeds(), rows, set(), set())
        assert ranked[0].artist.name == "X"
        assert ranked[0].score == pytest.approx(1.5)
        assert set(ranked[0].via) == {"A", "B"}

    def test_seed_weight_is_track_count(self):
        seeds = Seeds()
        seeds.add("seed-a", "A", tracks=4)
        seeds.add("seed-b", "B", tracks=1)
        ranked = _rank(seeds, self.sim([("x", "X", 100, "seed-a"), ("y", "Y", 100, "seed-b")]), set(), set())
        assert [s.artist.name for s in ranked] == ["X", "Y"]
        assert ranked[0].score == 4.0

    def test_seeds_and_known_artists_are_dropped(self):
        rows = self.sim(
            [
                ("seed-b", "B", 100, "seed-a"),
                ("k", "Known", 90, "seed-a"),
                ("n", "Named", 80, "seed-a"),
                ("x", "X", 70, "seed-a"),
            ]
        )
        ranked = _rank(self.seeds(), rows, {"k"}, {"named"})
        assert [s.artist.name for s in ranked] == ["X"]
