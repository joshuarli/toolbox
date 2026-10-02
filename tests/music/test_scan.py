import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tool.music.constants import BLUE, GOLD, GREEN, GREY, MAGENTA, RED
from tool.music.metadata import Metadata
from tool.music.scan import (
    _format_duration,
    _format_size,
    _print_file_detail,
    bitrate_color,
    codec_color,
    collect_files,
    find_audio_stream,
    fmt_bitrate,
    fmt_codec,
    fmt_cover,
    fmt_tags,
    fmt_verdict,
    has_cover_art,
    probe,
)


class TestCodecColor:
    def test_lossless(self):
        for c in ["flac", "alac", "wavpack", "pcm_s16le"]:
            assert codec_color(c) == MAGENTA

    def test_lossy_high(self):
        for c in ["aac", "opus", "vorbis"]:
            assert codec_color(c) == GOLD

    def test_lossy_standard(self):
        for c in ["mp3", "ac3", "dts"]:
            assert codec_color(c) == BLUE

    def test_lossy_ancient(self):
        for c in ["gsm", "adpcm", "g729"]:
            assert codec_color(c) == RED

    def test_unknown(self):
        assert codec_color("xyz_unknown") == GREY


class TestBitrateColor:
    def test_lossless_always_magenta(self):
        assert bitrate_color(1000, "flac") == MAGENTA
        assert bitrate_color(10, "alac") == MAGENTA

    def test_high_kbps(self):
        assert bitrate_color(320, "aac") == GOLD
        assert bitrate_color(256, "mp3") == GOLD

    def test_medium_kbps(self):
        assert bitrate_color(200, "mp3") == BLUE
        assert bitrate_color(192, "aac") == BLUE

    def test_low_kbps(self):
        assert bitrate_color(150, "mp3") == GREEN
        assert bitrate_color(128, "aac") == GREEN

    def test_very_low(self):
        assert bitrate_color(80, "mp3") == GREY
        assert bitrate_color(64, "aac") == GREY

    def test_terrible(self):
        assert bitrate_color(32, "mp3") == RED


