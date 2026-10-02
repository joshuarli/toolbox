import argparse
from unittest.mock import patch

import pytest
from mutagen.flac import FLAC

from tool.music.metadata import Candidate, Metadata
from tool.music.tag import (
    _fallback_search,
    _fallback_select,
    _match_lines,
    _normalize_filename,
    _print_tags,
    _rank,
    autotag,
    format_diff,
    format_match_line,
    interactive_select,
    run,
)
from tool.music.tags import read_tags, write_tags

KAL_REC = "1db8dba2-0d42-43e2-94d7-0a6a8df7d2d7"
KAL_VINYL = "c0035a48-a3bd-4a66-9a5a-938ffaf67f23"
KAL_CD = "490a58ac-6f2d-482e-8915-b703e229794b"
FINGERPRINT = "AQADtImSSEmSJFGk" * 100


def cand(score=0.9, **fields) -> Candidate:
    return Candidate(score, Metadata(**fields))


class TestFormatMatchLine:
    def test_full_line(self):
        c = cand(
            0.95,
            title="The Song",
            artist="The Artist",
            album="The Album",
            date="2023-04-05",
            releasecountry="GB",
            media="CD",
            discnumber="2",
            totaldiscs="3",
            tracknumber="7",
        )
        line = format_match_line(1, c)
        for part in ("95%", "The Song", "The Artist", "The Album", "2023-04-05", "GB", "CD", "disc 2/3", "#7"):
            assert part in line

    def test_minimal(self):
        line = format_match_line(1, cand(0.5, title="Song"))
        assert "50%" in line
        assert "Song" in line
        assert "?" in line  # no artist

    def test_single_disc_not_shown(self):
        line = format_match_line(1, cand(title="S", album="A", discnumber="1", totaldiscs="1"))
        assert "disc" not in line

    def test_disc_shown_when_total_unknown(self):
        assert "disc 2" in format_match_line(1, cand(title="S", album="A", discnumber="2"))

    def test_identical_looking_editions_get_release_id(self):
        a = cand(title="S", artist="A", album="Same", date="2020", musicbrainz_albumid="aaaaaaaa-1")
        b = cand(title="S", artist="A", album="Same", date="2020", musicbrainz_albumid="bbbbbbbb-2")
        c = cand(title="S", artist="A", album="Other", date="2020", musicbrainz_albumid="cccccccc-3")
        lines = _match_lines([a, b, c])
        assert "aaaaaaaa" in lines[0]
        assert "bbbbbbbb" in lines[1]
        assert "cccccccc" not in lines[2]


class TestRank:
    def test_orders_by_score_then_matching_album_then_plain_then_date(self):
        current = Metadata(album="The Line")
        compilation = cand(0.99, album="The Line", releasetype=["album", "compilation"], date="2019")
        late = cand(0.99, album="The Line", releasetype=["album"], date="2023")
        early = cand(0.99, album="The Line", releasetype=["album"], date="2020")
        other = cand(0.99, album="Other", releasetype=["album"], date="2010")
        weak = cand(0.80, album="The Line", releasetype=["album"], date="2010")
        assert _rank([weak, other, compilation, late, early], current) == [early, late, compilation, other, weak]

    def test_does_not_drop_candidates(self):
        cands = [cand(0.5, title=str(i)) for i in range(40)]
        assert len(_rank(cands, Metadata())) == 40


class TestFormatDiff:
    def test_added_field(self):
        lines = format_diff(Metadata(), Metadata(title="New Title"))
        assert len(lines) == 2
        assert "(none)" in lines[0]
        assert "+" in lines[1]

    def test_changed_field(self):
        lines = format_diff(Metadata(title="Old"), Metadata(title="New"))
        assert len(lines) == 2
        assert "Old" in lines[0]
        assert "→" in lines[1]

    def test_no_changes(self):
        assert format_diff(Metadata(title="Same"), Metadata(title="Same")) == []

    def test_only_fields_that_would_be_written_are_shown(self):
        current = Metadata(title="Old Title", artist="Same", genre=["x"])
        new = Metadata(title="New Title", artist="Same", album="New Album")
        lines = format_diff(current, new)
        assert len(lines) == 4  # title changed, album added; artist same, genre untouched
        assert not any("genre" in line for line in lines)

    def test_multi_values_compared_as_lists(self):
        assert format_diff(Metadata(genre=["a", "b"]), Metadata(genre=["a", "b"])) == []
        (_old, new) = format_diff(Metadata(genre=["a"]), Metadata(genre=["a", "b"]))
        assert "a; b" in new

    def test_fingerprint_is_abbreviated(self):
        lines = format_diff(Metadata(), Metadata(acoustid_fingerprint=FINGERPRINT))
        text = "\n".join(lines)
        assert FINGERPRINT not in text
        assert f"{len(FINGERPRINT)} chars" in text


