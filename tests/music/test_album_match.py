"""Folder-level release matching; no real network calls."""

import argparse
from pathlib import Path

from tool.music import album
from tool.music.api.musicbrainz import ReleaseSummary, ReleaseTrack, ReleaseTracklist
from tool.music.metadata import Metadata


def summary(
    *,
    release: str = "rel",
    title: str = "Album",
    group: str | None = "rg",
    count: int = 3,
    date: str = "2020-01-01",
    country: str = "XW",
    media: tuple[str | None, ...] = ("Digital Media",),
    status: str = "Official",
) -> ReleaseSummary:
    return ReleaseSummary(
        id=release,
        title=title,
        artist="Artist",
        group_id=group,
        date=date,
        country=country,
        status=status,
        types=["album"],
        media=list(media),
        track_count=count,
    )


def track(disc: str, number: str, title: str) -> ReleaseTrack:
    return ReleaseTrack(
        disc_number=disc,
        track_number=number,
        title=title,
        recording_id=f"rec-{disc}-{number}",
        track_id=f"track-{disc}-{number}",
    )


def meta(
    title: str,
    *,
    number: str | None = None,
    disc: str | None = None,
    album: str | None = "Album",
    artist: str = "Artist",
    albumartist: str | None = "Artist",
) -> Metadata:
    return Metadata(
        title=title,
        artist=artist,
        albumartist=albumartist,
        album=album,
        tracknumber=number,
        discnumber=disc,
    )


def args(path, **kwargs):
    defaults = {"paths": [str(path)], "dry_run": False}
    return argparse.Namespace(**(defaults | kwargs))


def wire(monkeypatch, tmp_path, metas, hits, tracks, written):
    for name in metas:
        (tmp_path / name).touch()

    def fake_read(path):
        return metas[Path(path).name]

    def fake_enrich(selection):
        return Metadata(
            title=selection.title,
            artist=selection.artist,
            album="Album",
            musicbrainz_recordingid=selection.musicbrainz_recordingid,
            musicbrainz_albumid=selection.musicbrainz_albumid,
            musicbrainz_releasegroupid=selection.musicbrainz_releasegroupid,
            musicbrainz_trackid=selection.musicbrainz_trackid,
        )

    monkeypatch.setattr(album, "read_tags", fake_read)
    monkeypatch.setattr(album, "search_releases", lambda a, b: hits)
    monkeypatch.setattr(album, "release_tracklist", lambda s: ReleaseTracklist(s, tracks))
    monkeypatch.setattr(album, "enrich", fake_enrich)
    monkeypatch.setattr(album, "write_tags", lambda path, m: written.append((Path(path).name, m)))


class TestFolderAlbum:
    def test_exact_tags(self):
        metas = [meta("A", number="1"), meta("B", number="2")]
        assert album.folder_album(metas) == ("Album", "Artist")

    def test_mixed_albums_are_rejected(self):
        metas = [meta("A", album="One"), meta("B", album="Two")]
        assert album.folder_album(metas) is None

    def test_missing_album_is_rejected(self):
        assert album.folder_album([meta("A", album=None)]) is None

    def test_track_artists_fall_back_when_no_album_artist(self):
        metas = [
            meta("A", albumartist=None, artist="Artist"),
            meta("B", albumartist=None, artist="Artist"),
        ]
        assert album.folder_album(metas) == ("Album", "Artist")


class TestMapTracks:
    def test_disc_and_track_numbers(self):
        metas = [meta("One", number="1", disc="1"), meta("Two", number="2", disc="1")]
        tracks = [track("1", "1", "One"), track("1", "2", "Two")]
        assert [t.track_id for t in album.map_tracks(metas, tracks)] == ["track-1-1", "track-1-2"]

    def test_track_numbers_without_discs_on_a_single_medium(self):
        metas = [meta("One", number="2"), meta("Two", number="1")]
        tracks = [track("1", "1", "Two"), track("1", "2", "One")]
        assert [t.title for t in album.map_tracks(metas, tracks)] == ["One", "Two"]

    def test_titles_when_numbers_are_absent(self):
        metas = [meta("Second  Song"), meta("First Song")]
        tracks = [track("1", "1", "First Song"), track("1", "2", "Second Song")]
        assert [t.title for t in album.map_tracks(metas, tracks)] == ["Second Song", "First Song"]

    def test_titles_win_over_misnumbered_files(self):
        metas = [meta("Second", number="1"), meta("First", number="2")]
        tracks = [track("1", "1", "First"), track("1", "2", "Second")]
        assert [t.title for t in album.map_tracks(metas, tracks)] == ["Second", "First"]

    def test_duplicate_titles_do_not_map(self):
        metas = [meta("Song"), meta("Song")]
        tracks = [track("1", "1", "Song"), track("1", "2", "Song")]
        assert album.map_tracks(metas, tracks) is None

    def test_unmapped_file_is_left_alone(self):
        metas = [meta("One", number="1"), meta("Other", number="9")]
        tracks = [track("1", "1", "One"), track("1", "2", "Two")]
        assert album.map_tracks(metas, tracks) is None


