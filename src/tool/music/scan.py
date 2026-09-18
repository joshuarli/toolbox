"""Walk a directory for audio files and report codec, bitrate, cover art,
metadata, lossy-transcode verdict, and EBU R 128 mastering-quality verdicts.

Uses ffprobe and ffmpeg for all analysis — file extensions are ignored for
codec detection.  Prints one line per file with ANSI 256-color output.

When given a single file path (not a directory), prints a detailed per-file
breakdown instead of the tabular scan view.
"""

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from tool.music.constants import (
    AUDIO_EXTENSIONS,
    BLUE,
    DSD,
    GOLD,
    GREEN,
    GREY,
    LOSSLESS,
    LOSSY_ANCIENT,
    LOSSY_HIGH,
    LOSSY_STANDARD,
    MAGENTA,
    RED,
    TAG_NAMES,
)
from tool.music.loudness import (
    LRA_FLAT_LU,
    TRUE_PEAK_LIMIT_DBTP,
    analyze_ebur128,
    compute_mastering_verdict,
)
from tool.music.tags import read_tags
from tool.music.ui import bold, colored
from tool.music.verdict import (
    BRICKWALL_DROP_DB,
    HI_RES_NO_HF_DB,
    LOW_ENERGY_DB,
    NO_HF_DB,
    VERDICT_W,
    compute_verdict,
)

# Column widths
CODEC_W = 7
RATE_W = 7
COVER_W = 5
TAGS_W = 20


def codec_color(codec: str) -> int:
    """Return a 256-color code for a codec name (gaming quality tiers)."""
    cl = codec.lower()
    if cl in DSD or cl in LOSSLESS:
        return MAGENTA
    if cl in LOSSY_HIGH:
        return GOLD
    if cl in LOSSY_STANDARD:
        return BLUE
    if cl in LOSSY_ANCIENT:
        return RED
    return GREY


def bitrate_color(kbps: int, codec: str) -> int:
    """Return a colour for a bitrate, contextualised by codec type."""
    cl = codec.lower()
    if cl in LOSSLESS or cl in DSD:
        return MAGENTA  # bitrate is just a compression metric here
    if kbps >= 256:
        return GOLD
    if kbps >= 192:
        return BLUE
    if kbps >= 128:
        return GREEN
    if kbps >= 64:
        return GREY
    return RED


def probe(filepath: Path) -> dict[str, Any] | None:
    """Run ffprobe on *filepath* and return parsed JSON, or None on failure."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(filepath),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired, json.JSONDecodeError, OSError:
        return None


def find_audio_stream(streams: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the first real audio stream, skipping attached-pic masquerading as audio."""
    for s in streams:
        if s.get("codec_type") == "audio" and not s.get("disposition", {}).get("attached_pic"):
            return s
    for s in streams:
        if s.get("codec_type") == "audio":
            return s
    return None


def has_cover_art(streams: list[dict[str, Any]]) -> bool:
    """Return True if any stream looks like embedded cover art."""
    for s in streams:
        if s.get("disposition", {}).get("attached_pic") == 1:
            return True
        if s.get("codec_type") == "video" and s.get("codec_name") in {"mjpeg", "png", "bmp", "gif"}:
            return True
    return False


def fmt_codec(codec: str) -> str:
    """Format a codec name: truncated to CODEC_W, right-justified, coloured."""
    display = codec.upper()[:CODEC_W]
    return colored(f"{display:>{CODEC_W}s}", codec_color(codec), bold=True)


def fmt_bitrate(bitrate_bps: int | None, codec: str) -> str:
    """Format a bitrate: right-justified to RATE_W, coloured by quality tier."""
    if bitrate_bps is None:
        return colored(f"{'?':>{RATE_W}s}", 240)
    kbps = int(bitrate_bps / 1000)
    text = f"{kbps}k"
    return colored(f"{text:>{RATE_W}s}", bitrate_color(kbps, codec), bold=True)


def fmt_cover(present: bool) -> str:
    """Format cover art indicator."""
    if present:
        return colored(f"{'yes':>{COVER_W}s}", GREEN)
    else:
        return colored(f"{'no':>{COVER_W}s}", GREY)


def fmt_tags(tags: dict[str, str]) -> str:
    """Build the tags column: show only missing tag names, or a dim '·' when all present."""
    missing = []
    for label, key in TAG_NAMES:
        if key not in tags:
            missing.append(label)

    if not missing:
        return colored(f"{'·':<{TAGS_W}s}", GREEN, dim=True)

    text = ", ".join(missing)
    return colored(f"{text:<{TAGS_W}s}", RED, bold=True)