class TestInteractiveSelect:
    def test_candidate_list_scrolls_with_the_cursor(self):
        cands = [cand(title=f"Song {i:02d}", artist="A", musicbrainz_albumid=str(i)) for i in range(30)]
        frames: list[str] = []

        def fake_select(render, count):
            frames.extend("\n".join(render(i)) for i in (0, 15, 29))
            return 0

        with (
            patch("tool.music.tag.select_interactive", side_effect=fake_select),
            patch("tool.music.tag.read_tags", return_value=Metadata()),
        ):
            interactive_select(cands, "f.flac")

        top, middle, bottom = frames
        assert "Song 00" in top
        assert "Song 12" not in top
        assert "↓ 18 more" in top
        assert " more" not in top.replace("↓ 18 more", "")
        assert "Song 15" in middle
        assert "↑ 9 more" in middle
        assert "↓ 9 more" in middle
        assert "Song 29" in bottom
        assert "↑ 18 more" in bottom
        assert "↓" not in bottom.replace("↑/↓", "")
        assert "Song 00" not in bottom

    def test_shows_diff_for_highlighted_candidate(self):
        frames: list[str] = []

        def fake_select(render, count):
            frames.append("\n".join(render(1)))
            return 1

        cands = [cand(title="First"), cand(title="Second", album="Other")]
        with (
            patch("tool.music.tag.select_interactive", side_effect=fake_select),
            patch("tool.music.tag.read_tags", return_value=Metadata(title="Second")),
        ):
            assert interactive_select(cands, "f.flac") == 1
        assert "album" in frames[0]
        assert "Other" in frames[0]


class TestFallbackSelect:
    def test_valid_selection(self):
        with patch("builtins.input", return_value="1"):
            assert _fallback_select([cand(title="Song")]) == 0

    def test_quit(self):
        with patch("builtins.input", return_value="q"), pytest.raises(SystemExit):
            _fallback_select([cand(title="Song")])

    def test_invalid_selection_exits(self):
        with patch("builtins.input", return_value="not_a_number"), pytest.raises(SystemExit):
            _fallback_select([cand(title="Song")])

    def test_out_of_range(self):
        with patch("builtins.input", return_value="99"), pytest.raises(SystemExit):
            _fallback_select([cand(title="Song")])


class TestPrintTags:
    def test_prints_tags(self, capsys):
        with patch("tool.music.tag.read_tags", return_value=Metadata(title="T", artist="A")):
            _print_tags("test.flac")
        out = capsys.readouterr().out
        assert "title" in out
        assert "artist" in out

    def test_no_tags(self, capsys):
        with patch("tool.music.tag.read_tags", return_value=Metadata()):
            _print_tags("test.flac")
        assert "no tags" in capsys.readouterr().out

    def test_shows_ids_and_abbreviates_fingerprint(self, capsys):
        meta = Metadata(musicbrainz_recordingid=KAL_REC, musicbrainz_albumid=KAL_CD, acoustid_fingerprint=FINGERPRINT)
        with patch("tool.music.tag.read_tags", return_value=meta):
            _print_tags("test.flac")
        out = capsys.readouterr().out
        assert KAL_REC in out
        assert KAL_CD in out
        assert FINGERPRINT not in out
        assert "chars" in out


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
            tags = Metadata(title="Found Song", artist="Test Artist")
            results = _fallback_search("/some/file.mp3", tags)
            assert len(results) == 1
            assert results[0].meta.title == "Found Song"

    def test_falls_back_to_filename(self):
        """When metadata search returns nothing, tries normalized filename."""
        call_count = [0]

        def side_effect(url, **kwargs):
            call_count[0] += 1
            if call_count[0] == 2:
                return {"recordings": [{"id": "mb-2", "title": "Filename Song", "score": 90}]}
            return {"recordings": []}

        with patch("tool.music.api.musicbrainz._session.get_json", side_effect=side_effect):
            tags = Metadata(title="Some Title", artist="Some Artist")
            results = _fallback_search("/path/01 - Actual Song Name.mp3", tags)
            assert len(results) == 1
            assert results[0].meta.title == "Filename Song"

    def test_no_metadata_and_bad_filename(self):
        """When nothing works, returns empty list."""
        with patch("tool.music.api.musicbrainz._session.get_json", return_value={"recordings": []}):
            results = _fallback_search("/path/01 - .mp3", Metadata())
            assert results == []

    def test_empty_tags_no_filename_match(self):
        """No metadata in file, filename normalizes to empty/nothing useful."""
        with patch("tool.music.api.musicbrainz._session.get_json", return_value={"recordings": []}):
            results = _fallback_search("/path/01 - .mp3", Metadata(title="Unknown"))
            assert results == []

    def test_fallback_search_preserves_recording_id(self):
        """MB search results keep recording and release ids so enrichment can look up the exact release."""
        mb_response = {
            "recordings": [
                {
                    "id": "rec-mb-1",
                    "title": "Song",
                    "score": 95,
                    "releases": [{"id": "rel-1", "title": "Album", "date": "2024"}],
                }
            ],
        }
        with patch("tool.music.api.musicbrainz._session.get_json", return_value=mb_response):
            results = _fallback_search("/some/file.mp3", Metadata(title="Song"))
            assert len(results) == 1
            assert results[0].meta.musicbrainz_recordingid == "rec-mb-1"
            assert results[0].meta.musicbrainz_albumid == "rel-1"


