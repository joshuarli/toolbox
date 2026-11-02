import argparse
import json
from pathlib import Path

from tool.music import audit
from tool.music.audit import FileAudit, State, audit_file, classify, report, run
from tool.music.metadata import Metadata


def complete_meta() -> Metadata:
    return Metadata(
        title="Song",
        artist="Artist",
        album="Album",
        musicbrainz_recordingid="rec",
        musicbrainz_albumid="release",
        musicbrainz_releasegroupid="group",
    )


class TestClassify:
    def test_complete_without_album(self):
        assert classify(Metadata(title="Song", artist="Artist", musicbrainz_recordingid="rec")) is State.COMPLETE

    def test_complete_with_album_identity(self):
        assert classify(complete_meta()) is State.COMPLETE

    def test_missing_album_identity(self):
        meta = Metadata(title="Song", artist="Artist", album="Album", musicbrainz_recordingid="rec")
        assert classify(meta) is State.MISSING_ALBUM

    def test_missing_recording_identity(self):
        assert classify(Metadata(title="Song", artist="Artist", album="Album")) is State.MISSING_IDENTITY

    def test_no_identity_without_album_claim(self):
        assert classify(Metadata(title="Song", artist="Artist")) is State.MISSING_IDENTITY

    def test_untagged(self):
        assert classify(Metadata()) is State.UNTAGGED

    def test_album_only_is_partial(self):
        assert classify(Metadata(album="Album")) is State.PARTIAL

    def test_title_only_is_partial(self):
        assert classify(Metadata(title="Song")) is State.PARTIAL

    def test_missing_identity_wins_over_partial_identity(self):
        meta = Metadata(title="Song", artist="Artist", musicbrainz_albumid="release")
        assert classify(meta) is State.MISSING_IDENTITY


class TestAuditFile:
    def test_uses_display_path_for_group_and_format(self, tmp_path, monkeypatch):
        f = tmp_path / "Album" / "track.flac"
        f.parent.mkdir()
        f.touch()
        monkeypatch.setattr(audit, "read_tags", lambda _: Metadata(title="Song", artist="Artist"))
        monkeypatch.setattr(audit, "is_writable", lambda _: True)

        row = audit_file(f, "Album/track.flac")

        assert row.group == "Album"
        assert row.format == "flac"
        assert row.state is State.MISSING_IDENTITY
        assert row.writable is True

    def test_file_at_root_uses_dot_group(self, tmp_path, monkeypatch):
        f = tmp_path / "track.mp3"
        f.touch()
        monkeypatch.setattr(audit, "read_tags", lambda _: Metadata())
        monkeypatch.setattr(audit, "is_writable", lambda _: True)

        row = audit_file(f, "track.mp3")

        assert row.group == "."
        assert row.state is State.UNTAGGED


def _row(path: str, state: State, *, writable: bool = True) -> FileAudit:
    group = Path(path).parts[0] if len(Path(path).parts) > 1 else "."
    return FileAudit(
        path=path,
        group=group,
        format=Path(path).suffix.lstrip("."),
        state=state,
        writable=writable,
        title=state is not State.UNTAGGED,
        artist=state is not State.UNTAGGED,
        album=state is not State.UNTAGGED,
        recording_id=state in (State.COMPLETE, State.MISSING_ALBUM),
        release_id=state is State.COMPLETE,
    )


class TestReport:
    def test_summarizes_states_formats_and_groups(self, capsys):
        rows = [
            _row("Albums/a.mp3", State.COMPLETE),
            _row("Albums/b.mp3", State.MISSING_IDENTITY),
            _row("WIP/c.webm", State.UNTAGGED, writable=False),
        ]

        report(rows, ["/music"])

        out = capsys.readouterr().out
        assert "3 files — 1 complete (33.3%), 2 need work; 1 not taggable in place" in out
        assert "missing-identity" in out
        assert "webm" in out
        assert "WIP" in out
        assert "NEEDS WORK (2)" in out
        assert "(convert: webm is not taggable in place)" in out

    def test_complete_library_omits_work_list(self, capsys):
        report([_row("a.mp3", State.COMPLETE)], ["/music"])

        out = capsys.readouterr().out
        assert "1 complete (100.0%)" in out
        assert "NEEDS WORK" not in out


class TestRun:
    def test_json_reports_counts_and_file_states(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "Album").mkdir()
        (tmp_path / "Album" / "a.flac").touch()
        (tmp_path / "b.mp3").touch()

        def fake_read(path: str) -> Metadata:
            if path.endswith("a.flac"):
                return complete_meta()
            return Metadata(title="Song", artist="Artist")

        monkeypatch.setattr(audit, "read_tags", fake_read)
        monkeypatch.setattr(audit, "is_writable", lambda _: True)

        run(argparse.Namespace(paths=[str(tmp_path)], jobs=1, json=True))

        payload = json.loads(capsys.readouterr().out)
        assert payload["counts"] == {
            "complete": 1,
            "missing-identity": 1,
            "missing-album": 0,
            "untagged": 0,
            "partial": 0,
        }
        by_path = {f["path"]: f for f in payload["files"]}
        assert by_path["Album/a.flac"]["state"] == "complete"
        assert by_path["Album/a.flac"]["group"] == "Album"
        assert by_path["b.mp3"]["state"] == "missing-identity"
        assert by_path["b.mp3"]["group"] == "."

    def test_empty_directory(self, tmp_path, capsys):
        run(argparse.Namespace(paths=[str(tmp_path)], jobs=1, json=False))

        assert "No audio files found." in capsys.readouterr().out
