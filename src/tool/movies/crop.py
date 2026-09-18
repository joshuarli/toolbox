import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

CROP_SAMPLES = 12
CROP_FRAMES_PER_SAMPLE = 3
CROP_CONSENSUS = 0.75
CROP_MIN_OBSERVATIONS = 6
CROP_TIMEOUT_SECONDS = 20
CROP_FILTER = "cropdetect=limit=24:round=2:reset=1:skip=0"
CROP_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")


@dataclass(frozen=True, slots=True)
class CropRect:
    width: int
    height: int
    x: int
    y: int

    @property
    def ffmpeg_filter(self) -> str:
        return f"crop={self.width}:{self.height}:{self.x}:{self.y}"


def parse_cropdetect(stderr: str) -> list[CropRect]:
    return [CropRect(*(int(value) for value in match.groups())) for match in CROP_RE.finditer(stderr)]


def _valid_crop(rect: CropRect, source_width: int, source_height: int) -> bool:
    return (
        rect.width > 0
        and rect.height > 0
        and rect.x >= 0
        and rect.y >= 0
        and rect.x + rect.width <= source_width
        and rect.y + rect.height <= source_height
    )


def select_crop(
    candidates: list[CropRect],
    source_width: int,
    source_height: int,
    *,
    consensus: float = CROP_CONSENSUS,
    min_observations: int = CROP_MIN_OBSERVATIONS,
) -> CropRect | None:
    valid = [rect for rect in candidates if _valid_crop(rect, source_width, source_height)]
    if len(valid) < min_observations:
        return None

    winner, votes = Counter(valid).most_common(1)[0]
    if votes / len(valid) < consensus:
        return None
    if winner == CropRect(source_width, source_height, 0, 0):
        return None
    return winner


def _sample_times(duration: float, samples: int) -> list[float]:
    if duration <= 0 or samples <= 0:
        return []
    return [duration * n / (samples + 1) for n in range(1, samples + 1)]


def detect_crop(
    path: Path,
    duration: float,
    source_width: int,
    source_height: int,
    *,
    samples: int = CROP_SAMPLES,
    frames_per_sample: int = CROP_FRAMES_PER_SAMPLE,
) -> CropRect | None:
    if source_width <= 0 or source_height <= 0 or frames_per_sample <= 0:
        return None

    candidates: list[CropRect] = []
    for timestamp in _sample_times(duration, samples):
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "info",
            "-ss",
            f"{timestamp:.6f}",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-frames:v",
            str(frames_per_sample),
            "-vf",
            CROP_FILTER,
            "-f",
            "null",
            "-",
        ]
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=CROP_TIMEOUT_SECONDS,
                check=False,
            )
        except OSError, subprocess.TimeoutExpired:
            continue
        if result.returncode == 0:
            candidates.extend(parse_cropdetect(result.stderr))

    return select_crop(candidates, source_width, source_height)
