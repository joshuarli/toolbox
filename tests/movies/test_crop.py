import subprocess
from pathlib import Path

from tool.movies.crop import CropRect, detect_crop, parse_cropdetect, select_crop


def test_parse_cropdetect_extracts_all_rectangles() -> None:
    stderr = """
[Parsed_cropdetect_0 @ 0x1] x1:40 x2:679 y1:60 y2:419 w:640 h:360 x:40 y:60 pts:0 t:0.0 crop=640:360:40:60
[Parsed_cropdetect_0 @ 0x1] x1:40 x2:679 y1:60 y2:419 w:640 h:360 x:40 y:60 pts:1 t:0.1 crop=640:360:40:60
"""

    assert parse_cropdetect(stderr) == [
        CropRect(width=640, height=360, x=40, y=60),
        CropRect(width=640, height=360, x=40, y=60),
    ]


def test_select_crop_accepts_strong_consensus() -> None:
    crop = CropRect(width=640, height=360, x=40, y=60)
    noise = CropRect(width=638, height=358, x=42, y=60)

    assert select_crop([crop] * 8 + [noise], 720, 480) == crop


def test_select_crop_rejects_ambiguous_geometry() -> None:
    crop = CropRect(width=640, height=360, x=40, y=60)
    alternate = CropRect(width=720, height=360, x=0, y=60)

    assert select_crop([crop] * 6 + [alternate] * 3, 720, 480) is None


def test_select_crop_treats_full_frame_as_no_crop() -> None:
    full = CropRect(width=720, height=480, x=0, y=0)

    assert select_crop([full] * 9, 720, 480) is None


def test_select_crop_rejects_out_of_bounds_geometry() -> None:
    invalid = CropRect(width=700, height=470, x=40, y=20)

    assert select_crop([invalid] * 9, 720, 480) is None


def test_detect_crop_finds_fixed_black_borders(tmp_path: Path) -> None:
    path = tmp_path / "bordered.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=duration=1:size=640x360:rate=24",
            "-vf",
            "pad=720:480:(ow-iw)/2:(oh-ih)/2:black",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )

    assert detect_crop(path, 1.0, 720, 480, samples=3, frames_per_sample=2) == CropRect(
        width=640,
        height=360,
        x=40,
        y=60,
    )
