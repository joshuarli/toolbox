#!/usr/bin/env python3
"""
Normalize audio loudness to -16 LUFS (EBU R128).
Video and subtitles are stream-copied; only audio is re-encoded.

Two-pass: measure integrated loudness first, then apply as volume= gain.
The volume= filter preserves source sample format, which is required because
aac_at (AudioToolbox) rejects the fltp output of the loudnorm filter directly.

Usage: movies-cli loudnorm <file> [file ...]
"""

import json
import re
import subprocess
import sys
import threading
from pathlib import Path

from tool.movies import probe

LOUDNORM_I = -16.0
LOUDNORM_TP = -1.5
LOUDNORM_LRA = 11.0


def measure_gain(src: Path) -> tuple[float, float]:
    """Decode audio only; return (target_offset_db, input_i_lufs).

    target_offset already accounts for the TP ceiling — it's the net dB gain
    loudnorm would apply in a linear second pass.
    """
    r = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-i",
            str(src),
            "-map",
            "0:a:0",
            "-af",
            f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}:print_format=json",
            "-f",
            "null",
            "/dev/null",
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    m = re.search(r"\{[^{}]+\}", r.stderr, re.DOTALL)
    if not m:
        return 0.0, 0.0
    try:
        data = json.loads(m.group())
        return float(data["target_offset"]), float(data["input_i"])
    except KeyError, ValueError, json.JSONDecodeError:
        return 0.0, 0.0


def build_cmd(src: Path, dst: Path, info: probe.ProbeInfo, gain_db: float) -> list[str]:
    info = probe.with_estimated_bitrates(src, info)
    audio = info.audio_streams[0] if info.audio_streams else None
    channels = audio.channels if audio else 2
    source_kbps = round(audio.bit_rate / 1000) if audio and audio.bit_rate else None

    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-i",
        str(src),
        # Map every stream — video, all audio tracks, subtitles, attachments, data.
        # Default to copy; only override audio stream 0.
        "-map",
        "0",
        "-c",
        "copy",
        "-c:a:0",
        "aac_at",
        "-b:a:0",
        probe.target_audio_bitrate(channels, source_kbps or None),
        "-filter:a:0",
        f"volume={gain_db:.2f}dB",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        str(dst),
    ]


def _fmt_size(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1e9:.2f} GB"
    if n >= 1_000_000:
        return f"{n / 1e6:.1f} MB"
    return f"{n / 1000:.0f} KB"


def _run(cmd: list[str], duration: float) -> tuple[bool, str]:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    assert proc.stderr is not None
    stdout = proc.stdout
    stderr = proc.stderr
    stderr_buf: list[str] = []
    threading.Thread(target=lambda: stderr_buf.append(stderr.read()), daemon=True).start()

    bar_w = 34
    for raw in stdout:
        if not raw.startswith("out_time_us="):
            continue
        try:
            secs = int(raw.strip().split("=", 1)[1]) / 1_000_000
        except ValueError:
            continue
        if duration > 0:
            pct = min(secs / duration, 1.0)
            filled = int(bar_w * pct)
            bar = "█" * filled + "░" * (bar_w - filled)
            print(f"\r  [{bar}] {pct * 100:5.1f}%", end="", flush=True)

    print()
    returncode = proc.wait()
    return returncode == 0, stderr_buf[0] if stderr_buf else ""


def normalize(src: Path) -> None:
    if not src.is_file():
        sys.exit(f"Error: not a file: {src}")

    dst = src.with_name(f"{src.stem}-loudnorm{src.suffix}")
    if dst.exists():
        print(f"Output already exists, skipping: {dst.name}")
        return

    print(f"Source: {src.name}")
    try:
        info = probe.ffprobe(src)
    except RuntimeError as e:
        sys.exit(f"Error: {e}")

    duration = info.format.duration

    print("  Pass 1/2  measuring loudness...")
    gain_db, input_i = measure_gain(src)
    print(f"  Measured: {input_i:.1f} LUFS  target: {LOUDNORM_I:.0f} LUFS  gain: {gain_db:+.1f} dB")

    if abs(gain_db) < 0.1:
        print("  Already at target loudness — nothing to do.")
        return

    print(f"  Pass 2/2  re-encoding audio → {dst.name}")
    ok, err = _run(build_cmd(src, dst, info, gain_db), duration)

    if not ok:
        dst.unlink(missing_ok=True)
        if err.strip():
            print(f"ffmpeg:\n{err.strip()}", file=sys.stderr)
        sys.exit("Failed.")

    src_sz = src.stat().st_size
    dst_sz = dst.stat().st_size
    pct = (dst_sz - src_sz) / src_sz * 100
    print(f"Done:   {_fmt_size(src_sz)} → {_fmt_size(dst_sz)}  ({'+' if pct >= 0 else ''}{pct:.1f}%)")


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print(f"Usage: {Path(sys.argv[0]).name} <file> [file ...]")
        sys.exit(0)
    for arg in args:
        normalize(Path(arg))


if __name__ == "__main__":
    main()