def _mb_router(load_fixture):
    """Serve MusicBrainz fixtures by URL, recording every request."""
    served = {
        f"/release/{KAL_CD}": "mb_release_kalandra_cd.json",
        f"/release/{KAL_VINYL}": "mb_release_kalandra_vinyl.json",
        f"/recording/{KAL_REC}": "mb_recording_kalandra.json",
        "/recording/": "mb_search_kalandra.json",
    }
    calls: list[str] = []

    def get_json(url, **kwargs):
        calls.append(url)
        path = url.removeprefix("https://musicbrainz.org/ws/2")
        return load_fixture(served[path])

    return get_json, calls


def _args(path, **kw):
    return argparse.Namespace(**{"file": path, "yes": True, "force": False, "read": False, **kw})


def _pick(album_id):
    return lambda cands: next(i for i, c in enumerate(cands) if c.meta.musicbrainz_albumid == album_id)


@pytest.fixture
def legacy_flac(flac_file):
    """A file tagged the old way: descriptive fields only, plus unrelated tags and artwork."""
    from mutagen.flac import Picture

    audio = FLAC(flac_file)
    audio["TITLE"], audio["ARTIST"], audio["ALBUM"] = "It Gets Easier", "Kalandra", "The Line"
    audio["COMMENT"] = "from youtube"
    pic = Picture()
    pic.data, pic.type, pic.mime = b"\x89PNG-fake", 3, "image/png"
    audio.add_picture(pic)
    audio.save()
    return flac_file


