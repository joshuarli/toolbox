import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from tool.music.transcode import _BITRATE, AAC_EXT, main, probe_audio, probe_codec, transcode_to_aac


class TestProbeAudio:
    def test_returns_info(self):
        stdout = json.dumps({"streams": [{"codec_name": "flac", "channels": 2, "sample_rate": "44100"}]})
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.stdout = stdout
            mock_run.return_value = mock_result

            info = probe_audio("test.flac")
            assert info is not None
            assert info["codec_name"] == "flac"
            assert info["channels"] == 2

    def test_returns_none_on_empty_streams(self):
        stdout = json.dumps({"streams": []})
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.stdout = stdout
            mock_run.return_value = mock_result

            info = probe_audio("test.flac")
            assert info is None

    def test_returns_none_on_error(self):
        with patch("subprocess.run", side_effect=OSError):
            info = probe_audio("test.flac")
            assert info is None

    def test_returns_none_on_nonzero_returncode(self):
        with patch("subprocess.run", side_effect=Exception):
            info = probe_audio("test.flac")
            assert info is None


class TestProbeCodec:
    def test_delegates_to_probe_audio(self):
        with patch("tool.music.transcode.probe_audio") as mock_probe:
            mock_probe.return_value = {"codec_name": "opus", "channels": 2}
            assert probe_codec("test.opus") == "opus"

    def test_returns_none_when_probe_fails(self):
        with patch("tool.music.transcode.probe_audio", return_value=None):
            assert probe_codec("test.bin") is None

    def test_returns_none_when_codec_missing(self):
        with patch("tool.music.transcode.probe_audio", return_value={"channels": 2}):
            assert probe_codec("test.flac") is None


class TestTranscodeToAac:
    def test_transcodes_with_derived_dst(self):
        with patch("tool.music.transcode.probe_audio") as mock_probe:
            mock_probe.return_value = {"codec_name": "flac", "channels": 2}
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock()
                result = transcode_to_aac("music/song.flac")
                assert result == f"music/song{AAC_EXT}"
                cmd = mock_run.call_args[0][0]
                assert "-n" in cmd
                assert "-y" not in cmd
                assert "-b:a" in cmd
                assert _BITRATE in cmd
                assert "-map_metadata" in cmd
                assert "-c:a" in cmd

    def test_transcodes_with_explicit_dst(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock()
            result = transcode_to_aac("input.webm", dst="output.m4a")
            assert result == "output.m4a"
            cmd = mock_run.call_args[0][0]
            assert cmd[-1] == "output.m4a"

    def test_overwrite_is_explicit(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock()
            result = transcode_to_aac("input.flac", dst="output.m4a", overwrite=True)
        assert result == "output.m4a"
        cmd = mock_run.call_args.args[0]
        assert "-y" in cmd
        assert "-n" not in cmd

    def test_existing_output_is_preserved(self, flac_file, tmp_path):
        dst = tmp_path / "existing.m4a"
        original = b"existing output"
        dst.write_bytes(original)
        assert transcode_to_aac(flac_file, dst=str(dst)) is None
        assert dst.read_bytes() == original

    def test_dangling_symlink_output_is_refused(self, flac_file, tmp_path):
        target = tmp_path / "target.m4a"
        dst = tmp_path / "output.m4a"
        dst.symlink_to(target)
        assert transcode_to_aac(flac_file, dst=str(dst)) is None
        assert dst.is_symlink()
        assert not target.exists()

    def test_returns_none_on_ffmpeg_failure(self):
        import subprocess

        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "ffmpeg")):
            result = transcode_to_aac("input.flac")
            assert result is None


class TestMain:
    def test_prints_help(self, capsys):
        with (
            patch.object(sys, "argv", ["transcode", "--help"]),
            pytest.raises(SystemExit) as exc,
        ):
            main()
        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert "usage" in captured.out

    def test_exits_on_no_audio_stream(self):
        with (
            patch.object(sys, "argv", ["transcode", "test.bin"]),
            patch("tool.music.transcode.probe_audio", return_value=None),
            pytest.raises(SystemExit) as exc,
        ):
            main()
        assert exc.value.code == 1

    def test_success_path(self, capsys):
        with (
            patch.object(sys, "argv", ["transcode", "song.flac"]),
            patch("tool.music.transcode.probe_audio") as mock_probe,
            patch("tool.music.transcode.transcode_to_aac") as mock_xcode,
        ):
            mock_probe.return_value = {"codec_name": "flac", "channels": 2}
            mock_xcode.return_value = "song.m4a"
            main()
        # No sys.exit on success
        captured = capsys.readouterr()
        assert "Source:" in captured.out
        assert "Wrote:" in captured.out

    def test_exits_on_transcode_failure(self):
        with (
            patch.object(sys, "argv", ["transcode", "song.flac"]),
            patch("tool.music.transcode.probe_audio", return_value={"codec_name": "flac", "channels": 2}),
            patch("tool.music.transcode.transcode_to_aac", return_value=None),
            pytest.raises(SystemExit) as exc,
        ):
            main()
        assert exc.value.code == 1

    def test_channel_labels(self, capsys):
        cases = [
            (1, "mono"),
            (2, "stereo"),
            (6, "5.1"),
            (8, "7.1"),
            (4, "4ch"),
        ]
        for channels, label in cases:
            with (
                patch.object(sys, "argv", ["transcode", "song.flac"]),
                patch("tool.music.transcode.probe_audio") as mock_probe,
                patch("tool.music.transcode.transcode_to_aac") as mock_xcode,
            ):
                mock_probe.return_value = {"codec_name": "flac", "channels": channels}
                mock_xcode.return_value = "song.m4a"
                main()
            captured = capsys.readouterr()
            assert f", {label}" in captured.out

    def test_channels_none(self, capsys):
        with (
            patch.object(sys, "argv", ["transcode", "song.flac"]),
            patch("tool.music.transcode.probe_audio") as mock_probe,
            patch("tool.music.transcode.transcode_to_aac") as mock_xcode,
        ):
            mock_probe.return_value = {"codec_name": "flac"}
            mock_xcode.return_value = "song.m4a"
            main()
        captured = capsys.readouterr()
        assert ", ?" in captured.out

    def test_custom_output_path(self, capsys):
        with (
            patch.object(sys, "argv", ["transcode", "song.flac", "-o", "custom.m4a"]),
            patch("tool.music.transcode.probe_audio") as mock_probe,
            patch("tool.music.transcode.transcode_to_aac") as mock_xcode,
        ):
            mock_probe.return_value = {"codec_name": "flac", "channels": 2}
            mock_xcode.return_value = "custom.m4a"
            main()
        mock_xcode.assert_called_once_with("song.flac", dst="custom.m4a", overwrite=False)

    def test_overwrite_option(self):
        with (
            patch.object(sys, "argv", ["transcode", "song.flac", "--overwrite"]),
            patch("tool.music.transcode.probe_audio", return_value={"codec_name": "flac", "channels": 2}),
            patch("tool.music.transcode.transcode_to_aac", return_value="song.m4a") as mock_xcode,
        ):
            main()
        mock_xcode.assert_called_once_with("song.flac", dst=None, overwrite=True)