def fmt_verdict(text: str, color: int, *, dim: bool = False) -> str:
    """Format the verdict column: left-justified, coloured."""
    return colored(f"{text:<{VERDICT_W}s}", color, bold=not dim, dim=dim)


def collect_files(paths: list[str]) -> list[tuple[Path, str]]:
    """Turn a mixed list of dirs and file paths into (Path, display_name) pairs.

    For directories, recurses through files matching AUDIO_EXTENSIONS and shows
    paths relative to that directory.  For individual files the extension filter
    is skipped — pass anything and ffprobe will sort it out.  Paths that don't
    exist on disk are treated as files (the caller asked for them explicitly).
    """
    result: list[tuple[Path, str]] = []
    seen: set[Path] = set()

    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if not f.is_file():
                    continue
                if f.suffix.lower() not in AUDIO_EXTENSIONS:
                    continue
                key = f.resolve()
                if key not in seen:
                    seen.add(key)
                    result.append((f, str(f.relative_to(p))))
        else:
            key = p.resolve()
            if key not in seen:
                seen.add(key)
                result.append((p, p.name))

    return result


def _format_duration(seconds: float) -> str:
    """Format a duration in seconds to m:ss or h:mm:ss."""
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _format_size(size_bytes: int) -> str:
    """Format a file size in human-readable form."""
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _print_file_detail(
    fp: Path,
    display_path: str,
    *,
    brickwall_threshold: float,
    low_energy_threshold: float,
    no_hf_threshold: float,
    hi_res_no_hf_threshold: float,
    no_mastering: bool,
    true_peak_limit: float,
    lra_flat_limit: float,
) -> None:
    """Print a detailed single-file breakdown."""
    data = probe(fp)
    if data is None:
        print(f"error: could not probe {display_path}", file=sys.stderr)
        sys.exit(1)

    streams = data.get("streams", [])
    fmt = data.get("format", {})

    audio = find_audio_stream(streams)
    if audio is None:
        print(f"error: no audio stream found in {display_path}", file=sys.stderr)
        sys.exit(1)

    codec = audio.get("codec_name", "?")
    bitrate_str = audio.get("bit_rate") or fmt.get("bit_rate")
    bitrate = int(bitrate_str) if bitrate_str else None
    cover = has_cover_art(streams)
    sample_rate_str = audio.get("sample_rate")
    sample_rate = int(sample_rate_str) if sample_rate_str else None
    bit_depth = audio.get("bits_per_raw_sample") or audio.get("bits_per_sample")
    channels = audio.get("channels")
    duration_s = float(fmt.get("duration", 0))
    file_size = fp.stat().st_size
    channel_map = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}

    verdict_text, verdict_color, verdict_dim = compute_verdict(
        codec,
        fp,
        sample_rate,
        duration_s=duration_s,
        brickwall_threshold=brickwall_threshold,
        low_energy_threshold=low_energy_threshold,
        no_hf_threshold=no_hf_threshold,
        hi_res_no_hf_threshold=hi_res_no_hf_threshold,
    )

    sep = colored("  " + "─" * 60, 240)

    print()
    print(f"  {bold('File:')}     {display_path}")
    print(f"  {bold('Size:')}     {_format_size(file_size)}")
    print(f"  {bold('Duration:')} {_format_duration(duration_s)}")
    print(sep)

    print(f"  {bold('Codec:')}    {colored(codec.upper(), codec_color(codec), bold=True)}")
    if bitrate:
        print(f"  {bold('Bitrate:')}  {bitrate // 1000}k")
    print(
        f"  {bold('Sample:')}   {sample_rate or '?'} Hz, {bit_depth or '?'} bit, {channel_map.get(channels, f'{channels}ch') if channels else '?'}"
    )
    print(f"  {bold('Cover:')}    {'yes' if cover else 'no'}")

    if not no_mastering:
        loud_spec = analyze_ebur128(fp, duration_s)
        if loud_spec:
            master = compute_mastering_verdict(
                loud_spec, true_peak_limit=true_peak_limit, lra_flat_limit=lra_flat_limit
            )
            # Low LRA is expected in ambient/drone/minimalist music and
            # lossy codecs already compromise dynamics.
            if (
                master
                and master[0] == "Dynamically Flat"
                and codec.lower() not in LOSSLESS
                and codec.lower() not in DSD
            ):
                master = None
            peak_color = RED if loud_spec["true_peak_dbtp"] > true_peak_limit else GREY
            master_parts = []
            master_parts.append(colored(f"{loud_spec['integrated_lufs']:+.1f} LUFS", GREY))
            master_parts.append(
                colored(f"{loud_spec['lra']:.1f} LU LRA", GREY, bold=bool(master and master[0] == "Dynamically Flat"))
            )
            master_parts.append(
                colored(
                    f"{loud_spec['true_peak_dbtp']:+.1f} dBTP",
                    peak_color,
                    bold=loud_spec["true_peak_dbtp"] > true_peak_limit,
                )
            )
            if master:
                master_parts.append(colored(master[0], master[1], bold=not master[2], dim=master[2]))
            print(f"  {bold('Mast:')}  {'  |  '.join(master_parts)}")
        else:
            print(f"  {bold('Mast:')}    {colored('(error)', RED)}")

    print(f"  {bold('Verdict:')}  {colored(verdict_text, verdict_color, bold=not verdict_dim, dim=verdict_dim)}")
    print(sep)

    tags = read_tags(str(fp))
    if tags:
        label_w = max((len(k) for k in tags), default=0)
        print(f"  {bold('Tags:')}")
        for key in sorted(tags.keys()):
            print(f"    {colored(key, GREEN):<{label_w + 13}s} {tags[key]}")
    else:
        print(f"  {bold('Tags:')}    (none)")
    print(sep)

    print(f"  {bold('Streams:')}")
    for i, s in enumerate(streams, 1):
        stype = s.get("codec_type", "?")
        sname = s.get("codec_name", "?")
        srate = s.get("sample_rate", "?")
        sch = s.get("channels", "?")
        sbits = s.get("bits_per_raw_sample") or s.get("bits_per_sample") or "?"
        disp = s.get("disposition", {})
        note = ""
        if disp.get("attached_pic"):
            note = " (cover art)"
        elif stype == "video" and sname in {"mjpeg", "png", "bmp", "gif"}:
            note = " (embedded image)"
        print(
            f"    #{i}  {stype:<6s} {sname:<8s} {srate} Hz  {channel_map.get(sch, f'{sch}ch') if isinstance(sch, int) else str(sch)}  {sbits}b{note}"
        )