class TestRunFlow:
    def run_tag(self, path, load_fixture, select, acoustid="acoustid_kalandra.json", fp=FINGERPRINT, **kw):
        mb_get, mb_calls = _mb_router(load_fixture)
        with (
            patch("tool.music.tag.get_audio_fingerprint", return_value=(306.4, fp)),
            patch("tool.music.api.acoustid._session.get_json", return_value=load_fixture(acoustid)) as ac_get,
            patch("tool.music.api.musicbrainz._session.get_json", side_effect=mb_get),
            patch("tool.music.tag._fallback_select", side_effect=select),
            patch.dict("os.environ", {"ACOUSTID_API_KEY": "k"}),
        ):
            run(_args(path, **kw))
        return ac_get, mb_calls

    def test_selected_release_is_written_with_full_identity(self, legacy_flac, load_fixture, tmp_path):
        ac_get, mb_calls = self.run_tag(legacy_flac, load_fixture, _pick(KAL_VINYL))

        assert ac_get.call_count == 1
        assert mb_calls == [f"https://musicbrainz.org/ws/2/release/{KAL_VINYL}"]  # one request total

        written = tmp_path / "It Gets Easier.flac"
        assert written.exists()
        tags = FLAC(str(written)).tags
        assert tags["MUSICBRAINZ_TRACKID"] == [KAL_REC]
        assert tags["MUSICBRAINZ_RELEASETRACKID"] == ["2aabd1c8-3793-4848-ba05-acf7ddd4b9ce"]
        assert tags["MUSICBRAINZ_ALBUMID"] == [KAL_VINYL]
        assert tags["MUSICBRAINZ_RELEASEGROUPID"] == ["4cda8183-371b-4189-bcb1-265bd0ff4b5f"]
        assert tags["ACOUSTID_ID"] == ["e8187d3a-71a8-49ef-8f95-2fa590426190"]
        assert tags["ACOUSTID_FINGERPRINT"] == [FINGERPRINT]
        assert tags["DISCNUMBER"] == ["2"]
        assert tags["TOTALDISCS"] == ["2"]
        assert tags["CATALOGNUMBER"] == ["BNM017LPB"]
        # unrelated tags and artwork survive
        assert tags["COMMENT"] == ["from youtube"]
        assert [p.data for p in FLAC(str(written)).pictures] == [b"\x89PNG-fake"]

    def test_other_release_gives_other_identity(self, legacy_flac, load_fixture, tmp_path):
        self.run_tag(legacy_flac, load_fixture, _pick(KAL_CD))
        meta = read_tags(str(tmp_path / "It Gets Easier.flac"))
        assert meta.musicbrainz_albumid == KAL_CD
        assert meta.musicbrainz_trackid == "82185c30-f97f-4b1b-8c70-a0bd3d077649"
        assert (meta.media, meta.discnumber) == ("CD", "1")

    def test_legacy_file_is_not_considered_complete(self, legacy_flac):
        assert not read_tags(legacy_flac).is_complete()

    def test_tagged_file_is_skipped_without_network(self, legacy_flac, load_fixture, tmp_path, capsys):
        self.run_tag(legacy_flac, load_fixture, _pick(KAL_CD))
        tagged = str(tmp_path / "It Gets Easier.flac")
        assert read_tags(tagged).is_complete()

        with patch("tool.music.tag.get_audio_fingerprint") as fp:
            run(_args(tagged))
        fp.assert_not_called()
        assert "Already identified" in capsys.readouterr().out

    def test_force_refreshes_a_tagged_file(self, legacy_flac, load_fixture, tmp_path):
        self.run_tag(legacy_flac, load_fixture, _pick(KAL_CD))
        tagged = str(tmp_path / "It Gets Easier.flac")
        ac_get, _ = self.run_tag(tagged, load_fixture, _pick(KAL_VINYL), force=True)
        assert ac_get.call_count == 1
        assert read_tags(tagged).musicbrainz_albumid == KAL_VINYL

    def test_optional_fields_never_cause_refetch(self, flac_file):
        # a recording-only match has no release, label, catalog number, ...; still complete
        write_tags(flac_file, Metadata(title="T", artist="A", musicbrainz_recordingid=KAL_REC))
        assert read_tags(flac_file).is_complete()
        # ...but a file claiming an album without release identity is not
        write_tags(flac_file, Metadata(album="Alb", musicbrainz_albumid=KAL_CD))
        assert not read_tags(flac_file).is_complete()

    def test_fallback_search_resolves_to_same_release_aware_flow(self, legacy_flac, load_fixture, tmp_path):
        no_matches = {"status": "ok", "results": []}
        mb_get, mb_calls = _mb_router(load_fixture)
        with (
            patch("tool.music.tag.get_audio_fingerprint", return_value=(306.4, FINGERPRINT)),
            patch("tool.music.api.acoustid._session.get_json", return_value=no_matches),
            patch("tool.music.api.musicbrainz._session.get_json", side_effect=mb_get),
            patch("tool.music.tag._fallback_select", side_effect=_pick(KAL_VINYL)),
            patch.dict("os.environ", {"ACOUSTID_API_KEY": "k"}),
        ):
            run(_args(legacy_flac))

        assert len(mb_calls) == 2  # one search + one release lookup
        meta = read_tags(str(tmp_path / "It Gets Easier.flac"))
        assert meta.musicbrainz_albumid == KAL_VINYL
        assert meta.musicbrainz_recordingid == KAL_REC
        assert meta.musicbrainz_trackid == "2aabd1c8-3793-4848-ba05-acf7ddd4b9ce"
        assert meta.acoustid_id is None  # no AcoustID match, so none is claimed
        assert meta.acoustid_fingerprint == FINGERPRINT


