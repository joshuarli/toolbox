#!/usr/bin/env python3
"""
Merge sidecar .srt files into MP4 containers as additional mov_text subtitle streams.

Usage: movies-cli merge-srt <directory>

For each pair (filename.mp4, filename.srt) in the directory, the SRT is merged
as an additional subtitle stream — existing streams in the MP4 are preserved.
On success the .srt is trashed.
"""

import subprocess
import sys
from pathlib import Path

# Map common filename language tags to ISO 639-2/B codes.
LANG_MAP: dict[str, str] = {
    "en": "eng",
    "eng": "eng",
    "zh": "zho",
    "chi": "zho",
    "zh-hans": "zho",
    "zh-hant": "zho",
    "ja": "jpn",
    "jpn": "jpn",
    "ko": "kor",
    "kor": "kor",
    "fr": "fra",
    "fre": "fra",
    "fra": "fra",
    "de": "deu",
    "ger": "deu",
    "deu": "deu",
    "it": "ita",
    "ita": "ita",
    "es": "spa",
    "spa": "spa",
    "pt": "por",
    "por": "por",
    "ru": "rus",
    "rus": "rus",
    "ar": "ara",
    "ara": "ara",
    "hi": "hin",
    "hin": "hin",
    "th": "tha",
    "tha": "tha",
    "vi": "vie",
    "vie": "vie",
    "tr": "tur",
    "tur": "tur",
    "pl": "pol",
    "pol": "pol",
    "nl": "nld",
    "dut": "nld",
    "nld": "nld",
    "sv": "swe",
    "swe": "swe",
    "da": "dan",
    "dan": "dan",
    "fi": "fin",
    "fin": "fin",
    "no": "nor",
    "nor": "nor",
    "cs": "ces",
    "cze": "ces",
    "ces": "ces",
    "ro": "ron",
    "rum": "ron",
    "ron": "ron",
    "hu": "hun",
    "hun": "hun",
    "el": "ell",
    "gre": "ell",
    "ell": "ell",
    "he": "heb",
    "heb": "heb",
    "id": "ind",
    "ind": "ind",
    "ms": "msa",
    "may": "msa",
    "msa": "msa",
    "uk": "ukr",
    "ukr": "ukr",
    "fa": "fas",
    "per": "fas",
    "fas": "fas",
    "bn": "ben",
    "ben": "ben",
    "ta": "tam",
    "tam": "tam",
    "te": "tel",
    "tel": "tel",
    "ur": "urd",
    "urd": "urd",
    "tl": "tgl",
    "fil": "fil",
    "tgl": "tgl",
}


