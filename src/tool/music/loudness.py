"""Mastering-quality analysis via EBU R 128 loudness measurement.

Uses ffmpeg's ebur128 filter to measure integrated loudness, loudness range,
and true peak.  These metrics complement the spectral brickwall analysis in
verdict.py — they catch amplitude-domain issues (over-compression, inter-sample
clipping) rather than frequency-domain transcoding artifacts.

Design decisions:

- **Separate pass** — The ebur128 filter graph cannot share a single ffmpeg
  invocation with firequalizer+astats (both write summary lines to stderr).
  Running a second pass keeps the analysis orthogonal and the parsing simple.
- **Middle 60 seconds** — When *duration_s* is given and > 60 s, seeks to the
  file midpoint and scans 60 s.  Integrated LUFS won't match a full-program
  measurement, but LRA and true peak are representative enough for scanning.
- **Summary-only** — framelog=quiet skips per-frame output; we only parse the
  final summary block from stderr.
"""

import re
import subprocess
from pathlib import Path

from tool.music.constants import GREY, RED

# Thresholds
TRUE_PEAK_LIMIT_DBTP = 0.0  # true peak above this = inter-sample clipping
LRA_FLAT_LU = 5.0  # LRA below this = dynamically flat (loudness-war victim)


def analyze_ebur128(filepath: Path, duration_s: float | None = None) -> dict[str, float] | None:
    """Run EBU R 128 analysis and return {integrated_lufs, lra, true_peak_dbtp} or None.

    If *duration_s* is given and the file is longer than 60 s, seeks to the
    midpoint and scans 60 s for speed.
    """
    seek_args: list[str] = []
    time_args: list[str] = []
    if duration_s is not None and duration_s > 60:
        seek_args = ["-ss", str(duration_s / 2 - 30)]
        time_args = ["-t", "60"]

    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "info",
                *seek_args,
                "-i",
                str(filepath),
                *time_args,
                "-af",
                "ebur128=peak=true:framelog=quiet",
                "-f",
                "null",
                "/dev/null",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired, OSError:
        return None

    if result.returncode != 0:
        return None

    stderr = result.stderr

    i_match = re.search(r"I:\s+(-?\d+\.?\d*)\s+LUFS", stderr)
    lra_match = re.search(r"LRA:\s+(\d+\.?\d*)\s+LU", stderr)
    peak_match = re.search(r"Peak:\s+(-?\d+\.?\d*)\s+dBFS", stderr)

    if i_match is None or lra_match is None or peak_match is None:
        return None

    return {
        "integrated_lufs": float(i_match.group(1)),
        "lra": float(lra_match.group(1)),
        "true_peak_dbtp": float(peak_match.group(1)),
    }


def compute_mastering_verdict(
    spec: dict[str, float],
    *,
    true_peak_limit: float = TRUE_PEAK_LIMIT_DBTP,
    lra_flat_limit: float = LRA_FLAT_LU,
) -> tuple[str, int, bool] | None:
    """Return (verdict_text, color_code, dim) for ebur128 results, or None if clean.

    Checks in priority order: true peak clipping first (more severe), then
    dynamically flat (loudness-war compression).
    """
    if spec["true_peak_dbtp"] > true_peak_limit:
        return ("True Peak Clipping", RED, False)
    if spec["lra"] < lra_flat_limit:
        return ("Dynamically Flat", GREY, False)
    return None
