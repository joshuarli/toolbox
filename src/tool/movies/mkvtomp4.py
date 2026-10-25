#!/usr/bin/env python3
"""
Remux MKV to MP4 with -c copy (no video/audio re-encoding).

Usage: movies-cli mkvtomp4 [--apply] <file>

Supported streams are copied or converted into the MP4 container.
Unsupported streams (DTS, FLAC, ASS, PGS, etc.) are extracted to
sidecar files, one per stream.

Refuses to operate on:
  - Non-MKV containers
  - Files with unsupported video codecs (vp8, theora, vc1, wmv, etc.)
  - Files with no video stream

Dry run (default) prints what would happen; --apply performs the remux
and extractions.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from tool.movies.probe import ProbeStream, ffprobe, is_image_video_stream

# ── codec support matrices for MP4 remux ────────────────────────────────

# Video codecs ffmpeg can copy into MP4 without re-encoding.
MP4_VIDEO_CODECS = {
    "h264",
    "hevc",
    "mpeg4",
    "vp9",
    "av1",
}

# Audio codecs ffmpeg can copy into MP4 without re-encoding.
MP4_AUDIO_CODECS = {
    "aac",
    "mp3",
    "ac3",
    "eac3",
}

# Text subtitle codecs we can convert losslessly to mov_text.
# ASS/SSA is deliberately excluded — formatting is destroyed in the
# mov_text conversion, so those streams are extracted instead.
CONVERTIBLE_SUB_CODECS = {"subrip", "srt", "webvtt", "mov_text", "text", "ttml"}

# ── extraction formats ──────────────────────────────────────────────────
# (extension, ffmpeg -f override | None).  None means ffmpeg infers from
# the extension.

EXTRACT_FORMATS: dict[str, tuple[str, str | None]] = {
    # Audio — lossless / high-bitrate formats common in MKV
    "dts": (".dts", "dts"),
    "flac": (".flac", None),
    "truehd": (".thd", "truehd"),
    "pcm_s16le": (".wav", "wav"),
    "pcm_s24le": (".wav", "wav"),
    "pcm_s32le": (".wav", "wav"),
    "alac": (".m4a", "ipod"),
    # Audio — lossy formats Apple players don't handle in MP4
    "opus": (".opus", None),
    "vorbis": (".ogg", None),
    "mp2": (".mp2", "mp2"),
    "wmav1": (".wma", "asf"),
    "wmav2": (".wma", "asf"),
    "wmapro": (".wma", "asf"),
    # Subtitles — text formats with complex styling
    "ass": (".ass", "ass"),
    "ssa": (".ass", "ass"),
    # Subtitles — image-based formats
    "hdmv_pgs_subtitle": (".sup", "sup"),
    "pgssub": (".sup", "sup"),
    "dvd_subtitle": (".sub", None),
    "vobsub": (".sub", None),
    "dvbsub": (".sup", None),
}


def _ext_format(codec: str) -> tuple[str, str | None]:
    """Return (extension, format_override) for extracting *codec*."""
    return EXTRACT_FORMATS.get(codec, (f".{codec}", None))


# ── helpers ─────────────────────────────────────────────────────────────


def _container_is_mkv(info) -> bool:
    """Check whether the probed file is a Matroska / WebM container."""
    raw = info.format.raw if hasattr(info.format, "raw") else info.format
    fmt = raw.get("format_name", "") if isinstance(raw, dict) else getattr(info.format, "format_name", "")
    if not fmt:
        fmt = info.format.raw.get("format_name", "")
    formats = {f.strip() for f in fmt.split(",")}
    return "matroska" in formats or "webm" in formats


def _stream_lang(stream) -> str:
    lang = stream.tags.get("language")
    return str(lang).strip()[:3] if lang else ""


def _safe_sidecar_part(value: str) -> str:
    safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in value.strip())
    safe = "-".join(part for part in safe.split("-") if part)
    return safe or "und"


def _stream_label(stream) -> str:
    """Human-readable label for a stream:  "#0:0  video  (h264)"."""
    codec = stream.codec_name
    stype = stream.codec_type
    parts = [f"#{stream.index}", stype, f"({codec})"]
    lang = _stream_lang(stream)
    if lang:
        parts.append(f"[{lang}]")
    return "  ".join(parts)


def _sidecar_name(stem: str, stream, ext: str) -> str:
    """Build a sidecar filename:  Movie.s2.eng.dts"""
    lang = _safe_sidecar_part(_stream_lang(stream))
    return f"{stem}.s{stream.index}.{lang}{ext}"


# A report entry is (label, stream, ext_info): ext_info is (ext, fmt) for
# "extract" entries and None for "mp4"/"block" entries.
ExtInfo = tuple[str, str | None] | None
ReportEntry = tuple[str, ProbeStream, ExtInfo]
Report = dict[str, list[ReportEntry]]


# ── plan ─────────────────────────────────────────────────────────────────
# "mp4"     — goes into the MP4 (copied or converted to mov_text)
# "extract" — saved to a sidecar file
# "block"   — prevents the remux entirely


def plan(path: Path) -> tuple[Report, list[str]]:
    """Probe *path* and return (report, issues).

    *report* maps disposition to list of (label, stream, ext_info) tuples:

        "mp4":     [(label, stream, None), ...]
        "extract": [(label, stream, (ext, fmt)), ...]
        "block":   [(label, stream, None), ...]

    *issues* is a human-readable list of problems.  An empty list means
    the remux can proceed (possibly with sidecar extractions).
    """
    info = ffprobe(path)

    if not _container_is_mkv(info):
        return {}, ["not an MKV / WebM container"]

    report: Report = {"mp4": [], "extract": [], "block": []}
    issues: list[str] = []

    for stream in info.streams:
        codec = stream.codec_name.lower()
        label = _stream_label(stream)

        if stream.codec_type == "video":
            if is_image_video_stream(stream):
                # Embedded cover art / thumbnail — ignore (MP4 doesn't need it).
                continue
            if codec in MP4_VIDEO_CODECS:
                report["mp4"].append((label, stream, None))
            else:
                report["block"].append((label, stream, None))
                issues.append(f"unsupported video codec: {stream.codec_name}")

        elif stream.codec_type == "audio":
            if codec in MP4_AUDIO_CODECS:
                report["mp4"].append((label, stream, None))
            else:
                ext, fmt = _ext_format(codec)
                report["extract"].append((label, stream, (ext, fmt)))

        elif stream.codec_type == "subtitle":
            if codec in CONVERTIBLE_SUB_CODECS:
                report["mp4"].append((label, stream, None))
            else:
                ext, fmt = _ext_format(codec)
                report["extract"].append((label, stream, (ext, fmt)))

        else:
            # Data, attachment — ignore.
            pass

    if not info.video_streams:
        issues.append("no video stream")

    return report, issues


# ── display ──────────────────────────────────────────────────────────────

R = "\033[0m"
D = "\033[2m"
B = "\033[1m"
BG = "\033[92m"
BY = "\033[93m"
BR = "\033[91m"


def _print_report(report: Report, stem: str, dest: Path | None = None) -> None:
    """Pretty-print the remux plan."""
    mp4_streams = report.get("mp4", [])
    extract_streams = report.get("extract", [])
    block_streams = report.get("block", [])
    prefix = f"{dest}/" if dest else ""

    for label, _stream, _ext_info in mp4_streams:
        print(f"  {BG}→ mp4{R}  {label}")
    for label, stream, ext_info in extract_streams:
        assert ext_info is not None
        ext, _fmt = ext_info
        sidecar = _sidecar_name(stem, stream, ext)
        print(f"  {BY}→ extract{R}  {label}  {D}→{R}  {BY}{prefix}{sidecar}{R}")
    for label, _stream, _ext_info in block_streams:
        print(f"  {BR}✗ block{R}  {label}")

    if not mp4_streams and not extract_streams and not block_streams:
        print(f"  {D}(no streams){R}")

    print()

    if not block_streams:
        parts: list[str] = [f"{len(mp4_streams)} stream(s) → mp4"]
        if extract_streams:
            parts.append(f"{len(extract_streams)} stream(s) → sidecar")
        if not mp4_streams:
            parts.append(f"{D}(no video in mp4){R}")
        print(f"  {BG}eligible{R} — {', '.join(parts)}")


# ── actions ──────────────────────────────────────────────────────────────


def _run_ffmpeg(cmd: list[str], label: str) -> None:
    """Run a single ffmpeg command.  Raise on failure."""
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"{label} failed")


def _do_remux(src: Path, mp4_streams: list[ReportEntry], dest: Path) -> Path:
    """Remux supported streams into an MP4.  Returns the output path."""
    out = dest / f"{src.stem}.mp4"
    if os.path.lexists(out):
        raise FileExistsError(f"output already exists: {out}")

    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(src),
    ]

    for _label, stream, _ext_info in mp4_streams:
        cmd += ["-map", f"0:{stream.index}"]

    cmd += ["-c", "copy"]

    # If any subtitle streams are in the mp4 list, convert them all to mov_text.
    has_subs = any(stream.codec_type == "subtitle" for _label, stream, _ext_info in mp4_streams)
    if has_subs:
        cmd += ["-c:s", "mov_text"]

    cmd += [
        "-movflags",
        "+faststart",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        str(out),
    ]

    _run_ffmpeg(cmd, "remux")

    if not out.is_file() or out.stat().st_size == 0:
        out.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg produced empty output")

    return out


def _do_extractions(src: Path, extract_streams: list[ReportEntry], dest: Path) -> list[Path]:
    """Extract unsupported streams to sidecar files.  Returns output paths."""
    stem = src.stem
    outputs: list[Path] = []

    for _label, stream, ext_info in extract_streams:
        assert ext_info is not None
        ext, fmt_override = ext_info
        out = dest / _sidecar_name(stem, stream, ext)
        if os.path.lexists(out):
            raise FileExistsError(f"sidecar already exists: {out}")

        cmd = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-map",
            f"0:{stream.index}",
            "-c",
            "copy",
        ]
        if fmt_override is not None and fmt_override:
            cmd += ["-f", fmt_override]
        cmd.append(str(out))

        _run_ffmpeg(cmd, f"extract stream {stream.index}")

        if not out.is_file() or out.stat().st_size == 0:
            out.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg produced empty sidecar for stream {stream.index}")

        outputs.append(out)

    return outputs


def _remove_if_same_identity(output: Path, identity: tuple[int, int]) -> None:
    try:
        current = os.stat(output, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == identity:
        try:
            os.unlink(output)
        except FileNotFoundError:
            # Another actor already removed this path, so there is nothing to clean.
            pass


def _copy_exclusive(staged: Path, output: Path) -> tuple[int, int]:
    """Copy a staged file into a newly-created path when hard links are unavailable."""
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    created = os.fstat(fd)
    identity = (created.st_dev, created.st_ino)
    try:
        with staged.open("rb") as source, os.fdopen(fd, "wb", closefd=False) as target:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(fd)
    except BaseException:
        try:
            _remove_if_same_identity(output, identity)
        finally:
            os.close(fd)
        raise
    else:
        try:
            os.close(fd)
        except OSError:
            _remove_if_same_identity(output, identity)
            raise
        return identity


def _publish_one(staged: Path, output: Path) -> tuple[int, int]:
    """Publish one staged file without replacing an existing output."""
    staged_stat = staged.stat()
    identity = (staged_stat.st_dev, staged_stat.st_ino)
    try:
        os.link(staged, output)
    except FileExistsError as e:
        raise FileExistsError(f"output already exists: {output}") from e
    except OSError:
        try:
            return _copy_exclusive(staged, output)
        except FileExistsError as e:
            raise FileExistsError(f"output already exists: {output}") from e
    return identity


def _publish_outputs(staged_outputs: list[tuple[Path, Path]]) -> None:
    """Publish staged outputs without replacing files that appeared meanwhile."""
    published: list[tuple[Path, tuple[int, int]]] = []
    try:
        for staged, output in staged_outputs:
            identity = _publish_one(staged, output)
            published.append((output, identity))
    except OSError as publish_error:
        cleanup_errors: list[OSError] = []
        for output, identity in reversed(published):
            try:
                _remove_if_same_identity(output, identity)
            except OSError as e:
                cleanup_errors.append(e)
        if cleanup_errors:
            details = "; ".join(str(e) for e in cleanup_errors)
            raise RuntimeError(f"output publication failed: {publish_error}; cleanup failed: {details}") from publish_error
        raise


# ── public entry point ──────────────────────────────────────────────────


def run(path: Path, *, apply: bool = False, dest: str | None = None) -> None:
    """Check eligibility and optionally remux *path* from MKV to MP4.

    *apply* controls whether the remux is actually performed.  When False
    (the default) only the eligibility check runs — a dry-run.

    *dest* is the output directory (default: same directory as the source).
    """
    path = path.resolve()
    if not path.is_file():
        sys.exit(f"Error: not a file: {path}")

    dest_dir = Path(dest).resolve() if dest is not None and dest else path.parent
    dest_dir.mkdir(parents=True, exist_ok=True)

    report, issues = plan(path)

    if not report and issues:
        print(f"{path.name}: {issues[0]}")
        sys.exit(1)

    print(f"\n{B}{path.name}{R}")
    stem = path.stem
    show_dest = None if dest_dir == path.parent else dest_dir
    _print_report(report, stem, show_dest)

    if issues:
        sys.exit(1)

    if not apply:
        print(f"\n{D}Dry run — pass --apply to remux{R}")
        return

    mp4_streams = report.get("mp4", [])
    extract_streams = report.get("extract", [])

    mp4_name = f"{stem}.mp4"
    print(f"\nRemuxing to {dest_dir / mp4_name} …")

    try:
        out = dest_dir / mp4_name
        sidecar_paths: list[Path] = []
        for _label, stream, ext_info in extract_streams:
            assert ext_info is not None
            sidecar_paths.append(dest_dir / _sidecar_name(stem, stream, ext_info[0]))
        output_paths = [out, *sidecar_paths]
        for output in output_paths:
            if os.path.lexists(output):
                label = "sidecar" if output in sidecar_paths else "output"
                raise FileExistsError(f"{label} already exists: {output}")

        with tempfile.TemporaryDirectory(prefix=".mkvtomp4-", dir=dest_dir) as stage_name:
            stage_dir = Path(stage_name)
            staged_remux = _do_remux(path, mp4_streams, stage_dir)
            staged_sidecars = _do_extractions(path, extract_streams, stage_dir) if extract_streams else []
            staged_outputs = [(staged_remux, out), *zip(staged_sidecars, sidecar_paths, strict=True)]
            _publish_outputs(staged_outputs)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as e:
        sys.exit(f"Error: {e}")

    in_size = path.stat().st_size
    out_size = out.stat().st_size
    delta = out_size - in_size
    sign = "+" if delta >= 0 else ""
    rel = out.name if dest_dir == path.parent else str(out)
    print(f"  done  {D}{path.name}{R}  →  {B}{rel}{R}  ({_fmt_size(out_size)}, {sign}{_fmt_size(delta)})")

    if extract_streams:
        print(f"\nExtracted {len(extract_streams)} sidecar stream(s):")
        for sidecar in sidecar_paths:
            sz = _fmt_size(sidecar.stat().st_size)
            rel = sidecar.name if dest_dir == path.parent else str(sidecar)
            print(f"  {D}→{R}  {BY}{rel}{R}  ({sz})")

    print()


def _fmt_size(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1e9:.2f} GB"
    if n >= 1_000_000:
        return f"{n / 1e6:.1f} MB"
    return f"{n / 1000:.0f} KB"