def _detect_srt_encoding(srt_path: Path) -> str | None:
    """Detect the character encoding of an SRT file.

    Returns an ffmpeg-compatible charset name (e.g. 'ISO-8859-1'), or None
    if the file is already valid UTF-8.
    """
    # Quick check: try reading as UTF-8 first.
    try:
        srt_path.read_text(encoding="utf-8")
        return None  # Valid UTF-8, no -sub_charenc needed.
    except UnicodeDecodeError:
        pass

    # Fall back to `file -I` for MIME charset detection.
    try:
        result = subprocess.run(
            ["file", "-bI", str(srt_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    charset = ""
    for part in result.stdout.strip().split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            charset = part.split("=", 1)[1].strip()
            break

    if not charset or charset.lower() in ("utf-8", "us-ascii", "binary", "unknown-8bit"):
        return None

    # Map common charset names to ffmpeg-compatible values.
    charset_lower = charset.lower()
    # Some `file` spellings that ffmpeg doesn't recognize directly.
    remap = {
        "iso-8859-1": "ISO-8859-1",
        "iso-8859-2": "ISO-8859-2",
        "iso-8859-5": "ISO-8859-5",
        "iso-8859-7": "ISO-8859-7",
        "iso-8859-9": "ISO-8859-9",
        "iso-8859-15": "ISO-8859-15",
        "windows-1250": "CP1250",
        "windows-1251": "CP1251",
        "windows-1252": "CP1252",
        "windows-1253": "CP1253",
        "windows-1254": "CP1254",
        "windows-1255": "CP1255",
        "windows-1256": "CP1256",
        "windows-1257": "CP1257",
        "windows-1258": "CP1258",
        "koi8-r": "KOI8-R",
        "koi8-u": "KOI8-U",
        "gb2312": "GB2312",
        "gbk": "GBK",
        "gb18030": "GB18030",
        "big5": "BIG5",
        "shift_jis": "SHIFT_JIS",
        "euc-jp": "EUC-JP",
        "euc-kr": "EUC-KR",
    }
    return remap.get(charset_lower, charset.upper())


def _detect_language(srt_path: Path) -> str | None:
    """Try to extract a language code from the SRT filename.

    Handles patterns like:
        movie.en.srt      → eng
        movie.eng.srt     → eng
        movie.zh-Hans.srt → zho
    """
    # Remove the .srt extension, then look at the last dotted segment.
    stem = srt_path.stem  # e.g. "movie.en" or "movie" or "movie.zh-Hans"
    parts = stem.rsplit(".", 1)
    if len(parts) < 2:
        return None
    candidate = parts[1].lower()
    return LANG_MAP.get(candidate)


def _fmt_size(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1e9:.2f} GB"
    if n >= 1_000_000:
        return f"{n / 1e6:.1f} MB"
    return f"{n / 1000:.0f} KB"


def find_srt_pairs(directory: Path) -> list[tuple[Path, Path]]:
    """Find (video, srt) pairs where stems match and both files exist.

    Matches .mp4 and .mkv video files. Multiple SRTs can target the same
    video (e.g. movie.en.srt and movie.fr.srt both matching movie.mp4).
    Each is returned as its own pair and will be merged sequentially.
    """
    pairs: list[tuple[Path, Path]] = []

    for srt in sorted(directory.glob("*.srt")):
        stem = srt.stem  # e.g. "movie" or "movie.en"
        # Try exact stem match first (stem == video.stem), but also handle
        # the case where the SRT has a language suffix (e.g. movie.en.srt
        # matching movie.mp4).  In that case the SRT stem is "movie.en"
        # and the video stem is "movie", so we walk the stem parts.
        candidates = [stem]
        while "." in stem:
            stem = stem.rsplit(".", 1)[0]
            candidates.append(stem)

        for cand in candidates:
            for ext in (".mp4", ".mkv"):
                video = directory / f"{cand}{ext}"
                if video.is_file():
                    pairs.append((video, srt))
                    break
            else:
                continue
            break

    return pairs


def merge_one(video_path: Path, srt_path: Path, sub_charenc: str | None = None) -> bool:
    """Merge srt_path into video_path as an additional subtitle stream.

    Existing streams are preserved. The operation is atomic: ffmpeg writes to a
    temporary file, then the temp file replaces the source on success.

    MP4 containers get mov_text subtitles and faststart moov rewriting.
    MKV containers get native subrip (SRT) subtitles via stream copy.
    """
    suffix = video_path.suffix.lower()
    is_mp4 = suffix == ".mp4"

    # Reject SRT files that aren't valid text (all nulls, binary garbage, etc.).
    try:
        raw = srt_path.read_bytes()
        if not raw.strip():
            print("  SRT file is empty — skipping", file=sys.stderr)
            return False
        # Detect files that are mostly null bytes (corrupted).
        null_ratio = raw.count(0) / len(raw) if raw else 0
        if null_ratio > 0.9:
            print(f"  SRT file appears corrupted ({null_ratio:.0%} null bytes) — skipping", file=sys.stderr)
            return False
    except OSError as e:
        print(f"  Could not read SRT file: {e}", file=sys.stderr)
        return False

    lang = _detect_language(srt_path)
    tmp = video_path.with_name(video_path.stem + f".merge-tmp{suffix}")

    # The new SRT stream will be the N-th subtitle stream in the output,
    # where N = number of existing subtitle streams (0-indexed).
    n_subs = 0
    if lang:
        try:
            from tool.movies import probe

            info = probe.ffprobe(video_path)
            n_subs = len(info.subtitle_streams)
        except Exception:
            pass

    # Clean up any stale temp file from a previous failed run.
    tmp.unlink(missing_ok=True)

    if is_mp4:
        # MP4 only supports mov_text; convert all subtitle streams.
        # faststart rewrites the moov atom to the front for streaming.
        sub_args = ["-c:s", "mov_text"]
        mux_args = ["-movflags", "+faststart"]
    else:
        # MKV supports subrip (SRT) natively; just stream-copy everything.
        sub_args = []
        mux_args = []

    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        *(["-sub_charenc", sub_charenc] if sub_charenc else []),
        "-i",
        str(srt_path),
        "-map",
        "0",
        "-map",
        "1",
        "-c",
        "copy",
        *sub_args,
        *([f"-metadata:s:s:{n_subs}", f"language={lang}"] if lang else []),
        *mux_args,
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        str(tmp),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        tmp.unlink(missing_ok=True)
        print(f"  Error: ffmpeg failed: {e}", file=sys.stderr)
        return False

    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        if result.stderr.strip():
            print(f"  ffmpeg: {result.stderr.strip()}", file=sys.stderr)
        return False

    # Safety: refuse to replace the source with a zero-byte or missing output.
    if not tmp.is_file() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        print("  Error: ffmpeg produced empty output", file=sys.stderr)
        return False

    # Atomic replacement: rename temp over source.
    try:
        tmp.replace(video_path)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        print(f"  Error: could not replace {video_path.name}: {e}", file=sys.stderr)
        return False

    return True


def trash_file(path: Path) -> bool:
    """Move a file to the Trash using the macOS trash CLI."""
    try:
        subprocess.run(
            ["trash", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return True
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"  Warning: could not trash {path.name}: {e}", file=sys.stderr)
        return False


def merge_all(directory: Path) -> None:
    """Find and merge all SRT/video pairs in directory."""
    if not directory.is_dir():
        sys.exit(f"Error: not a directory: {directory}")

    pairs = find_srt_pairs(directory)
    if not pairs:
        print("No .srt files found alongside .mp4 or .mkv files.")
        return

    print(f"Found {len(pairs)} SRT file{'s' if len(pairs) != 1 else ''} to merge.\n")

    merged = 0
    failed = 0

    for video_path, srt_path in pairs:
        print(f"  {video_path.name}  ←  {srt_path.name}")
        sub_charenc = _detect_srt_encoding(srt_path)
        if merge_one(video_path, srt_path, sub_charenc):
            if trash_file(srt_path):
                note = f" (decoded as {sub_charenc})" if sub_charenc else ""
                print(f"    ✓ merged, SRT trashed{note}")
            else:
                print("    ✓ merged (SRT kept — trash failed)")
            merged += 1
        else:
            print("    ✗ failed")
            failed += 1

    print(f"\n{merged} merged, {failed} failed")
    if failed:
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("Usage: movies-cli merge-srt <directory>")
        sys.exit(0)
    merge_all(Path(args[0]))


if __name__ == "__main__":
    main()