def configure_parser(p: argparse.ArgumentParser) -> None:
    p.description = (
        "Walk directories for audio files and report codec, "
        "bitrate, cover art, missing tags, and a lossy-transcode verdict. "
        "Given a single file, prints a detailed breakdown."
    )
    p.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=os.cpu_count() or 4,
        help="number of parallel workers (default: cpu count)",
    )
    p.add_argument(
        "--brickwall-threshold",
        type=float,
        default=BRICKWALL_DROP_DB,
        help=f"min dB drop between adjacent HF bands to flag a brickwall (default: {BRICKWALL_DROP_DB})",
    )
    p.add_argument(
        "--low-energy-db",
        type=float,
        default=LOW_ENERGY_DB,
        help=f"overall RMS below this is too quiet to analyse (default: {LOW_ENERGY_DB})",
    )
    p.add_argument(
        "--no-hf-db",
        type=float,
        default=NO_HF_DB,
        help=f"RMS in 15 kHz band below this = no HF content (default: {NO_HF_DB})",
    )
    p.add_argument(
        "--hi-res-no-hf-db",
        type=float,
        default=HI_RES_NO_HF_DB,
        help=f"RMS above 25 kHz below this = likely upsampled hi-res file (default: {HI_RES_NO_HF_DB})",
    )
    p.add_argument(
        "--no-mastering",
        action="store_true",
        help="skip the EBU R 128 loudness pass (faster, no loudness verdicts)",
    )
    p.add_argument(
        "--true-peak-limit",
        type=float,
        default=TRUE_PEAK_LIMIT_DBTP,
        help=f"true peak above this dBTP is flagged as clipping (default: {TRUE_PEAK_LIMIT_DBTP})",
    )
    p.add_argument(
        "--lra-flat-limit",
        type=float,
        default=LRA_FLAT_LU,
        help=f"LRA below this LU is flagged as dynamically flat (default: {LRA_FLAT_LU})",
    )
    p.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="directories or files to scan",
    )