class TestFindAudioStream:
    def test_normal_audio(self):
        streams = [{"codec_type": "audio", "codec_name": "flac"}]
        result = find_audio_stream(streams)
        assert result == streams[0]

    def test_skips_attached_pic(self):
        streams = [
            {"codec_type": "audio", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
            {"codec_type": "audio", "codec_name": "flac"},
        ]
        result = find_audio_stream(streams)
        assert result["codec_name"] == "flac"

    def test_fallback_when_all_are_pics(self):
        streams = [
            {"codec_type": "audio", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
        ]
        result = find_audio_stream(streams)
        assert result is not None
        assert result["codec_name"] == "mjpeg"

    def test_no_audio_stream(self):
        streams = [{"codec_type": "video", "codec_name": "h264"}]
        result = find_audio_stream(streams)
        assert result is None


class TestHasCoverArt:
    def test_attached_pic_disposition(self):
        streams = [{"disposition": {"attached_pic": 1}}]
        assert has_cover_art(streams) is True

    def test_video_image_codecs(self):
        for codec in ["mjpeg", "png", "bmp", "gif"]:
            streams = [{"codec_type": "video", "codec_name": codec}]
            assert has_cover_art(streams) is True

    def test_no_cover(self):
        streams = [{"codec_type": "audio", "codec_name": "flac"}]
        assert has_cover_art(streams) is False


class TestFormatFunctions:
    def test_fmt_codec(self):
        result = fmt_codec("flac")
        assert "FLAC" in result
        assert "\033" in result

    def test_fmt_bitrate(self):
        result = fmt_bitrate(320000, "flac")
        assert "320k" in result
        assert "\033" in result

    def test_fmt_bitrate_none(self):
        result = fmt_bitrate(None, "flac")
        assert "?" in result

    def test_fmt_cover_yes(self):
        result = fmt_cover(True)
        assert "yes" in result

    def test_fmt_cover_no(self):
        result = fmt_cover(False)
        assert "no" in result

    def test_fmt_tags_all_present(self):
        tags = {"title": "T", "artist": "A", "album": "B", "tracknumber": "1"}
        result = fmt_tags(tags)
        assert "\033" in result

    def test_fmt_tags_missing(self):
        result = fmt_tags({})
        assert "\033" in result

    def test_fmt_verdict(self):
        result = fmt_verdict("Test", GREEN)
        assert "Test" in result
        assert "\033" in result

    def test_fmt_verdict_dim(self):
        result = fmt_verdict("Test", GREY, dim=True)
        assert "Test" in result


class TestDurationAndSize:
    def test_duration_seconds(self):
        assert _format_duration(30) == "0:30"

    def test_duration_minutes(self):
        assert _format_duration(125) == "2:05"

    def test_duration_hours(self):
        assert _format_duration(3661) == "1:01:01"

    def test_size_bytes(self):
        assert "B" in _format_size(500)

    def test_size_kb(self):
        assert "KB" in _format_size(2000)

    def test_size_mb(self):
        assert "MB" in _format_size(5_000_000)


class TestProbe:
    def test_returns_parsed_json(self):
        stdout = json.dumps({"format": {"duration": "30.0"}, "streams": []})
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = stdout
            mock_run.return_value = mock_result

            result = probe(Path("test.flac"))
            assert result is not None
            assert result["format"]["duration"] == "30.0"

    def test_returns_none_on_nonzero(self):
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 1
            mock_run.return_value = mock_result

            result = probe(Path("test.flac"))
            assert result is None

    def test_returns_none_on_error(self):
        with patch("subprocess.run", side_effect=OSError):
            result = probe(Path("test.flac"))
            assert result is None


class TestPrintFileDetail:
    def test_no_audio_stream(self, tmp_path, capsys):
        f = tmp_path / "test.bin"
        f.touch()

        probe_data = {"streams": [{"codec_type": "video", "codec_name": "h264"}], "format": {"duration": "10"}}
        with patch("tool.music.scan.probe", return_value=probe_data):
            with pytest.raises(SystemExit) as exc:
                _print_file_detail(
                    f,
                    "test.bin",
                    brickwall_threshold=25,
                    low_energy_threshold=-50,
                    no_hf_threshold=-72,
                    hi_res_no_hf_threshold=-50,
                    no_mastering=True,
                    true_peak_limit=0.0,
                    lra_flat_limit=5.0,
                )
            assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "no audio stream" in captured.err

    def test_probe_failure(self, tmp_path, capsys):
        f = tmp_path / "test.bin"
        f.touch()

        with patch("tool.music.scan.probe", return_value=None):
            with pytest.raises(SystemExit) as exc:
                _print_file_detail(
                    f,
                    "test.bin",
                    brickwall_threshold=25,
                    low_energy_threshold=-50,
                    no_hf_threshold=-72,
                    hi_res_no_hf_threshold=-50,
                    no_mastering=True,
                    true_peak_limit=0.0,
                    lra_flat_limit=5.0,
                )
            assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "could not probe" in captured.err

    def test_detail_with_mastering(self, tmp_path, capsys):
        f = tmp_path / "test.flac"
        f.write_bytes(b"dummy audio content" * 100)

        probe_data = {
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "flac",
                    "sample_rate": "44100",
                    "channels": 2,
                    "bits_per_raw_sample": 16,
                    "bit_rate": "800000",
                }
            ],
            "format": {"duration": "180.0"},
        }

        loud_spec = {"integrated_lufs": -14.0, "lra": 8.0, "true_peak_dbtp": -1.0}

        with (
            patch("tool.music.scan.probe", return_value=probe_data),
            patch("tool.music.scan.compute_verdict", return_value=("No Brickwall Detected", 201, False)),
            patch("tool.music.scan.read_tags", return_value=Metadata()),
            patch("tool.music.scan.analyze_ebur128", return_value=loud_spec),
            patch("tool.music.scan.compute_mastering_verdict", return_value=None),
        ):
            _print_file_detail(
                f,
                "test.flac",
                brickwall_threshold=25,
                low_energy_threshold=-50,
                no_hf_threshold=-72,
                hi_res_no_hf_threshold=-50,
                no_mastering=False,
                true_peak_limit=0.0,
                lra_flat_limit=5.0,
            )
        captured = capsys.readouterr()
        assert "LUFS" in captured.out
        assert "test.flac" in captured.out

    def test_full_detail_output(self, tmp_path, capsys):
        f = tmp_path / "test.flac"
        f.write_bytes(b"dummy audio content")

        probe_data = {
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "flac",
                    "sample_rate": "44100",
                    "channels": 2,
                    "bits_per_raw_sample": 16,
                    "bit_rate": "800000",
                }
            ],
            "format": {"duration": "180.0", "bit_rate": "800000"},
        }

        with (
            patch("tool.music.scan.probe", return_value=probe_data),
            patch("tool.music.scan.compute_verdict", return_value=("No Brickwall Detected", 201, False)),
            patch("tool.music.scan.read_tags", return_value=Metadata(title="Test", artist="Artist")),
        ):
            _print_file_detail(
                f,
                "test.flac",
                brickwall_threshold=25,
                low_energy_threshold=-50,
                no_hf_threshold=-72,
                hi_res_no_hf_threshold=-50,
                no_mastering=True,
                true_peak_limit=0.0,
                lra_flat_limit=5.0,
            )
        captured = capsys.readouterr()
        assert "test.flac" in captured.out
        assert "FLAC" in captured.out
        assert "Test" in captured.out
        assert "Artist" in captured.out


class TestScanMain:
    def test_no_files(self, capsys):
        from tool.music.scan import main

        with (
            patch("sys.argv", ["scan", "/nonexistent/dir"]),
            patch("tool.music.scan.collect_files", return_value=[]),
        ):
            main()
        captured = capsys.readouterr()
        assert "No audio files found" in captured.out

    def test_missing_ffprobe(self):
        from tool.music.scan import main

        with (
            patch("sys.argv", ["scan"]),
            patch("subprocess.run", side_effect=FileNotFoundError),
            pytest.raises(SystemExit),
        ):
            main()


class TestCollectFiles:
    def test_collects_from_directory(self, tmp_path):
        (tmp_path / "a.flac").touch()
        (tmp_path / "b.mp3").touch()
        (tmp_path / "readme.txt").touch()
        # .txt not in AUDIO_EXTENSIONS
        result = collect_files([str(tmp_path)])
        paths = {r[1] for r in result}
        assert "a.flac" in paths
        assert "b.mp3" in paths
        assert "readme.txt" not in paths

    def test_collects_single_file(self, tmp_path):
        f = tmp_path / "song.flac"
        f.touch()
        result = collect_files([str(f)])
        assert len(result) == 1
        assert result[0][1] == "song.flac"

    def test_deduplicates(self, tmp_path):
        d = tmp_path / "sub"
        d.mkdir()
        (d / "song.mp3").touch()
        result = collect_files([str(tmp_path)])
        assert len(result) == 1

    def test_skips_non_audio_extensions_in_dir(self, tmp_path):
        (tmp_path / "song.flac").touch()
        (tmp_path / "notes.txt").touch()
        result = collect_files([str(tmp_path)])
        paths = [r[1] for r in result]
        assert "song.flac" in [str(p) for p in paths]
        assert "notes.txt" not in [str(p) for p in paths]
