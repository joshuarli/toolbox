from pathlib import Path
from unittest import mock

from tool.movies import encode, probe
from tool.movies.cli import build_parser
from tool.movies.crop import CropRect


def _probe_info(*, field_order: str = "progressive") -> probe.ProbeInfo:
    return probe.ProbeInfo(
        streams=[
            probe.ProbeStream(
                index=0,
                codec_type="video",
                codec_name="h264",
                width=1280,
                height=720,
                avg_frame_rate="24/1",
                r_frame_rate="24/1",
                pix_fmt="yuv420p",
                field_order=field_order,
            ),
            probe.ProbeStream(
                index=1,
                codec_type="audio",
                codec_name="aac",
                channels=2,
                bit_rate=128_000,
            ),
        ],
        format=probe.ProbeFormat(duration=120.0),
    )


def test_encode_cli_enables_crop_by_default_with_escape_hatch() -> None:
    parser = build_parser()

    default = parser.parse_args(["encode", "hq", "movie.mkv"])
    disabled = parser.parse_args(["encode", "--no-crop", "hq", "movie.mkv"])

    assert default.no_crop is False
    assert disabled.no_crop is True


def test_build_cmd_applies_crop_first_and_uses_cropped_dimensions_for_bpp() -> None:
    info = _probe_info()
    crop = CropRect(width=640, height=360, x=40, y=60)

    cmd = encode.build_cmd(
        Path("movie.mkv"),
        Path("movie.mp4"),
        encode.PRESETS["hq"],
        info,
        "abort",
        crop=crop,
    )

    vf = cmd[cmd.index("-vf") + 1]
    assert vf.split(",", 1)[0] == "crop=640:360:40:60"
    assert cmd[cmd.index("-maxrate") + 1] == "414720"
    assert cmd[cmd.index("-bufsize") + 1] == "1658880"


def test_build_cmd_keeps_crop_before_deinterlace_and_scale() -> None:
    info = _probe_info(field_order="tt")
    crop = CropRect(width=640, height=360, x=40, y=60)

    cmd = encode.build_cmd(
        Path("movie.mkv"),
        Path("movie.mp4"),
        encode.PRESETS["hq-720"],
        info,
        "abort",
        crop=crop,
    )

    vf = cmd[cmd.index("-vf") + 1]
    filters = vf.split(",")
    assert filters[0] == "crop=640:360:40:60"
    assert filters[1] == "bwdif=mode=send_field"
    assert filters[2].startswith("scale=")


def test_prepare_task_detects_crop_by_default(tmp_path: Path) -> None:
    src = tmp_path / "movie.mkv"
    src.touch()
    info = _probe_info()
    crop = CropRect(width=1200, height=640, x=40, y=40)

    with (
        mock.patch.object(encode.probe, "ffprobe", return_value=info),
        mock.patch.object(encode.probe, "with_estimated_bitrates", return_value=info),
        mock.patch.object(encode, "_check_source_quality", return_value=True),
        mock.patch.object(encode, "select_tracks", return_value=(info.audio_streams, [])),
        mock.patch.object(encode, "detect_crop", return_value=crop) as detect,
    ):
        task = encode.prepare_task("hq", src, "abort", True)

    assert task is not None
    assert task.crop == crop
    detect.assert_called_once_with(src, 120.0, 1280, 720)


def test_prepare_task_no_crop_skips_detection(tmp_path: Path) -> None:
    src = tmp_path / "movie.mkv"
    src.touch()
    info = _probe_info()

    with (
        mock.patch.object(encode.probe, "ffprobe", return_value=info),
        mock.patch.object(encode.probe, "with_estimated_bitrates", return_value=info),
        mock.patch.object(encode, "_check_source_quality", return_value=True),
        mock.patch.object(encode, "select_tracks", return_value=(info.audio_streams, [])),
        mock.patch.object(encode, "detect_crop") as detect,
    ):
        task = encode.prepare_task("hq", src, "abort", True, no_crop=True)

    assert task is not None
    assert task.crop is None
    detect.assert_not_called()
