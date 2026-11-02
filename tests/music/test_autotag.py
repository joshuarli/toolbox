import argparse

import pytest

from tool.music import autotag as queue
from tool.music.api.acoustid import AcoustIDError
from tool.music.autotag import Outcome, process, run
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


class TestProcess:
    def test_complete_file_is_left_alone(self, monkeypatch):
        monkeypatch.setattr(queue, "read_tags", lambda _: complete_meta())
        called = []
        monkeypatch.setattr(queue, "autotag", lambda _: called.append(_))

        outcome = process("song.flac", delete_source=False)

        assert outcome.status == "already-complete"
        assert called == []

    def test_writable_file_is_autotagged(self, monkeypatch):
        monkeypatch.setattr(queue, "read_tags", lambda _: Metadata(title="Song", artist="Artist"))
        monkeypatch.setattr(queue, "is_writable", lambda _: True)
        monkeypatch.setattr(queue, "autotag", lambda _: complete_meta())

        outcome = process("song.flac", delete_source=False)

        assert outcome.status == "tagged-release"
        assert outcome.artist == "Artist"
        assert outcome.title == "Song"

    def test_recording_only_match_is_reported_separately(self, monkeypatch):
        written = Metadata(title="Song", artist="Artist", musicbrainz_recordingid="rec")
        monkeypatch.setattr(queue, "read_tags", lambda _: Metadata(title="Song", artist="Artist"))
        monkeypatch.setattr(queue, "is_writable", lambda _: True)
        monkeypatch.setattr(queue, "autotag", lambda _: written)

        outcome = process("song.flac", delete_source=False)

        assert outcome.status == "tagged-recording"

    def test_ambiguous_match_needs_manual(self, monkeypatch):
        monkeypatch.setattr(queue, "read_tags", lambda _: Metadata(title="Song", artist="Artist"))
        monkeypatch.setattr(queue, "is_writable", lambda _: True)
        monkeypatch.setattr(queue, "autotag", lambda _: None)

        assert process("song.flac", delete_source=False).status == "needs-manual"

    def test_fingerprint_failure_is_isolated(self, monkeypatch):
        def fail(_):
            raise SystemExit(1)

        monkeypatch.setattr(queue, "read_tags", lambda _: Metadata(title="Song", artist="Artist"))
        monkeypatch.setattr(queue, "is_writable", lambda _: True)
        monkeypatch.setattr(queue, "autotag", fail)

        assert process("song.flac", delete_source=False).status == "failed"

    def test_unconvertible_container_needs_conversion(self, monkeypatch):
        monkeypatch.setattr(queue, "read_tags", lambda _: Metadata(title="Song", artist="Artist"))
        monkeypatch.setattr(queue, "is_writable", lambda _: False)
        monkeypatch.setattr(queue, "remux_extension", lambda _: None)
        called = []
        monkeypatch.setattr(queue, "autotag", lambda _: called.append(_))

        outcome = process("song.wav", delete_source=False)

        assert outcome.status == "needs-conversion"
        assert called == []

    def test_existing_remux_is_reused(self, tmp_path, monkeypatch):
        src = tmp_path / "song.webm"
        dst = tmp_path / "song.opus"
        src.touch()
        dst.touch()
        monkeypatch.setattr(queue, "read_tags", lambda _: Metadata(title="Song", artist="Artist"))
        monkeypatch.setattr(queue, "is_writable", lambda _: False)
        monkeypatch.setattr(queue, "remux_extension", lambda _: ".opus")
        monkeypatch.setattr(queue, "transcode_destination", lambda _: str(dst))
        monkeypatch.setattr(queue, "transcode", lambda _: pytest.fail("should reuse existing output"))
        monkeypatch.setattr(queue, "autotag", lambda p: complete_meta())

        outcome = process(str(src), delete_source=False)

        assert outcome.target == str(dst)
        assert outcome.remuxed is True

    def test_missing_remux_is_created_and_source_kept(self, tmp_path, monkeypatch):
        src = tmp_path / "song.webm"
        dst = tmp_path / "song.opus"
        src.touch()
        calls = []

        def fake_transcode(path):
            calls.append(path)
            dst.touch()
            return str(dst)

        monkeypatch.setattr(queue, "read_tags", lambda _: Metadata(title="Song", artist="Artist"))
        monkeypatch.setattr(queue, "is_writable", lambda _: False)
        monkeypatch.setattr(queue, "remux_extension", lambda _: ".opus")
        monkeypatch.setattr(queue, "transcode_destination", lambda _: str(dst))
        monkeypatch.setattr(queue, "transcode", fake_transcode)
        monkeypatch.setattr(queue, "autotag", lambda p: complete_meta())

        outcome = process(str(src), delete_source=False)

        assert outcome.remuxed is True
        assert calls == [str(src)]
        assert src.exists()

    def test_delete_source_requires_a_verified_remux(self, tmp_path, monkeypatch):
        src = tmp_path / "song.webm"
        dst = tmp_path / "song.opus"
        src.touch()
        dst.touch()
        monkeypatch.setattr(queue, "read_tags", lambda _: Metadata(title="Song", artist="Artist"))
        monkeypatch.setattr(queue, "is_writable", lambda _: False)
        monkeypatch.setattr(queue, "remux_extension", lambda _: ".opus")
        monkeypatch.setattr(queue, "transcode_destination", lambda _: str(dst))
        monkeypatch.setattr(queue, "probe_audio", lambda _: {"codec_name": "opus", "channels": 2})
        monkeypatch.setattr(queue, "autotag", lambda p: complete_meta())

        process(str(src), delete_source=True)

        assert not src.exists()
        assert dst.exists()

    def test_delete_source_keeps_source_when_remux_cannot_be_probed(self, tmp_path, monkeypatch):
        src = tmp_path / "song.webm"
        dst = tmp_path / "song.opus"
        src.touch()
        dst.touch()
        monkeypatch.setattr(queue, "read_tags", lambda _: Metadata(title="Song", artist="Artist"))
        monkeypatch.setattr(queue, "is_writable", lambda _: False)
        monkeypatch.setattr(queue, "remux_extension", lambda _: ".opus")
        monkeypatch.setattr(queue, "transcode_destination", lambda _: str(dst))
        monkeypatch.setattr(queue, "probe_audio", lambda _: None)

        outcome = process(str(src), delete_source=True)

        assert outcome.status == "failed"
        assert src.exists()