class TestAutotag:
    @staticmethod
    def run_autotag(path, load_fixture, acoustid="acoustid_kalandra.json", edit=None):
        results = load_fixture(acoustid)
        if edit:
            edit(results)
        mb_get, mb_calls = _mb_router(load_fixture)
        with (
            patch("tool.music.tag.get_audio_fingerprint", return_value=(306.4, FINGERPRINT)),
            patch("tool.music.api.acoustid._session.get_json", return_value=results),
            patch("tool.music.api.musicbrainz._session.get_json", side_effect=mb_get),
            patch.dict("os.environ", {"ACOUSTID_API_KEY": "k"}),
        ):
            return autotag(path), mb_calls

    @staticmethod
    def only_release(results):
        """Make the match a single release (the CD), as for a rarely released recording."""
        rg = results["results"][0]["recordings"][0]["releasegroups"][0]
        rg["releases"] = [r for r in rg["releases"] if r["id"] == KAL_CD]

    def test_ambiguous_release_writes_recording_identity_only(self, legacy_flac, load_fixture):
        meta, calls = self.run_autotag(legacy_flac, load_fixture)

        assert calls == [f"https://musicbrainz.org/ws/2/recording/{KAL_REC}"]  # one request, no release picked
        assert meta is not None
        tags = read_tags(legacy_flac)
        assert tags.musicbrainz_recordingid == KAL_REC
        assert tags.musicbrainz_artistid == ["7c67fb96-7e24-4893-a020-fcc53a1f7814"]
        assert tags.acoustid_id == "e8187d3a-71a8-49ef-8f95-2fa590426190"
        assert tags.acoustid_fingerprint == FINGERPRINT
        assert tags.musicbrainz_albumid is None
        assert tags.musicbrainz_releasegroupid is None
        assert tags.album == "The Line"  # the file's own album is left alone
        assert not tags.is_complete()  # `tag` still has the release to ask about

    def test_sole_release_with_matching_album_writes_release_identity(self, legacy_flac, load_fixture):
        meta, calls = self.run_autotag(legacy_flac, load_fixture, edit=self.only_release)

        assert calls == [f"https://musicbrainz.org/ws/2/release/{KAL_CD}"]
        assert meta is not None
        tags = read_tags(legacy_flac)
        assert tags.musicbrainz_albumid == KAL_CD
        assert tags.musicbrainz_trackid == "82185c30-f97f-4b1b-8c70-a0bd3d077649"
        assert tags.is_complete()

    def test_sole_release_with_different_album_is_not_overwritten(self, legacy_flac, load_fixture):
        audio = FLAC(legacy_flac)
        audio["ALBUM"] = "Some Single"
        audio.save()

        self.run_autotag(legacy_flac, load_fixture, edit=self.only_release)
        tags = read_tags(legacy_flac)
        assert tags.album == "Some Single"
        assert tags.musicbrainz_albumid is None
        assert tags.musicbrainz_recordingid == KAL_REC

    def test_file_without_album_gets_the_sole_release(self, flac_file, load_fixture):
        self.run_autotag(flac_file, load_fixture, edit=self.only_release)
        assert read_tags(flac_file).musicbrainz_albumid == KAL_CD

    def test_unrelated_tags_and_artwork_survive(self, legacy_flac, load_fixture):
        self.run_autotag(legacy_flac, load_fixture)
        audio = FLAC(legacy_flac)
        assert audio["COMMENT"] == ["from youtube"]
        assert [p.data for p in audio.pictures] == [b"\x89PNG-fake"]

    def test_low_score_is_left_alone(self, legacy_flac, load_fixture):
        def weaken(results):
            results["results"][0]["score"] = 0.6

        meta, calls = self.run_autotag(legacy_flac, load_fixture, edit=weaken)
        assert meta is None
        assert calls == []
        assert read_tags(legacy_flac).musicbrainz_recordingid is None

    def test_two_strong_recordings_are_ambiguous(self, legacy_flac, load_fixture):
        def add_second_recording(results):
            other = {"id": "00000000-0000-0000-0000-000000000000", "title": "Other"}
            results["results"].append({"id": "x", "score": 0.95, "recordings": [other]})

        meta, calls = self.run_autotag(legacy_flac, load_fixture, edit=add_second_recording)
        assert meta is None
        assert calls == []

    def test_no_matches(self, legacy_flac, load_fixture):
        meta, calls = self.run_autotag(legacy_flac, load_fixture, edit=lambda r: r["results"].clear())
        assert meta is None
        assert calls == []

    def test_unwritable_format_is_skipped_before_any_work(self, wav_file):
        with patch("tool.music.tag.get_audio_fingerprint") as fp:
            assert autotag(wav_file) is None
        fp.assert_not_called()

    def test_failed_musicbrainz_lookup_writes_nothing(self, legacy_flac, load_fixture):
        with (
            patch("tool.music.tag.get_audio_fingerprint", return_value=(306.4, FINGERPRINT)),
            patch("tool.music.api.acoustid._session.get_json", return_value=load_fixture("acoustid_kalandra.json")),
            patch("tool.music.api.musicbrainz._session.get_json", side_effect=Exception("down")),
            patch.dict("os.environ", {"ACOUSTID_API_KEY": "k"}),
        ):
            assert autotag(legacy_flac) is None
        assert read_tags(legacy_flac).musicbrainz_recordingid is None
