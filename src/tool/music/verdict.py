"""Lossy-transcode detection via spectral brickwall analysis.

Lossy encoders (MP3, AAC, Vorbis, Opus) apply a steep low-pass filter at a
bitrate-dependent cutoff (e.g. 16 kHz for 128 kbps MP3, 20.5 kHz for 320 kbps).
A genuine lossless file has a gradual natural roll-off; a lossy-to-lossless
transcode preserves the encoder's abrupt brickwall.

We detect the brickwall with a multi-point slope profile:

1. Measure RMS at 5 frequency bands (15k, 17k, 19k, 20.5k, 21.5k Hz) plus an
   unfiltered overall RMS — all in a single ffmpeg call via asplit=6.
2. Compute the drop between each adjacent pair of bands.
3. If any adjacent drop exceeds BRICKWALL_DROP_DB (25 dB), that is a codec
   brickwall — not a natural roll-off — and the file is flagged as
   "Suspected Transcode (Lossy)".

The analysis frequencies are constant regardless of sample rate because the
encoder cutoff does not move with the container — a 96 kHz FLAC transcoded
from MP3 still has its brickwall at ~20 kHz, not at 40 kHz.

Design decisions:

- **Multi-point over two-point** — A single-drop test at 18→21.5 kHz misses
  128 kbps MP3 brickwalls at 16 kHz.  Measuring at 15/17/19/20.5/21.5 kHz
  catches brickwalls at any common encoder cutoff.
- **RMS peak-hold over sample peak** — `astats` reports the loudest short-window
  RMS across the scan. This preserves sparse full-bandwidth passages without
  letting a single click dominate the measurement.
- **Maximum adjacent drop** — A brickwall concentrates its energy gap into a
  single adjacent band pair; a natural roll-off distributes the drop evenly.
- **Fixed analysis frequencies** — Codified in BRICKWALL_DROP_DB / LOW_ENERGY_DB
  / NO_HF_DB so the thresholds are easy to tune after field experience.
- **Middle 60 seconds** — Seeks to the file midpoint and scans 60 s.
  The middle is most representative — it avoids intro silence and fade-outs
  while keeping scans fast.
- **Hi-res check is separate** — 88.2/96/192 kHz files with no energy above
  25 kHz are flagged as upsampled, not as lossy transcodes.

Guards:

- Overall RMS < LOW_ENERGY_DB (-50 dB) -> "Inconclusive (Low Energy)".
  Catches silence, ambient tracks, and long silent intros.
- RMS(highpass@15k) < NO_HF_DB (-72 dB) -> "Inconclusive (Low Energy)".
  No HF content at all to measure (naturally bass-heavy track, or a very
  low-bitrate transcode below our analysis window).
- Lossy codecs -> "N/A (Lossy Codec)".  The file already declares itself lossy.
- DSD -> "Lossless Codec (DSD)".  DSD is treated as out of scope for the
  PCM brickwall detector.
"""

import re
import subprocess
from pathlib import Path

from tool.music.constants import DSD, GREY, LOSSLESS, LOSSY_ALL, MAGENTA, RED

VERDICT_W = 29

# Thresholds (all dB, negative)
BRICKWALL_DROP_DB = 25  # min drop between adjacent HF bands to flag as a codec brickwall
LOW_ENERGY_DB = -50  # overall RMS below this = track too quiet to analyse
NO_HF_DB = -72  # RMS in 15 kHz band below this = no HF content to measure
HI_RES_NO_HF_DB = -50  # RMS above 25 kHz below this = likely 44.1/48 kHz upsample

# Analysis frequencies for the multi-point slope profile (Hz)
HP_FREQS = (15000, 17000, 19000, 20500, 21500)
HI_RES_HP_FREQ = 25000
NUM_BANDS = len(HP_FREQS) + 1  # +1 for unfiltered overall


