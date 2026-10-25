#!/usr/bin/env python3
"""
Scan movie files for encode quality issues.

Usage: movies-cli scan [file-or-dir ...]   (defaults to current directory)
"""

import argparse
import math
import sys
from pathlib import Path
from typing import Any

from tool.movies import probe

RST = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[1;31m"
YELLOW = "\033[1;33m"
GREEN = "\033[1;32m"
BGREEN = "\033[92m"  # bright green — AV1 / 1440p tier
CYAN = "\033[1;36m"
MAGENTA = "\033[1;35m"
ORANGE = "\033[1;38;5;208m"  # bold orange — legendary (6K+)
PURPLE = "\033[38;5;135m"  # purple — 4K / lossless audio

FLAG_STYLE = {
    "old_codec": (RED, "OLD_CODEC"),
    "old_audio": (RED, "OLD_AUDIO"),
    "bloated": (RED, "BLOATED"),
    "thin": (YELLOW, "THIN"),
    "interlaced": (MAGENTA, "INTERLACED"),
    "not_h265": (YELLOW, "NOT_H265"),
    "low_audio_br": (YELLOW, "LOW_AUDIO_BR"),
    "mp3_audio": (CYAN, "MP3_AUDIO"),
}


def color_res(width: int, height: int) -> str:
    s = f"{width}×{height}"
    if width >= 6000:
        c = ORANGE
    elif height >= 2160:
        c = PURPLE
    elif height >= 1440:
        c = BGREEN
    elif height >= 1080:
        c = GREEN
    elif height >= 720:
        c = YELLOW
    else:
        c = RED
    return f"{c}{s}{RST}"


def color_vcodec(display: str, tier: str, codec_name: str) -> str:
    if codec_name == "av1":
        c = BGREEN
    elif tier == "A":
        c = GREEN
    elif tier == "B":
        c = YELLOW
    elif tier == "C":
        c = RED
    else:
        c = DIM
    return f"{c}{display}{RST}"


def color_acodec(display: str, tier: str) -> str:
    if tier == "L":
        c = PURPLE
    elif tier == "A":
        c = GREEN
    elif tier == "B":
        c = YELLOW
    elif tier == "C":
        c = RED
    else:
        c = DIM
    return f"{c}{display}{RST}"


def color_bpp(bpp: float, tier: str, width: int = 0, height: int = 0) -> str:
    s = f"BPP {bpp:.4f}"
    if not bpp:
        return f"{DIM}{s}{RST}"
    ranges = probe.BPP_RANGES.get(tier)
    if not ranges:
        return s
    rl, yl, yh, rh = ranges
    # Normalize low-side checks so SD content is not unfairly marked thin, but
    # keep raw BPP on the high side because bloated SD still wastes file size.
    nbpp = bpp * math.sqrt((width * height) / probe.BPP_REF_PIXELS) if (width and height) else bpp
    if nbpp < rl or nbpp > rh or bpp > rh:
        c = RED
    elif nbpp < yl or nbpp > yh or bpp > yh:
        c = YELLOW
    else:
        c = GREEN
    return f"{c}{s}{RST}"


def color_quality_score(score: int) -> str:
    s = f"Q:{score}"
    if score >= 80:
        c = GREEN
    elif score >= 72:
        c = YELLOW
    elif score:
        c = RED
    else:
        c = DIM
    return f"{c}{s}{RST}"


def color_audio_bpc(br_kbps: int | None, channels: int, tier: str) -> str | None:
    """Audio bitrate-per-channel: the audio analog of video BPP."""
    if br_kbps is None or not br_kbps or not channels or tier == "L":
        return None
    bpc = br_kbps // channels
    if bpc < 16:
        c = RED
    elif bpc < 32:
        c = YELLOW
    elif bpc < 96:
        c = GREEN
    else:
        c = CYAN
    return f"{c}{bpc}/ch{RST}"


def render_flag(flag: str) -> str:
    key = flag.split(":")[0]
    c, label = FLAG_STYLE.get(key, (CYAN, flag.upper()))
    detail = flag[len(key) :]
    return f"{c}{BOLD}[{label}{detail.upper()}]{RST}"


def render_audio_line(info: dict[str, Any]) -> str:
    parts = [color_acodec(info["codec"], info["tier"])]
    if info["ch"]:
        parts.append(info["ch"])
    if info["tier"] == "L":
        parts.append(f"{PURPLE}lossless{RST}")
    if info["br_kbps"]:
        parts.append(f"{info['br_kbps']} kbps")
    bpc = color_audio_bpc(info["br_kbps"], info["channels"], info["tier"])
    if bpc is not None and bpc:
        parts.append(bpc)
    if info["lang"]:
        parts.append(f"{DIM}({info['lang']}){RST}")
    return "  ".join(parts)


def print_result(name: str, result: dict[str, Any]) -> None:
    if "error" in result:
        print(f"{BOLD}{name}{RST}")
        print(f"  {RED}Error: {result['error']}{RST}")
        print()
        return

    v = result["video"]
    dur = result["duration_min"]
    sz = result["size_gb"]

    print(f"{BOLD}{name}{RST}  {DIM}{dur} min · {sz} GB{RST}")

    v_parts = [color_vcodec(v["codec"], v["tier"], v["codec_name"])]
    if v.get("bit_depth"):
        v_parts.append(f"{v['bit_depth']}-bit")
    v_parts += [color_res(v["width"], v["height"]), f"{v['fps']} fps"]
    if v["br_mbps"]:
        v_parts.append(f"{v['br_mbps']} Mbps")
    if v["bpp"]:
        v_parts.append(color_bpp(v["bpp"], v["tier"], v["width"], v["height"]))
    score, _ = probe.quality_score(v, result["video_flags"])
    v_parts.append(color_quality_score(score))
    print(f"  {DIM}Video{RST}  {'  '.join(v_parts)}")

    if result["audio"]:
        for ai in result["audio"]:
            print(f"  {DIM}Audio{RST}  {render_audio_line(ai)}")
    else:
        print(f"  {DIM}Audio{RST}  none")

    sub_str = "  ".join(result["subs"]) if result["subs"] else "none"
    print(f"  {DIM}Subs{RST}   {sub_str}")

    all_flags = result["video_flags"] + result["audio_flags"]
    if all_flags:
        print(f"  {'  '.join(render_flag(f) for f in all_flags)}")
    else:
        print(f"  {GREEN}{BOLD}[OK]{RST}")

    print()


def flag_severity(result: dict[str, Any]) -> int:
    weights = {
        "old_codec": 8,
        "old_audio": 7,
        "bloated": 5,
        "thin": 4,
        "low_audio_br": 3,
        "interlaced": 2,
        "mp3_audio": 2,
        "not_h265": 1,
    }
    return sum(weights.get(f.split(":")[0], 0) for f in result.get("video_flags", []) + result.get("audio_flags", []))


def collect_files(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            candidates = sorted(
                p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in probe.VIDEO_EXTENSIONS
            )
        elif path.is_file():
            candidates = [path] if path.suffix.lower() in probe.VIDEO_EXTENSIONS else []
        else:
            print(f"Skipping missing path: {path}", file=sys.stderr)
            continue

        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                files.append(candidate)
    return files


def scan(paths: list[Path]) -> None:
    files = collect_files(paths)
    if not files:
        label = ", ".join(str(path) for path in paths)
        print(f"No video files found in {label}")
        return

    results = []
    for f in files:
        print(f"  probing {f.name[:60]}...", end="\r", flush=True)
        r = probe.analyze(f)
        r["name"] = f.name
        results.append(r)
    print(" " * 70, end="\r")

    results.sort(key=lambda r: -flag_severity(r))

    print()
    for r in results:
        print_result(r["name"], r)

    flagged = sum(1 for r in results if r.get("video_flags") or r.get("audio_flags"))
    print(f"{DIM}{len(results)} files scanned, {flagged} flagged.{RST}")


def run_from_args(args: argparse.Namespace) -> None:
    scan(args.paths)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Scan movie files for encode quality issues.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("paths", nargs="*", type=Path, default=[Path(".")], help="files or directories to scan")
    run_from_args(parser.parse_args(argv))


if __name__ == "__main__":
    main()
