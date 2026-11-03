import argparse

from tool.music import resolve
from tool.music.metadata import Candidate, Metadata
from tool.music.resolve import album_score, choose_release, run


def candidate(
    *,
    album: str = "Album",
    group: str = "rg",
    release: str = "rel",
    track: str = "1",
    disc: str = "1",
    media: str = "Digital Media",
    date: str = "2020-01-01",
    country: str = "XW",
    status: str = "Official",
) -> Candidate:
    return Candidate(
        1.0,
        Metadata(
            title="Song",
            musicbrainz_recordingid="rec",
            album=album,
            musicbrainz_albumid=release,
            musicbrainz_releasegroupid=group,
            musicbrainz_trackid=f"{release}-t{track}",
            tracknumber=track,
            discnumber=disc,
            media=media,
            date=date,
            releasecountry=country,
            releasestatus=status,
            releasetype=["album"],
        ),
    )


def meta(album: str | None = "Album", track: str | None = "1") -> Metadata:
    return Metadata(title="Song", artist="Artist", album=album, tracknumber=track, musicbrainz_recordingid="rec")


class TestAlbumScore:
    def test_exact(self):
        assert album_score("Album", "Album") == 3

    def test_artist_prefix(self):
        assert album_score("Artist: Album", "Album") == 2

    def test_artist_suffix(self):
        assert album_score("Album - Artist", "Album") == 2

    def test_edition_suffix(self):
        assert album_score("Album (Deluxe Edition)", "Album") == 2

    def test_separator_and_bracket_variants_match(self):
        assert (
            album_score(
                "Broadchurch - The Final Chapter (Music From The Original TV Series)",
                "Broadchurch: The Final Chapter",
            )
            == 2
        )

    def test_labelled_soundtrack_variant_matches_as_edition(self):
        assert (
            album_score(
                "The Lost Flowers of Alice Hart (Prime Video Original Series Soundtrack)",
                "The Lost Flowers of Alice Hart: Prime Video Original Series Soundtrack",
            )
            == 1
        )

    def test_a_different_release_title_is_not_an_edition(self):
        assert album_score("Album", "Album Remixes") == 0
        assert album_score("Album", "Album II") == 0

    def test_unrelated(self):
        assert album_score("Other", "Album") == 0


class TestChooseRelease:
    def test_prefers_digital_media_for_a_file_without_media(self):
        cd = candidate(media="CD", release="cd", country="GB")
        digital = candidate(media="Digital Media", release="digital", country="XW")
        assert choose_release(meta(), [cd, digital]) is not None
        assert choose_release(meta(), [cd, digital]).meta.musicbrainz_albumid == "digital"

    def test_prefers_matching_media_when_the_file_names_one(self):
        cd = candidate(media="CD", release="cd")
        digital = candidate(media="Digital Media", release="digital")
        chosen = choose_release(
            Metadata(
                title="Song",
                artist="Artist",
                album="Album",
                media="CD",
                musicbrainz_recordingid="rec",
            ),
            [digital, cd],
        )
        assert chosen is not None
        assert chosen.meta.musicbrainz_albumid == "cd"

    def test_ambiguous_release_group_is_left_alone(self):
        assert choose_release(meta(), [candidate(group="a"), candidate(group="b")]) is None

    def test_track_number_selects_the_matching_edition(self):
        first = candidate(track="1", release="a")
        second = candidate(track="2", release="b")
        chosen = choose_release(meta(track="2"), [first, second])
        assert chosen is not None
        assert chosen.meta.musicbrainz_trackid == "b-t2"

    def test_track_mismatch_falls_back_to_a_single_release_group(self):
        chosen = choose_release(meta(track="9"), [candidate(track="1")])
        assert chosen is not None
        assert chosen.meta.musicbrainz_trackid == "rel-t1"

    def test_album_mismatch_with_one_release_group_is_resolved(self):
        chosen = choose_release(meta(album="Other"), [candidate()])
        assert chosen is not None
        assert chosen.meta.musicbrainz_albumid == "rel"

    def test_without_album_a_sole_release_group_supplies_it(self):
        chosen = choose_release(meta(album=None), [candidate()])
        assert chosen is not None
        assert chosen.meta.musicbrainz_albumid == "rel"

    def test_without_album_several_release_groups_stay_ambiguous(self):
        assert choose_release(meta(album=None), [candidate(group="a"), candidate(group="b")]) is None

    def test_official_release_wins(self):
        bootleg = candidate(release="bootleg", status="Bootleg")
        official = candidate(release="official", status="Official")
        chosen = choose_release(meta(), [bootleg, official])
        assert chosen is not None
        assert chosen.meta.musicbrainz_albumid == "official"

    def test_artist_prefixed_album_matches(self):
        chosen = choose_release(meta(album="Artist: Album"), [candidate()])
        assert chosen is not None
        assert chosen.meta.musicbrainz_albumid == "rel"