def analyze_steepness(filepath: Path, sample_rate: int, duration_s: float | None = None) -> dict[str, float] | None:
    """Run the multi-point HF steepness test (see module docs for methodology).

    Uses a single ffmpeg call with asplit=N so the file is decoded once.
    If *duration_s* is given and the file is longer than 60 s, seeks to the
    midpoint and scans 60 s.  Returns {'rms_overall', 'rms_peak_overall',
    'rms_peak_15000', 'rms_peak_17000', 'rms_peak_19000', 'rms_peak_20500',
    'rms_peak_21500'} in dB, plus 'rms_peak_25000' for hi-res material, or
    None on failure.
    """
    if sample_rate < 44100:
        return None

    include_hi_res = sample_rate > 48000
    hp_freqs = (*HP_FREQS, HI_RES_HP_FREQ) if include_hi_res else HP_FREQS
    num_bands = len(hp_freqs) + 1

    # Build the filter graph: asplit=N for each band, each measured with astats
    labels = [chr(ord("a") + i) for i in range(num_bands)]
    splits = "".join(f"[{c}]" for c in labels)
    astats = "astats=measure_overall=RMS_level+RMS_peak:measure_perchannel=none"
    chains = [f"[a]{astats}[aout]"]  # unfiltered
    for i, freq in enumerate(hp_freqs):
        c = labels[i + 1]
        energy_above = f"firequalizer=gain='if(gte(f\\,{freq})\\,0\\,-120)'"
        chains.append(f"[{c}]{energy_above},{astats}[{c}out]")

    filter_graph = f"asplit={num_bands}{splits};" + ";".join(chains)

    # Seek to the middle of the file for a representative 60 s sample.
    # When duration is unknown or the file is short, scan the whole file.
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
                "-filter_complex",
                filter_graph,
                "-map",
                "[aout]",
                *[item for c in labels[1:] for item in ("-map", f"[{c}out]")],
                "-f",
                "null",
                "/dev/null",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired, OSError:
        return None

    if result.returncode != 0:
        return None

    rms_level_matches = re.findall(r"RMS\s+level\s+dB:\s*(-?\d+\.?\d*|-inf)", result.stderr)
    rms_peak_matches = re.findall(r"RMS\s+peak\s+dB:\s*(-?\d+\.?\d*|-inf)", result.stderr)
    if len(rms_level_matches) != num_bands or len(rms_peak_matches) != num_bands:
        return None

    def _parse(v: str) -> float:
        return -200.0 if "inf" in v else float(v)

    # The astats branches can finish in any order, but both total RMS and
    # short-window RMS peak decrease as the analysis frequency rises.  Sort
    # descending to assign the unfiltered signal followed by ascending bands.
    rms_levels = sorted((_parse(v) for v in rms_level_matches), reverse=True)
    rms_peaks = sorted((_parse(v) for v in rms_peak_matches), reverse=True)

    spec = {
        "rms_overall": rms_levels[0],
        "rms_peak_overall": rms_peaks[0],
        "rms_peak_15000": rms_peaks[1],
        "rms_peak_17000": rms_peaks[2],
        "rms_peak_19000": rms_peaks[3],
        "rms_peak_20500": rms_peaks[4],
        "rms_peak_21500": rms_peaks[5],
    }
    if include_hi_res:
        spec["rms_peak_25000"] = rms_peaks[6]
    return spec


def compute_verdict(
    codec: str,
    filepath: Path,
    sample_rate: int | None,
    *,
    duration_s: float | None = None,
    brickwall_threshold: float = BRICKWALL_DROP_DB,
    low_energy_threshold: float = LOW_ENERGY_DB,
    no_hf_threshold: float = NO_HF_DB,
    hi_res_no_hf_threshold: float = HI_RES_NO_HF_DB,
) -> tuple[str, int, bool]:
    """Return (verdict_text, color_code, dim) for an audio file.

    Lossy codec      -> "N/A (Lossy Codec)"
    DSD              -> "Lossless Codec (DSD)"
    Unknown codec    -> "Unknown Codec"
    Low sample rate  -> "Inconclusive (Low SR)"
    Low overall RMS  -> "Inconclusive (Low Energy)"
    No HF content    -> "Inconclusive (Low Energy)"
    Steep brickwall  -> "Suspected Transcode (Lossy)"
    Hi-res upsample  -> "Likely Upsampled"
    Otherwise        -> "No Brickwall Detected"
    """
    cl = codec.lower()

    if cl in LOSSY_ALL:
        return ("N/A (Lossy Codec)", GREY, True)

    if cl in DSD:
        return ("Lossless Codec (DSD)", MAGENTA, False)

    if cl not in LOSSLESS:
        return ("Unknown Codec", GREY, True)

    if sample_rate is None or sample_rate < 44100:
        return ("Inconclusive (Low SR)", GREY, True)

    spec = analyze_steepness(filepath, sample_rate, duration_s)
    if spec is None:
        return ("Error", RED, False)

    rms_peak_overall = spec["rms_peak_overall"]
    rms_15000 = spec["rms_peak_15000"]

    if rms_peak_overall < low_energy_threshold:
        return ("Inconclusive (Low Energy)", GREY, True)

    if rms_15000 < no_hf_threshold:
        return ("Inconclusive (Low Energy)", GREY, True)

    # Check the maximum adjacent drop across all band pairs
    band_values = [
        spec["rms_peak_overall"],
        spec["rms_peak_15000"],
        spec["rms_peak_17000"],
        spec["rms_peak_19000"],
        spec["rms_peak_20500"],
        spec["rms_peak_21500"],
    ]
    max_drop = max(band_values[i] - band_values[i + 1] for i in range(len(band_values) - 1))
    if max_drop > brickwall_threshold:
        return ("Suspected Transcode (Lossy)", RED, False)

    if sample_rate > 48000 and spec.get("rms_peak_25000", -200.0) < hi_res_no_hf_threshold:
        return ("Likely Upsampled", GREY, True)

    return ("No Brickwall Detected", MAGENTA, False)
