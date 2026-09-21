import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pytest

from tool.movies import encode, loudnorm, probe, scan_encodes

from .conftest import FIXTURE_DIR

ROOT = Path(__file__).resolve().parents[2]


def run(cmd: list[str], cwd: Path = ROOT, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60)
    if check and result.returncode != 0:
        raise AssertionError(f"Command failed: {cmd}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    return result


def ffprobe_json(path: Path) -> dict:
    result = run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ]
    )
    return json.loads(result.stdout)


def stream(path: Path, kind: str) -> dict:
    streams = ffprobe_json(path)["streams"]
    return next(s for s in streams if s.get("codec_type") == kind)


def make_fixture(name: str, cmd: list[str]) -> Path:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / name
    if not path.exists():
        run([*cmd, str(path)])
    return path


def sdr_fixture() -> Path:
    return make_fixture(
        "sdr-1280x720.mp4",
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=duration=2:size=1280x720:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:duration=2",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
        ],
    )


def sd_untagged_fixture() -> Path:
    return make_fixture(
        "sd-untagged-640x360.mp4",
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=duration=2:size=640x360:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=700:duration=2",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
        ],
    )


def interlaced_fixture() -> Path:
    return make_fixture(
        "interlaced-320x240.mp4",
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=duration=2:size=320x240:rate=30000/1001",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:duration=2",
            "-shortest",
            "-vf",
            "tinterlace=interleave_top",
            "-c:v",
            "libx264",
            "-flags",
            "+ilme+ildct",
            "-top",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
        ],
    )


def hdr_fixture() -> Path:
    return make_fixture(
        "hdr-tagged-320x180.mp4",
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=duration=1:size=320x180:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:duration=1",
            "-shortest",
            "-c:v",
            "libx265",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p10le",
            "-x265-params",
            "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc",
            "-c:a",
            "aac",
        ],
    )


def two_audio_fixture() -> Path:
    return make_fixture(
        "two-audio.mkv",
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=duration=2:size=160x90:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=2",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-map",
            "2:a",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
        ],
    )


def bloated_mpeg2_fixture() -> Path:
    return make_fixture(
        "bloated-mpeg2.mkv",
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=duration=2:size=640x360:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=550:duration=2",
            "-shortest",
            "-c:v",
            "mpeg2video",
            "-b:v",
            "4M",
            "-minrate",
            "4M",
            "-maxrate",
            "4M",
            "-bufsize",
            "8M",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
        ],
    )


class EncodingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def copy_fixture(self, path: Path, name: str | None = None) -> Path:
        dst = self.work / (name or path.name)
        shutil.copy2(path, dst)
        return dst

    def encode(self, *args: str) -> subprocess.CompletedProcess:
        return run([sys.executable, "-m", "tool", "movies", "encode", *args], cwd=ROOT)

    def test_hq_outputs_apple_friendly_hevc_main10_mp4(self) -> None:
        src = self.copy_fixture(sdr_fixture())
        self.encode("-y", "hq", str(src))

        out = src.with_name("sdr-1280x720-encoded-hq.mp4")
        video = stream(out, "video")
        audio = stream(out, "audio")

        assert video["codec_name"] == "hevc"
        assert video["codec_tag_string"] == "hvc1"
        assert video["profile"] == "Main 10"
        assert video["color_range"] == "tv"
        assert video["color_space"] == "bt709"
        assert audio["codec_name"] == "aac"

    def test_directory_input_runs_multiple_real_encodes(self) -> None:
        self.copy_fixture(sdr_fixture(), "one.mp4")
        self.copy_fixture(sdr_fixture(), "two.mp4")

        result = self.encode("-y", "hq", str(self.work))

        assert "Queue: 2 files" in result.stdout
        assert (self.work / "one-encoded-hq.mp4").exists()
        assert (self.work / "two-encoded-hq.mp4").exists()

    def test_single_file_uses_general_progress_path(self) -> None:
        src = self.copy_fixture(sdr_fixture())

        result = self.encode("-y", "--progress", "plain", "hq", str(src))

        assert "Queue: 1 file" in result.stdout
        assert "Start:  sdr-1280x720.mp4" in result.stdout
        assert src.with_name("sdr-1280x720-encoded-hq.mp4").exists()

    def test_interlaced_sources_are_deinterlaced_to_field_rate_progressive(self) -> None:
        src = self.copy_fixture(interlaced_fixture())
        input_video = stream(src, "video")

        self.encode("-y", "hq", str(src))

        out = src.with_name("interlaced-320x240-encoded-hq.mp4")
        output_video = stream(out, "video")
        input_fps = probe.parse_fps(input_video["avg_frame_rate"])
        output_fps = probe.parse_fps(output_video["avg_frame_rate"])

        assert output_video["field_order"] == "progressive"
        assert output_fps > input_fps * 1.9

    def test_hdr_sources_are_skipped_by_default(self) -> None:
        src = self.copy_fixture(hdr_fixture())

        result = self.encode("hq", str(src))

        assert "Skipping HDR source" in result.stdout
        assert not src.with_name("hdr-tagged-320x180-encoded-hq.mp4").exists()

    def test_hdr_tonemap_outputs_bt709_main10(self) -> None:
        src = self.copy_fixture(hdr_fixture())

        self.encode("-y", "--hdr-policy", "tonemap", "hq", str(src))

        out = src.with_name("hdr-tagged-320x180-encoded-hq.mp4")
        video = stream(out, "video")
        assert video["codec_name"] == "hevc"
        assert video["profile"] == "Main 10"
        assert video["color_range"] == "tv"
        assert video["color_space"] == "bt709"
        assert video["color_transfer"] == "bt709"
        assert video["color_primaries"] == "bt709"

    def test_not_h265_sources_are_encoded_without_confirmation(self) -> None:
        src = self.copy_fixture(sdr_fixture())

        result = self.encode("hq", str(src))

        assert "Skipping quality source" not in result.stdout
        assert src.with_name("sdr-1280x720-encoded-hq.mp4").exists()

    def test_delete_option_removes_source_after_better_temp_encode(self) -> None:
        src = self.copy_fixture(bloated_mpeg2_fixture())

        result = self.encode("--delete", "--progress", "plain", "hq", str(src))

        assert "deleted source" in result.stdout
        assert not src.exists()
        assert src.with_name("bloated-mpeg2-encoded-hq.mp4").exists()

    def test_strictly_better_rejects_larger_output(self) -> None:
        src_quality = encode.FileQuality(
            score=80,
            size_bytes=100,
            video_bpp=0.04,
            flags=set(),
            stream_counts={"video": 1, "audio": 1},
            subtitle_codecs=[],
        )
        dst_quality = encode.FileQuality(
            score=95,
            size_bytes=120,
            video_bpp=0.05,
            flags=set(),
            stream_counts={"video": 1, "audio": 1},
            subtitle_codecs=[],
        )

        with mock.patch.object(encode, "_file_quality", side_effect=[(src_quality, ""), (dst_quality, "")]):
            better, reason = encode._strictly_better_output(Path("source.mp4"), Path("encoded.mp4"))

        assert not better
        assert "larger than source" in reason

    def test_build_command_maps_selected_audio_tracks(self) -> None:
        src = two_audio_fixture()
        info = probe.with_estimated_bitrates(src, probe.ffprobe(src))

        cmd = encode.build_cmd(
            src,
            src.with_name("out.mp4"),
            encode.PRESETS["hq"],
            info,
            "abort",
            info.audio_streams,
            [],
        )

        assert ["-map", f"0:{info.audio_streams[0].index}"] == cmd[cmd.index("-map", cmd.index("0:V:0") + 1) :][:2]
        assert f"0:{info.audio_streams[1].index}" in cmd
        assert "-c:a:0" in cmd
        assert "-c:a:1" in cmd

    def test_hq_preset_uses_quality_vbr_with_practical_cap(self) -> None:
        src = sdr_fixture()
        info = probe.with_estimated_bitrates(src, probe.ffprobe(src))

        cmd = encode.build_cmd(src, src.with_name("out.mp4"), encode.PRESETS["hq"], info, "abort")

        assert cmd[cmd.index("-q:v") + 1] == "85"
        assert "-b:v" not in cmd
        assert "-constant_bit_rate" not in cmd
        assert cmd[cmd.index("-maxrate") + 1] == "1658880"
        assert cmd[cmd.index("-bufsize") + 1] == "6635520"

    def test_uhq_preset_uses_higher_vt_bitrate_target_without_quality_mode(self) -> None:
        src = sdr_fixture()
        info = probe.with_estimated_bitrates(src, probe.ffprobe(src))

        cmd = encode.build_cmd(src, src.with_name("out.mp4"), encode.PRESETS["uhq"], info, "abort")

        assert "-q:v" not in cmd
        assert cmd[cmd.index("-b:v") + 1] == "1548288"
        assert cmd[cmd.index("-constant_bit_rate") + 1] == "true"
        assert "-spatial_aq" in cmd
        assert "-prio_speed" in cmd
        assert "-maxrate" not in cmd
        assert "-bufsize" not in cmd

    def test_vt_preserves_422_chroma_with_main42210_output(self) -> None:
        src = sdr_fixture()
        info = probe.with_estimated_bitrates(src, probe.ffprobe(src))
        streams = [
            replace(stream, pix_fmt="yuv422p10le") if stream.codec_type == "video" else stream
            for stream in info.streams
        ]
        info = replace(info, streams=streams)

        for preset_name in ("hq", "uhq"):
            cmd = encode.build_cmd(src, src.with_name("out.mp4"), encode.PRESETS[preset_name], info, "abort")

            assert cmd[cmd.index("-profile:v") + 1] == "main42210"
            assert cmd[cmd.index("-pix_fmt") + 1] == "p210le"

    def test_vt_rejects_444_sources_instead_of_downsampling(self) -> None:
        src = sdr_fixture()
        info = probe.with_estimated_bitrates(src, probe.ffprobe(src))
        streams = [
            replace(stream, pix_fmt="yuv444p10le") if stream.codec_type == "video" else stream
            for stream in info.streams
        ]
        info = replace(info, streams=streams)

        with pytest.raises(ValueError, match="cannot preserve 4:4:4"):
            encode.build_cmd(src, src.with_name("out.mp4"), encode.PRESETS["hq"], info, "abort")

    def test_web_preset_uses_size_preserving_vt_bitrate_target(self) -> None:
        src = sdr_fixture()
        info = probe.with_estimated_bitrates(src, probe.ffprobe(src))

        cmd = encode.build_cmd(src, src.with_name("out.mp4"), encode.PRESETS["web"], info, "abort")

        assert "-q:v" not in cmd
        assert cmd[cmd.index("-b:v") + 1] == "287539"
        assert cmd[cmd.index("-constant_bit_rate") + 1] == "true"
        assert "-maxrate" not in cmd
        assert "-bufsize" not in cmd

    def test_interactive_track_selection_accepts_csv_numbers(self) -> None:
        info = probe.ProbeInfo(
            streams=[
                probe.ProbeStream(index=0, codec_type="video", codec_name="h264"),
                probe.ProbeStream(index=1, codec_type="audio", codec_name="dts", channels=6),
                probe.ProbeStream(index=2, codec_type="audio", codec_name="eac3", channels=8),
                probe.ProbeStream(index=3, codec_type="subtitle", codec_name="subrip"),
                probe.ProbeStream(index=4, codec_type="subtitle", codec_name="subrip"),
            ],
            format=probe.ProbeFormat(),
        )

        with mock.patch("builtins.input", side_effect=["1,2", "2"]):
            audio, subtitles = encode.select_tracks(info, interactive=True)

        assert [stream.index for stream in audio] == [1, 2]
        assert [stream.index for stream in subtitles] == [4]

    def test_default_subtitle_selection_keeps_only_english_text_tracks(self) -> None:
        info = probe.ProbeInfo(
            streams=[
                probe.ProbeStream(index=0, codec_type="video", codec_name="h264"),
                probe.ProbeStream(index=1, codec_type="audio", codec_name="aac", channels=2),
                probe.ProbeStream(index=2, codec_type="subtitle", codec_name="subrip", tags={"language": "eng"}),
                probe.ProbeStream(index=3, codec_type="subtitle", codec_name="subrip", tags={"language": "spa"}),
                probe.ProbeStream(
                    index=4, codec_type="subtitle", codec_name="hdmv_pgs_subtitle", tags={"language": "eng"}
                ),
            ],
            format=probe.ProbeFormat(),
        )

        _, subtitles = encode.select_tracks(info, interactive=False)

        assert [stream.index for stream in subtitles] == [2]

    def test_interactive_track_selection_accepts_dvd_subtitle_for_sidecar(self) -> None:
        info = probe.ProbeInfo(
            streams=[
                probe.ProbeStream(index=0, codec_type="video", codec_name="h264"),
                probe.ProbeStream(index=1, codec_type="audio", codec_name="aac", channels=2),
                probe.ProbeStream(index=2, codec_type="subtitle", codec_name="dvd_subtitle", tags={"language": "dut"}),
            ],
            format=probe.ProbeFormat(),
        )

        with mock.patch("builtins.input", return_value="1"):
            _, subtitles = encode.select_tracks(info, interactive=True)

        assert [stream.index for stream in subtitles] == [2]

    def test_subtitle_choice_label_omits_stream_index(self) -> None:
        stream = probe.ProbeStream(index=7, codec_type="subtitle", codec_name="subrip", tags={"language": "eng"})

        assert encode._describe_subtitle_choice(1, stream) == "  1. eng subrip"

    def test_build_command_does_not_map_sidecar_subtitles_into_mp4(self) -> None:
        src = sdr_fixture()
        info = probe.ProbeInfo(
            streams=[
                probe.ProbeStream(
                    index=0, codec_type="video", codec_name="h264", width=1280, height=720, avg_frame_rate="24/1"
                ),
                probe.ProbeStream(index=1, codec_type="audio", codec_name="aac", channels=2, bit_rate=128_000),
                probe.ProbeStream(index=2, codec_type="subtitle", codec_name="dvd_subtitle", tags={"language": "dut"}),
            ],
            format=probe.ProbeFormat(),
        )

        cmd = encode.build_cmd(
            src,
            src.with_name("out.mp4"),
            encode.PRESETS["hq"],
            info,
            "abort",
            [info.audio_streams[0]],
            [info.subtitle_streams[0]],
        )

        assert f"0:{info.subtitle_streams[0].index}" not in cmd

    def test_extract_subtitle_sidecars_uses_matroska_muxer(self) -> None:
        stream = probe.ProbeStream(index=3, codec_type="subtitle", codec_name="dvd_subtitle", tags={"language": "dut"})

        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run_mock:
            ok, err, outputs = encode.extract_subtitle_sidecars(
                Path("Summer Palace.mkv"), Path("Summer Palace.mp4"), [stream]
            )

        assert ok
        assert err == ""
        assert outputs == [Path("Summer Palace.s3.dut.mks")]
        cmd = run_mock.call_args.args[0]
        assert cmd[cmd.index("-map") + 1] == "0:3"
        assert cmd[cmd.index("-f") + 1] == "matroska"

    def test_strictly_better_allows_subrip_to_mov_text_conversion(self) -> None:
        src_quality = encode.FileQuality(
            score=40,
            size_bytes=120,
            video_bpp=0.08,
            flags=set(),
            stream_counts={"video": 1, "audio": 1, "subtitle": 1},
            subtitle_codecs=["subrip"],
        )
        dst_quality = encode.FileQuality(
            score=80,
            size_bytes=100,
            video_bpp=0.04,
            flags=set(),
            stream_counts={"video": 1, "audio": 1, "subtitle": 1},
            subtitle_codecs=["mov_text"],
        )

        with mock.patch.object(encode, "_file_quality", side_effect=[(src_quality, ""), (dst_quality, "")]):
            better, reason = encode._strictly_better_output(Path("source.mkv"), Path("encoded.mp4"))

        assert better
        assert "improved" in reason

    def test_strictly_better_validates_selected_stream_counts(self) -> None:
        src_quality = encode.FileQuality(
            score=40,
            size_bytes=120,
            video_bpp=0.08,
            flags=set(),
            stream_counts={"video": 1, "audio": 4, "subtitle": 3},
            subtitle_codecs=["subrip", "subrip", "subrip"],
        )
        dst_quality = encode.FileQuality(
            score=80,
            size_bytes=100,
            video_bpp=0.04,
            flags=set(),
            stream_counts={"video": 1, "audio": 2, "subtitle": 1},
            subtitle_codecs=["mov_text"],
        )

        with mock.patch.object(encode, "_file_quality", side_effect=[(src_quality, ""), (dst_quality, "")]):
            better, reason = encode._strictly_better_output(
                Path("source.mkv"),
                Path("encoded.mp4"),
                selected_audio_count=2,
                selected_subtitle_codecs=["subrip"],
            )

        assert better
        assert "improved" in reason

    def test_identical_file_is_not_strictly_better(self) -> None:
        src = self.copy_fixture(sdr_fixture(), "source.mp4")
        dst = self.copy_fixture(sdr_fixture(), "copy.mp4")

        better, reason = encode._strictly_better_output(src, dst)

        assert not better
        assert "not greater" in reason

    def test_delete_cannot_be_combined_with_yes(self) -> None:
        src = self.copy_fixture(sdr_fixture())

        result = run(
            [sys.executable, "-m", "tool", "movies", "encode", "--delete", "--yes", "hq", str(src)], check=False
        )

        assert result.returncode != 0
        assert "--delete cannot be combined with --yes" in result.stderr
        assert src.exists()

    def test_tmp_dir_requires_delete(self) -> None:
        src = self.copy_fixture(sdr_fixture())

        result = run(
            [sys.executable, "-m", "tool", "movies", "encode", "--tmp-dir", str(self.work), "hq", str(src)],
            check=False,
        )

        assert result.returncode != 0
        assert "--tmp-dir requires --delete" in result.stderr
        assert src.exists()

    def test_high_raw_bpp_sd_h264_does_not_score_as_quality_source(self) -> None:
        stream = probe.ProbeStream(
            codec_type="video",
            codec_name="h264",
            width=704,
            height=352,
            avg_frame_rate="24000/1001",
            bit_rate=1_600_000,
            pix_fmt="yuv420p",
            field_order="progressive",
        )

        video_info, video_flags = probe.analyze_video(stream)
        score, reasons = probe.quality_score(video_info, video_flags)

        assert video_info["bpp"] > 0.2
        assert score < encode.QUALITY_SKIP_THRESHOLD
        assert any(r.startswith("acceptable BPP") for r in reasons)
        assert not any(r.startswith("healthy BPP") for r in reasons)

    def test_solid_h264_encode_scores_as_quality_source(self) -> None:
        stream = probe.ProbeStream(
            codec_type="video",
            codec_name="h264",
            width=1920,
            height=1008,
            avg_frame_rate="24000/1001",
            bit_rate=2_250_000,
            pix_fmt="yuv420p",
            field_order="progressive",
        )

        video_info, video_flags = probe.analyze_video(stream)
        score, reasons = probe.quality_score(video_info, video_flags)

        assert score >= encode.QUALITY_SKIP_THRESHOLD
        assert any(r.startswith("decent codec") for r in reasons)
        assert any(r.startswith("healthy BPP") for r in reasons)

    def test_audio_flags_do_not_affect_quality_score(self) -> None:
        stream = probe.ProbeStream(
            codec_type="video",
            codec_name="hevc",
            width=1280,
            height=720,
            avg_frame_rate="24000/1001",
            bit_rate=1_500_000,
            pix_fmt="yuv420p10le",
            field_order="progressive",
        )

        video_info, video_flags = probe.analyze_video(stream)
        score, reasons = probe.quality_score(video_info, video_flags)

        assert score == 95
        assert "adequate audio" not in reasons

    def test_high_bpp_hevc_does_not_get_full_bpp_credit(self) -> None:
        stream = probe.ProbeStream(
            codec_type="video",
            codec_name="hevc",
            width=1920,
            height=1008,
            avg_frame_rate="24000/1001",
            bit_rate=3_730_000,
            pix_fmt="yuv420p10le",
            field_order="progressive",
        )

        video_info, video_flags = probe.analyze_video(stream)
        score, reasons = probe.quality_score(video_info, video_flags)

        assert video_info["bpp"] > 0.07
        assert score == 75
        assert any(r.startswith("acceptable BPP") for r in reasons)
        assert not any(r.startswith("healthy BPP") for r in reasons)

    def test_efficient_sd_av1_scores_as_quality_source(self) -> None:
        stream = probe.ProbeStream(
            codec_type="video",
            codec_name="av1",
            width=640,
            height=480,
            avg_frame_rate="30000/1001",
            bit_rate=300_000,
            pix_fmt="yuv420p",
            field_order="progressive",
        )

        video_info, video_flags = probe.analyze_video(stream)
        score, reasons = probe.quality_score(video_info, video_flags)

        assert video_info["bpp"] < 0.04
        assert not video_flags
        assert score == 100
        assert any(r.startswith("healthy BPP") for r in reasons)

    def test_sd_hevc_uses_normalized_bpp_for_quality_score(self) -> None:
        stream = probe.ProbeStream(
            codec_type="video",
            codec_name="hevc",
            width=704,
            height=352,
            avg_frame_rate="24000/1001",
            bit_rate=570_000,
            pix_fmt="yuv420p10le",
            field_order="progressive",
        )

        video_info, video_flags = probe.analyze_video(stream)
        score, reasons = probe.quality_score(video_info, video_flags)

        assert video_info["bpp"] > 0.07
        assert score == 95
        assert any(r.startswith("healthy BPP") for r in reasons)

    def test_quality_score_coloring_matches_skip_policy(self) -> None:
        assert scan_encodes.RED in scan_encodes.color_quality_score(71)
        assert scan_encodes.YELLOW in scan_encodes.color_quality_score(72)
        assert scan_encodes.GREEN in scan_encodes.color_quality_score(80)

    def test_scan_collects_explicit_video_files(self) -> None:
        src = sdr_fixture()

        assert scan_encodes.collect_files([src]) == [src]

    def test_untagged_sd_defaults_to_smpte170m(self) -> None:
        src = self.copy_fixture(sd_untagged_fixture())

        self.encode("-y", "hq", str(src))

        out = src.with_name("sd-untagged-640x360-encoded-hq.mp4")
        video = stream(out, "video")
        assert video["color_space"] == "smpte170m"

    def test_missing_audio_bitrate_is_estimated_per_audio_stream(self) -> None:
        path = two_audio_fixture()

        result = probe.analyze(path)

        assert len(result["audio"]) == 2
        assert all(a["br_kbps"] for a in result["audio"])

    def test_audio_target_bitrate_policy(self) -> None:
        assert probe.target_audio_bitrate(2) == "256k"
        assert probe.target_audio_bitrate(6) == "384k"
        assert probe.target_audio_bitrate(8) == "448k"
        assert probe.target_audio_bitrate(2, 128) == "128k"
        assert probe.target_audio_bitrate(2, 32) == "256k"

    def test_loudnorm_command_uses_estimated_source_audio_bitrate(self) -> None:
        path = two_audio_fixture()
        info = probe.with_estimated_bitrates(path, probe.ffprobe(path))
        audio = info.audio_streams[0]
        expected = probe.target_audio_bitrate(audio.channels, round(audio.bit_rate / 1000))

        cmd = loudnorm.build_cmd(path, path.with_name("out.mkv"), probe.ffprobe(path), 1.25)

        assert cmd[cmd.index("-b:a:0") + 1] == expected

    def test_audio_bitrate_estimate_uses_full_stream_when_head_is_silence(self) -> None:
        path = self.work / "leading-silence-opus.webm"
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000:duration=2",
                "-f",
                "lavfi",
                "-i",
                "aevalsrc=0.2*sin(2*PI*1000*t)|0.2*sin(2*PI*1000*t):s=48000:d=3",
                "-filter_complex",
                "[0:a][1:a]concat=n=2:v=0:a=1[a]",
                "-map",
                "[a]",
                "-c:a",
                "libopus",
                str(path),
            ]
        )

        br = probe.probe_audio_br(path, sample_secs=1)

        assert br is not None
        assert br > probe.LOW_CONFIDENCE_AUDIO_SAMPLE_BPS

    def test_ffprobe_json_is_deserialized_to_typed_probe_info(self) -> None:
        info = probe.ffprobe(sdr_fixture())

        assert isinstance(info, probe.ProbeInfo)
        assert info.format.duration > 0
        assert info.first_video is not None
        assert info.first_video.codec_type == "video"
        assert info.audio_streams[0].codec_type == "audio"

    def test_image_video_streams_are_ignored_for_video_selection_and_counts(self) -> None:
        info = probe.ProbeInfo(
            streams=[
                probe.ProbeStream(index=0, codec_type="video", codec_name="h264"),
                probe.ProbeStream(
                    index=1,
                    codec_type="video",
                    codec_name="mjpeg",
                    disposition={"attached_pic": 1},
                    tags={"filename": "cover.jpg", "mimetype": "image/jpeg"},
                ),
                probe.ProbeStream(index=2, codec_type="audio", codec_name="aac"),
            ],
            format=probe.ProbeFormat(),
        )

        assert [stream.index for stream in info.video_streams] == [0]
        assert info.first_video is info.streams[0]
        assert encode._stream_counts(info) == {"video": 1, "audio": 1}

    def test_ffmpeg_progress_output_is_grouped_into_typed_snapshots(self) -> None:
        snapshots = list(
            encode.iter_ffmpeg_progress(
                [
                    "frame=12\n",
                    "fps=24.5\n",
                    "out_time_us=500000\n",
                    "speed=1.2x\n",
                    "progress=continue\n",
                    "frame=24\n",
                    "out_time_us=1000000\n",
                    "progress=end\n",
                ]
            )
        )

        assert len(snapshots) == 2
        assert snapshots[0].frame == 12
        assert snapshots[0].fps == 24.5
        assert snapshots[0].out_time_seconds == 0.5
        assert snapshots[1].progress == "end"

    def test_run_encode_returns_stderr_after_process_exit(self) -> None:
        ok, err = encode.run_encode(
            [
                sys.executable,
                "-c",
                (
                    "import sys\n"
                    "print('out_time_us=1000000')\n"
                    "print('progress=end')\n"
                    "sys.stderr.write('encoder failed late\\n')\n"
                    "sys.exit(7)\n"
                ),
            ],
            1,
        )

        assert not ok
        assert "encoder failed late" in err

    def test_encode_command_requests_periodic_machine_progress(self) -> None:
        src = sdr_fixture()
        info = probe.ffprobe(src)
        cmd = encode.build_cmd(src, src.with_name("out.mp4"), encode.PRESETS["hq"], info, "abort")

        assert "-progress" in cmd
        assert "pipe:1" in cmd
        assert "-stats_period" in cmd
        assert cmd[cmd.index("-stats_period") + 1] == "1"

    def test_live_progress_lines_fit_terminal_width(self) -> None:
        status = encode.ProgressStatus(
            name="A Very Long Movie Filename That Would Otherwise Wrap In A Narrow Terminal.mkv",
            pct=0.42,
            fps=58.4,
            speed="3.1x",
        )

        line = encode._progress_line(status, status.pct, 60)

        assert len(line) <= 59
        assert "..." in line
        assert "58.4" in line

    def test_live_progress_header_labels_speed_fps_and_file(self) -> None:
        header = encode._progress_header(80)

        assert "FPS" in header
        assert "Speed" in header
        assert "File" in header


if __name__ == "__main__":
    unittest.main()