class TestRun:
    @staticmethod
    def args(tmp_path, **kwargs):
        defaults = {"paths": [str(tmp_path)], "dry_run": False, "delete_source": False}
        return argparse.Namespace(**(defaults | kwargs))

    def test_dry_run_lists_queue_without_preflight(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "a.flac").write_bytes(b"x")
        (tmp_path / "b.webm").write_bytes(b"x")
        monkeypatch.setattr(queue, "read_tags", lambda p: Metadata(title="Song", artist="Artist"))
        monkeypatch.setattr(queue, "is_writable", lambda p: p.endswith(".flac"))
        monkeypatch.setattr(queue, "remux_extension", lambda p: ".opus")
        monkeypatch.setattr(queue, "ensure_acoustid_key", lambda: pytest.fail("dry run must not touch secrets"))

        run(self.args(tmp_path, dry_run=True))

        out = capsys.readouterr().out
        assert "remux + tag" in out
        assert "tag" in out
        assert "2 files would be processed." in out

    def test_missing_fpcalc_fails_before_processing(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "a.flac").write_bytes(b"x")
        monkeypatch.setattr(queue.shutil, "which", lambda _: None)

        with pytest.raises(SystemExit) as exc:
            run(self.args(tmp_path))

        assert exc.value.code == 1
        assert "fpcalc not found" in capsys.readouterr().err

    def test_missing_key_fails_before_processing(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "a.flac").write_bytes(b"x")
        monkeypatch.setattr(queue.shutil, "which", lambda _: "/usr/bin/fpcalc")
        monkeypatch.setattr(queue, "ensure_acoustid_key", lambda: False)

        with pytest.raises(SystemExit) as exc:
            run(self.args(tmp_path))

        assert exc.value.code == 1
        assert "ACOUSTID_API_KEY" in capsys.readouterr().err

    def test_summary_counts_outcomes(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "a.flac").write_bytes(b"x")
        (tmp_path / "b.flac").write_bytes(b"x")
        monkeypatch.setattr(queue.shutil, "which", lambda _: "/usr/bin/fpcalc")
        monkeypatch.setattr(queue, "ensure_acoustid_key", lambda: True)
        outcomes = iter(
            [
                Outcome("tagged-release", "a.flac", artist="A", title="T"),
                Outcome("needs-manual", "b.flac"),
            ]
        )
        monkeypatch.setattr(queue, "process", lambda src, delete_source: next(outcomes))

        run(self.args(tmp_path))

        out = capsys.readouterr().out
        assert "1 tagged (1 with release, 0 recording only)" in out
        assert "1 need manual" in out
        assert "A — T" in out

    def test_remux_source_is_skipped_when_output_is_in_queue(self, tmp_path, monkeypatch, capsys):
        src = tmp_path / "a.webm"
        dst = tmp_path / "a.opus"
        src.touch()
        dst.touch()
        monkeypatch.setattr(queue.shutil, "which", lambda _: "/usr/bin/fpcalc")
        monkeypatch.setattr(queue, "ensure_acoustid_key", lambda: True)
        monkeypatch.setattr(queue, "is_writable", lambda p: p.endswith(".opus"))
        monkeypatch.setattr(queue, "transcode_destination", lambda _: str(dst))
        called = []
        monkeypatch.setattr(
            queue,
            "process",
            lambda path, delete_source: (called.append(path), Outcome("already-complete", path))[1],
        )

        run(self.args(tmp_path))

        assert called == [str(dst)]
        assert "1 sources skipped" in capsys.readouterr().out

    def test_service_error_aborts_the_run(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "a.flac").write_bytes(b"x")
        monkeypatch.setattr(queue.shutil, "which", lambda _: "/usr/bin/fpcalc")
        monkeypatch.setattr(queue, "ensure_acoustid_key", lambda: True)

        def fail(src, delete_source):
            raise AcoustIDError("invalid API key")

        monkeypatch.setattr(queue, "process", fail)

        with pytest.raises(SystemExit) as exc:
            run(self.args(tmp_path))

        assert exc.value.code == 1
        assert "invalid API key" in capsys.readouterr().err

    def test_failures_set_exit_code(self, tmp_path, monkeypatch):
        (tmp_path / "a.flac").write_bytes(b"x")
        monkeypatch.setattr(queue.shutil, "which", lambda _: "/usr/bin/fpcalc")
        monkeypatch.setattr(queue, "ensure_acoustid_key", lambda: True)
        monkeypatch.setattr(queue, "process", lambda src, delete_source: Outcome("failed", src))

        with pytest.raises(SystemExit) as exc:
            run(self.args(tmp_path))

        assert exc.value.code == 1
