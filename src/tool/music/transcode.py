"""Audio transcoding via ffmpeg.  Convert audio files to high-quality AAC
in an M4A container, preserving channel layout and copying metadata.

Accepts both files and directories.  Directories are walked recursively
for known audio extensions.
"""

import argparse
import json
import os
import subprocess
import sys
from typing import Any

from tool.music.scan import collect_files

# Prefer Apple AudioToolbox encoder on macOS (hardware-accelerated, higher quality)
_AAC_ENCODER = "aac_at" if sys.platform == "darwin" else "aac"

AAC_EXT = ".m4a"
_BITRATE = "320k"


def probe_audio(filepath: str) -> dict[str, Any] | None:
    """Return audio stream info (codec_name, channels, sample_rate) from ffprobe.

    Returns None if no audio stream could be found or ffprobe fails.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name,channels,sample_rate",
                "-of",
                "json",
                filepath,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        return streams[0] if streams else None
    except Exception:
        return None


def probe_codec(filepath: str) -> str | None:
    """Return the audio codec name from ffprobe, or None on failure."""
    info = probe_audio(filepath)
    if info is None or not info:
        return None
    codec = info.get("codec_name")
    return str(codec) if codec else None


def transcode_to_aac(filepath: str, dst: str | None = None, *, overwrite: bool = False) -> str | None:
    """Transcode *filepath* to AAC at 320k in an M4A container.

    Preserves channel layout and copies metadata from the source.
    If *dst* is given, writes there; otherwise derives the path from
    *filepath* by replacing the extension with .m4a. Existing output is refused
    unless *overwrite* is true.
    Returns the path to the new file, or None on failure.
    """
    if dst is None:
        base = os.path.splitext(filepath)[0]
        dst = base + AAC_EXT

    # ffmpeg can return success after logging that -n refused an existing output.
    if not overwrite and os.path.lexists(dst):
        return None

    try:
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-y" if overwrite else "-n",
                "-i",
                filepath,
                "-c:a",
                _AAC_ENCODER,
                "-b:a",
                _BITRATE,
                "-vn",
                "-map_metadata",
                "0",
                dst,
            ],
            stdout=subprocess.DEVNULL,
            check=True,
        )
    except subprocess.CalledProcessError:
        return None

    return dst


def configure_parser(p: argparse.ArgumentParser) -> None:
    p.description = "Transcode audio files to high-quality AAC in an M4A container."
    p.add_argument("paths", nargs="*", default=["."], help="audio files or directories to transcode")
    p.add_argument("-o", "--output", help="output path (only valid for a single input file)")
    p.add_argument("--overwrite", action="store_true", help="replace existing output files")


def run(args: argparse.Namespace) -> None:
    files = collect_files(args.paths)

    if not files:
        print("No audio files found.", file=sys.stderr)
        sys.exit(1)

    if args.output and len(files) > 1:
        print("error: -o/--output can only be used with a single input file", file=sys.stderr)
        sys.exit(1)

    success = 0
    failed = 0
    for fp, _ in files:
        src = str(fp)
        info = probe_audio(src)
        if info is None:
            print(f"error: no audio stream found in {src}", file=sys.stderr)
            failed += 1
            continue

        codec = info.get("codec_name", "?")
        channels = info.get("channels")
        ch_map = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}
        ch_label = ch_map.get(channels, f"{channels}ch") if channels else "?"

        dst = args.output or None
        result = transcode_to_aac(src, dst=dst, overwrite=args.overwrite)
        if result is None:
            print(f"error: transcode failed: {src}", file=sys.stderr)
            failed += 1
        else:
            if len(files) > 1:
                print(f"{src} → {result} ({codec}, {ch_label})")
            else:
                print(f"Source: {codec}, {ch_label}")
                print(f"Wrote: {result}")
            success += 1

    if failed:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    configure_parser(parser)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