class TestResolveFile:
    def test_writes_enriched_release_identity(self, monkeypatch):
        selected = MetaCapture()
        monkeypatch.setattr(resolve, "read_tags", lambda _: meta())
        monkeypatch.setattr(resolve, "release_candidates", lambda _: [candidate()])
        monkeypatch.setattr(resolve, "enrich", lambda m: selected(m))
        monkeypatch.setattr(resolve, "write_tags", lambda path, m: selected.written.append((path, m)))

        result = resolve.resolve_file("song.flac")

        assert result is not None
        assert selected.written[0][0] == "song.flac"
        assert selected.written[0][1].musicbrainz_trackid == "rel-t1"

    def test_does_not_write_when_enrichment_lacks_a_track(self, monkeypatch):
        monkeypatch.setattr(resolve, "read_tags", lambda _: meta())
        monkeypatch.setattr(resolve, "release_candidates", lambda _: [candidate()])
        monkeypatch.setattr(
            resolve,
            "enrich",
            lambda m: Metadata(
                title="Song",
                artist="Artist",
                album="Album",
                musicbrainz_recordingid="rec",
                musicbrainz_albumid="rel",
                musicbrainz_releasegroupid="rg",
            ),
        )
        written = []
        monkeypatch.setattr(resolve, "write_tags", lambda path, m: written.append(path))

        assert resolve.resolve_file("song.flac") is None
        assert written == []


class MetaCapture:
    def __init__(self):
        self.written = []

    def __call__(self, m: Metadata) -> Metadata:
        return Metadata(
            title="Song",
            artist="Artist",
            album="Album",
            musicbrainz_recordingid="rec",
            musicbrainz_albumid="rel",
            musicbrainz_releasegroupid="rg",
            musicbrainz_trackid="rel-t1",
        )


class TestRun:
    @staticmethod
    def args(tmp_path, **kwargs):
        defaults = {"paths": [str(tmp_path)], "dry_run": False}
        return argparse.Namespace(**(defaults | kwargs))

    def test_dry_run_lists_files_awaiting_a_release(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "a.mp3").touch()
        (tmp_path / "b.mp3").touch()
        (tmp_path / "c.mp3").touch()
        meta_by_path = {
            str(tmp_path / "a.mp3"): meta(),
            str(tmp_path / "b.mp3"): Metadata(
                title="Complete",
                artist="A",
                album="Album",
                musicbrainz_recordingid="r",
                musicbrainz_albumid="rel",
                musicbrainz_releasegroupid="rg",
            ),
            str(tmp_path / "c.mp3"): Metadata(title="Single", artist="A", musicbrainz_recordingid="r"),
        }
        monkeypatch.setattr(resolve, "read_tags", lambda p: meta_by_path[p])

        run(self.args(tmp_path, dry_run=True))

        out = capsys.readouterr().out
        assert "a.mp3" in out
        assert "b.mp3" not in out
        assert "c.mp3" in out
        assert "2 files would be resolved." in out

    def test_summary_counts(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "a.mp3").touch()
        monkeypatch.setattr(resolve, "read_tags", lambda p: meta())
        resolved = Metadata(
            title="Song",
            artist="Artist",
            album="Album",
            media="Digital Media",
            releasecountry="XW",
            date="2020-01-01",
            musicbrainz_recordingid="rec",
            musicbrainz_albumid="rel",
            musicbrainz_releasegroupid="rg",
            musicbrainz_trackid="t1",
        )
        monkeypatch.setattr(resolve, "resolve_file", lambda p: resolved)

        run(self.args(tmp_path))

        out = capsys.readouterr().out
        assert "1 resolved" in out
        assert "[Album · Digital Media, XW, 2020-01-01]" in out

    def test_added_album_is_reported(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "a.mp3").touch()
        monkeypatch.setattr(resolve, "read_tags", lambda p: Metadata(title="Song", artist="A", musicbrainz_recordingid="rec"))
        resolved = Metadata(
            title="Song",
            artist="A",
            album="Album",
            musicbrainz_recordingid="rec",
            musicbrainz_albumid="rel",
            musicbrainz_releasegroupid="rg",
            musicbrainz_trackid="t1",
        )
        monkeypatch.setattr(resolve, "resolve_file", lambda p: resolved)

        run(self.args(tmp_path))

        assert "(album tag added)" in capsys.readouterr().out

    def test_replaced_album_is_reported(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "a.mp3").touch()
        monkeypatch.setattr(resolve, "read_tags", lambda p: meta(album="Wrong Album"))
        resolved = Metadata(
            title="Song",
            artist="Artist",
            album="Canonical Album",
            musicbrainz_recordingid="rec",
            musicbrainz_albumid="rel",
            musicbrainz_releasegroupid="rg",
            musicbrainz_trackid="t1",
        )
        monkeypatch.setattr(resolve, "resolve_file", lambda p: resolved)

        run(self.args(tmp_path))

        assert "(album tag replaced)" in capsys.readouterr().out