def run(args: argparse.Namespace) -> None:
    try:
        subprocess.run(
            ["ffprobe", "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except subprocess.CalledProcessError, FileNotFoundError:
        print("error: ffprobe not found. Install ffmpeg.", file=sys.stderr)
        sys.exit(1)

    verdict_kwargs = {
        "brickwall_threshold": args.brickwall_threshold,
        "low_energy_threshold": args.low_energy_db,
        "no_hf_threshold": args.no_hf_db,
        "hi_res_no_hf_threshold": args.hi_res_no_hf_db,
    }
    detail_kwargs = {
        **verdict_kwargs,
        "no_mastering": args.no_mastering,
        "true_peak_limit": args.true_peak_limit,
        "lra_flat_limit": args.lra_flat_limit,
    }

    files = collect_files(args.paths)

    if not files:
        print("No audio files found.")
        return

    # Single file (not a directory) → detailed view
    if len(files) == 1 and len(args.paths) == 1 and Path(args.paths[0]).is_file():
        _print_file_detail(files[0][0], files[0][1], **detail_kwargs)
        return

    header = (
        f"  {'CODEC':>{CODEC_W}s}  {'RATE':>{RATE_W}s}  {'VERDICT':<{VERDICT_W}s}  "
        f"{'COVER':>{COVER_W}s}  {'MISSING':<{TAGS_W}s}  PATH"
    )
    print(colored(header, 15, bold=True))
    print(colored("  " + "─" * (len(header) - 2), 240))

    def _process_one(fp_display: tuple[Path, str]) -> str | None:
        fp, display_path = fp_display
        data = probe(fp)
        if data is None:
            return None

        streams = data.get("streams", [])
        fmt = data.get("format", {})

        audio = find_audio_stream(streams)
        if audio is None:
            return None

        codec = audio.get("codec_name", "?")
        bitrate_str = audio.get("bit_rate") or fmt.get("bit_rate")
        bitrate = int(bitrate_str) if bitrate_str else None
        cover = has_cover_art(streams)

        sample_rate_str = audio.get("sample_rate")
        sample_rate = int(sample_rate_str) if sample_rate_str else None
        duration_str = fmt.get("duration")
        duration_s = float(duration_str) if duration_str else None
        brick_text, brick_color, brick_dim = compute_verdict(
            codec, fp, sample_rate, duration_s=duration_s, **verdict_kwargs
        )

        findings: list[tuple[str, int, bool]] = []
        if brick_text == "Suspected Transcode (Lossy)":
            findings.append((brick_text, brick_color, brick_dim))

        if not args.no_mastering:
            loud_spec = analyze_ebur128(fp, duration_s)
            if loud_spec:
                master = compute_mastering_verdict(
                    loud_spec, true_peak_limit=args.true_peak_limit, lra_flat_limit=args.lra_flat_limit
                )
                if master:
                    master_text = master[0]
                    # Low LRA is expected in ambient/drone/minimalist music
                    # and lossy codecs already compromise dynamics.  Only flag
                    # it for lossless/DSD where preserved dynamics matter.
                    if master_text == "Dynamically Flat" and codec.lower() not in LOSSLESS and codec.lower() not in DSD:
                        pass
                    else:
                        findings.append(master)

        if findings:
            if len(findings) == 1:
                verdict_text, verdict_color, verdict_dim = findings[0]
            else:
                parts = []
                for text, _, _ in findings:
                    if text == "Suspected Transcode (Lossy)":
                        parts.append("Suspected Transcode")
                    elif text == "True Peak Clipping":
                        parts.append("Clip")
                    elif text == "Dynamically Flat":
                        parts.append("Flat")
                    else:
                        parts.append(text)
                verdict_text = ", ".join(parts)
                verdict_color = RED
                verdict_dim = False
        else:
            verdict_text, verdict_color, verdict_dim = brick_text, brick_color, brick_dim

        tags = read_tags(str(fp))

        return (
            f"  {fmt_codec(codec)}  {fmt_bitrate(bitrate, codec)}  "
            f"{fmt_verdict(verdict_text, verdict_color, dim=verdict_dim)}  "
            f"{fmt_cover(cover)}  "
            f"{fmt_tags(tags)}  "
            f"{colored(display_path, 51)}"
        )

    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        for line in ex.map(_process_one, files):
            if line is not None:
                print(line)


def main() -> None:
    parser = argparse.ArgumentParser()
    configure_parser(parser)
    run(parser.parse_args())