class TestRun:
    def test_writes_every_pending_track(self, tmp_path, monkeypatch, capsys):
        metas = {
            "01.flac": meta("One", number="1"),
            "02.flac": meta("Two", number="2"),
            "03.flac": meta("Three", number="3"),
        }
        tracks = [track("1", "1", "One"), track("1", "2", "Two"), track("1", "3", "Three")]
        written = []
        wire(monkeypatch, tmp_path, metas, [summary()], tracks, written)

        album.run(args(tmp_path))

        assert [name for name, _ in written] == ["01.flac", "02.flac", "03.flac"]
        assert written[0][1].musicbrainz_trackid == "track-1-1"
        assert "1 folders matched (3 files)" in capsys.readouterr().out

    def test_two_release_groups_stay_untouched(self, tmp_path, monkeypatch, capsys):
        metas = {"01.flac": meta("One", number="1")}
        written = []
        wire(
            monkeypatch,
            tmp_path,
            metas,
            [summary(release="a", group="rg-a", count=1), summary(release="b", group="rg-b", count=1)],
            [track("1", "1", "One")],
            written,
        )

        album.run(args(tmp_path))

        assert written == []
        assert "1 ambiguous" in capsys.readouterr().out

    def test_partial_folder_matches_when_positions_and_titles_agree(self, tmp_path, monkeypatch):
        metas = {"01.flac": meta("One", number="1")}
        written = []
        wire(monkeypatch, tmp_path, metas, [summary(count=10)], [track("1", "1", "One")], written)

        album.run(args(tmp_path))

        assert [name for name, _ in written] == ["01.flac"]

    def test_partial_folder_with_a_different_title_is_left_alone(self, tmp_path, monkeypatch):
        metas = {"01.flac": meta("Something Else", number="1")}
        written = []
        wire(monkeypatch, tmp_path, metas, [summary(count=10)], [track("1", "1", "One")], written)

        album.run(args(tmp_path))

        assert written == []

    def test_release_shorter_than_the_folder_is_not_matched(self, tmp_path, monkeypatch):
        metas = {"01.flac": meta("One", number="1"), "02.flac": meta("Two", number="2")}
        written = []
        wire(monkeypatch, tmp_path, metas, [summary(count=1)], [track("1", "1", "One")], written)

        album.run(args(tmp_path))

        assert written == []

    def test_complete_files_are_not_rewritten(self, tmp_path, monkeypatch):
        metas = {
            "01.flac": Metadata(
                title="One",
                artist="Artist",
                album="Album",
                tracknumber="1",
                musicbrainz_recordingid="rec",
                musicbrainz_albumid="rel",
                musicbrainz_releasegroupid="rg",
            ),
            "02.flac": meta("Two", number="2"),
        }
        tracks = [track("1", "1", "One"), track("1", "2", "Two")]
        written = []
        wire(monkeypatch, tmp_path, metas, [summary(count=2)], tracks, written)

        album.run(args(tmp_path))

        assert [name for name, _ in written] == ["02.flac"]

    def test_unrelated_complete_files_do_not_block_the_match(self, tmp_path, monkeypatch):
        metas = {
            "01.flac": Metadata(
                title="Some Other Song",
                artist="Other",
                album="Another Album",
                musicbrainz_recordingid="other-rec",
                musicbrainz_albumid="other-rel",
                musicbrainz_releasegroupid="other-rg",
            ),
            "02.flac": meta("Two", number="2"),
        }
        written = []
        wire(monkeypatch, tmp_path, metas, [summary(count=10)], [track("1", "2", "Two")], written)
        monkeypatch.setattr(album, "read_tags", lambda path: metas[Path(path).name])

        album.run(args(tmp_path))

        assert [name for name, _ in written] == ["02.flac"]

    def test_dry_run_prints_proposal_and_writes_nothing(self, tmp_path, monkeypatch, capsys):
        metas = {"01.flac": meta("One", number="1")}
        written = []
        wire(monkeypatch, tmp_path, metas, [summary(count=1)], [track("1", "1", "One")], written)

        album.run(args(tmp_path, dry_run=True))

        assert written == []
        out = capsys.readouterr().out
        assert "would match" in out
        assert "01.flac -> 1.1 One" in out
