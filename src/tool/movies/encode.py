#!/usr/bin/env python3
"""
High-quality movie encoder: Apple VideoToolbox HEVC + AudioToolbox AAC.

Usage: movies-cli encode [options] <preset> <file-or-dir> [file-or-dir ...]
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tool.movies import probe
from tool.movies.crop import CropRect, detect_crop

# VideoToolbox -q:v maps directly to kVTCompressionPropertyKey_Quality (0.0–1.0).
# ffmpeg divides by 100, so -q:v 85 → quality 0.85.  Higher = better quality.

# BPP settings for H.265 VideoToolbox output. HQ stays quality-driven VBR with
# a cap; UHQ uses fixed bitrate because VT ignores normal average bitrate mode.
HQ_BPP_CEILING = 0.075
WEB_BPP_TARGET = 0.013
UHQ_BPP_TARGET = 0.070

# Sources scoring at or above this threshold are already well-encoded;
# re-encoding risks generation loss and is skipped unless forced.
QUALITY_SKIP_THRESHOLD = 72


PRESETS = {
    "hq": {
        "desc": "H.265/VT q85 VBR cap 0.075 BPP 10-bit  AAC-LC/AT 256k(stereo)/scaled(surround)  MP4  hvc1/faststart",
        "encoder": "vt",
        "q": 85,
        "bpp_ceiling": HQ_BPP_CEILING,
        "scale": None,
    },
    "hq-720": {
        "desc": "H.265/VT q85 VBR cap 0.075 BPP 10-bit capped to 720p  AAC-LC/AT 256k(stereo)/scaled(surround)  MP4  hvc1/faststart",
        "encoder": "vt",
        "q": 85,
        "bpp_ceiling": HQ_BPP_CEILING,
        "scale": 720,
    },
    "sq-720": {
        "desc": "H.265/VT q60 CQ 10-bit capped to 720p  AAC-LC/AT 256k(stereo)/scaled(surround)  MP4  hvc1/faststart",
        "encoder": "vt",
        "q": 60,
        "scale": 720,
    },
    "web": {
        "desc": "H.265/VT fixed 0.013 BPP 10-bit size-preserving web re-encode  AAC-LC/AT source-capped  MP4  hvc1/faststart",
        "encoder": "vt",
        "q": None,
        "bpp_target": WEB_BPP_TARGET,
        "constant_bit_rate": True,
        "scale": None,
    },
    "uhq": {
        "desc": "H.265/VT fixed 0.070 BPP 10-bit  AAC-LC/AT 256k(stereo)/scaled(surround)  MP4  hvc1/faststart",
        "encoder": "vt",
        "q": None,
        "bpp_target": UHQ_BPP_TARGET,
        "constant_bit_rate": True,
        "scale": None,
    },
    "x265-hq": {
        "desc": "libx265 CRF 22 slow 10-bit  AAC-LC/AT 256k(stereo)/scaled(surround)  MP4  hvc1/faststart",
        "encoder": "x265",
        "crf": 22,
        "scale": None,
    },
}


@dataclass
class EncodeTask:
    src: Path
    dst: Path
    info: probe.ProbeInfo
    duration: float
    audio_streams: list[probe.ProbeStream]
    subtitle_streams: list[probe.ProbeStream]
    crop: CropRect | None = None


@dataclass(frozen=True)
class FileQuality:
    score: int
    size_bytes: int
    video_bpp: float
    flags: set[str]
    stream_counts: dict[str, int]
    subtitle_codecs: list[str]


LOSSLESS_MOV_TEXT_SUB_CODECS = {"subrip", "srt", "webvtt", "mov_text", "text", "ttml"}
SIDECAR_SUB_CODECS = {"dvd_subtitle", "vobsub"}
SELECTABLE_SUB_CODECS = LOSSLESS_MOV_TEXT_SUB_CODECS | SIDECAR_SUB_CODECS


@dataclass
class ProgressStatus:
    name: str
    pct: float = 0.0
    fps: float | None = None
    speed: str = ""


@dataclass(frozen=True)
class FfmpegProgress:
    frame: int | None
    fps: float | None
    total_size: int | None
    out_time_us: int | None
    speed: str
    progress: str
    raw: dict[str, str]

    @classmethod
    def from_fields(cls, fields: dict[str, str]) -> FfmpegProgress:
        return cls(
            frame=_optional_int(fields.get("frame")),
            fps=_optional_float(fields.get("fps")),
            total_size=_optional_int(fields.get("total_size")),
            out_time_us=_optional_int(fields.get("out_time_us")),
            speed=fields.get("speed", ""),
            progress=fields.get("progress", ""),
            raw=dict(fields),
        )

    @property
    def out_time_seconds(self) -> float | None:
        return self.out_time_us / 1_000_000 if self.out_time_us is not None else None


def _optional_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None and value != "N/A" else None
    except ValueError:
        return None


def _optional_float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None and value != "N/A" else None
    except ValueError:
        return None


def iter_ffmpeg_progress(lines: Iterable[str]) -> Iterable[FfmpegProgress]:
    fields: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key] = value
        if key == "progress":
            yield FfmpegProgress.from_fields(fields)
            fields = {}
    if fields:
        yield FfmpegProgress.from_fields(fields)


class ProgressDisplay:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.statuses: dict[str, ProgressStatus] = {}
        self.lines_rendered = 0
        self.last_render = 0.0
        self.lock = threading.Lock()

    def start(self, name: str) -> None:
        with self.lock:
            self.statuses[name] = ProgressStatus(name=name)
            if self.enabled:
                self._render_locked(force=True)
            else:
                print(f"Start:  {name}")

    def update(self, name: str, pct: float, fps: float | None, speed: str) -> None:
        with self.lock:
            status = self.statuses.get(name)
            if not status:
                return
            status.pct = max(status.pct, pct)
            status.fps = fps
            status.speed = speed
            if self.enabled:
                self._render_locked()

    def finish(self, name: str, message: str) -> None:
        with self.lock:
            self.statuses.pop(name, None)
            if self.enabled:
                self._clear_locked()
                print(message)
                self._render_locked(force=True)
            else:
                print(message)

    def fail(self, name: str, message: str) -> None:
        with self.lock:
            self.statuses.pop(name, None)
            if self.enabled:
                self._clear_locked()
                sys.stdout.flush()
                print(message, file=sys.stderr)
                self._render_locked(force=True)
            else:
                sys.stdout.flush()
                print(message, file=sys.stderr)

    def close(self) -> None:
        with self.lock:
            if self.enabled:
                self._clear_locked()

    def _clear_locked(self) -> None:
        if not self.lines_rendered:
            return
        sys.stdout.write(f"\033[{self.lines_rendered}F\033[J")
        sys.stdout.flush()
        self.lines_rendered = 0

    def _render_locked(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last_render < 0.5:
            return
        self._clear_locked()
        if not self.statuses:
            self.last_render = now
            return
        columns = max(40, shutil.get_terminal_size((100, 20)).columns)
        lines = [_progress_header(columns)]
        for status in sorted(self.statuses.values(), key=lambda s: s.name.lower()):
            pct = min(status.pct, 1.0)
            lines.append(_progress_line(status, pct, columns))
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()
        self.lines_rendered = len(lines)
        self.last_render = now


def _is_hdr(video_stream: probe.ProbeStream) -> bool:
    return video_stream.color_transfer.lower() in probe.HDR_TRANSFERS


def _default_sdr_color(video_stream: probe.ProbeStream, field: str) -> str:
    unknown = {"", "unknown", "unspecified"}
    raw = video_stream.get(field)
    value = str(raw).lower() if raw else ""
    if value not in unknown:
        return value
    if video_stream.width and video_stream.width <= 720:
        return "smpte170m"
    return "bt709"


def color_args(video_stream: probe.ProbeStream, hdr_policy: str) -> list[str]:
    """Return ffmpeg color metadata flags for the output stream.

    Handles four fields: colorspace (matrix), primaries, transfer, and range.
    HDR sources are rejected by default; explicit retag/tonemap policies output bt709/tv.
    For SDR sources, preserves source tags (BT.601 for SD, BT.709 for HD).
    Falls back to bt709/tv for unknown/unspecified values.
    """
    trc = video_stream.color_transfer.lower()
    range_tag = video_stream.color_range.lower()

    if trc in probe.HDR_TRANSFERS:
        if hdr_policy == "abort":
            raise ValueError("HDR source requires --hdr-policy retag or --hdr-policy tonemap")
        return [
            "-colorspace",
            "bt709",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-color_range",
            "tv",
        ]

    out_cs = _default_sdr_color(video_stream, "color_space")
    out_pri = _default_sdr_color(video_stream, "color_primaries")
    out_trc = _default_sdr_color(video_stream, "color_transfer")
    out_range = range_tag if range_tag in ("tv", "pc") else "tv"
    return [
        "-colorspace",
        out_cs,
        "-color_primaries",
        out_pri,
        "-color_trc",
        out_trc,
        "-color_range",
        out_range,
    ]


def _pixel_format_chroma(pix_fmt: str) -> str:
    """Return the source pixel format's chroma layout.

    VideoToolbox's default ``p010le`` is 4:2:0.  Selecting it unconditionally
    silently throws away chroma samples from 4:2:2 (and 4:4:4) sources, so the
    encode path needs to inspect the source layout before choosing an output
    format.  ``unknown`` is kept distinct from 4:2:0 so an unfamiliar format
    cannot be reduced by accident.
    """
    normalized = pix_fmt.lower()
    if any(token in normalized for token in ("yuv444", "yuvj444", "yuva444", "ayuv", "nv24", "p410")):
        return "444"
    if any(token in normalized for token in ("yuv422", "yuvj422", "yuva422", "p210", "nv16", "uyvy422", "v210")):
        return "422"
    if any(token in normalized for token in ("yuv420", "yuvj420", "yuva420", "p010", "nv12")):
        return "420"
    return "unknown"


def _pixel_format_bit_depth(pix_fmt: str) -> int:
    """Return the source pixel format bit depth, defaulting to 8-bit."""
    normalized = pix_fmt.lower()
    for depth in (16, 14, 12, 11, 10, 9):
        if str(depth) in normalized:
            return depth
    return 8


def _videotoolbox_video_format(video_stream: probe.ProbeStream | None) -> tuple[str, str]:
    """Choose a VT pixel format/profile without reducing source chroma.

    VideoToolbox exposes a 10-bit 4:2:2 path as ``p210le``/``main42210``.
    It does not expose a 4:4:4 HEVC profile through this encoder, so refusing
    that input is safer than silently converting it to 4:2:0.
    """
    pix_fmt = video_stream.pix_fmt if video_stream else ""
    chroma = _pixel_format_chroma(pix_fmt)
    bit_depth = _pixel_format_bit_depth(pix_fmt)
    if bit_depth > 10:
        raise ValueError(
            f"VideoToolbox cannot preserve {bit_depth}-bit source pixel format {pix_fmt!r}; "
            "use a format-preserving software encoder for this source"
        )
    if chroma == "422":
        return "p210le", "main42210"
    if chroma == "420" or not pix_fmt:
        return "p010le", "main10"
    if chroma == "444":
        raise ValueError(
            f"VideoToolbox cannot preserve 4:4:4 source pixel format {pix_fmt!r}; "
            "use a format-preserving software encoder for this source"
        )
    raise ValueError(
        f"VideoToolbox cannot preserve source pixel format {pix_fmt!r}; "
        "use a format-preserving software encoder for this source"
    )


def _hdr_tonemap_filter(pixel_format: str = "p010le") -> str:
    return (
        "zscale=t=linear:npl=100,format=gbrpf32le,"
        "tonemap=tonemap=hable:desat=0,"
        f"zscale=t=bt709:p=bt709:m=bt709:r=tv,format={pixel_format}"
    )


def _audio_ordinal(info: probe.ProbeInfo, audio_stream: probe.ProbeStream) -> int:
    for ordinal, stream in enumerate(info.audio_streams):
        if stream.index == audio_stream.index:
            return ordinal
    return 0


def _default_subtitle_streams(info: probe.ProbeInfo) -> list[probe.ProbeStream]:
    return [
        stream
        for stream in info.subtitle_streams
        if stream.codec_name.lower() in probe.TEXT_SUB_CODECS
        and _stream_language(stream).lower() in {"en", "eng", "english"}
    ]


def _mp4_subtitle_streams(subtitle_streams: list[probe.ProbeStream]) -> list[probe.ProbeStream]:
    return [stream for stream in subtitle_streams if stream.codec_name.lower() in LOSSLESS_MOV_TEXT_SUB_CODECS]


def _sidecar_subtitle_streams(subtitle_streams: list[probe.ProbeStream]) -> list[probe.ProbeStream]:
    return [stream for stream in subtitle_streams if stream.codec_name.lower() in SIDECAR_SUB_CODECS]


def _mp4_sub_args(subtitle_streams: list[probe.ProbeStream]) -> list[str]:
    subtitle_streams = _mp4_subtitle_streams(subtitle_streams)
    if not subtitle_streams:
        return []
    args: list[str] = []
    for stream in subtitle_streams:
        args += ["-map", f"0:{stream.index}"]
    args += ["-c:s", "mov_text"]
    return args


def _safe_sidecar_part(value: str) -> str:
    safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in value.strip())
    safe = "-".join(part for part in safe.split("-") if part)
    return safe or "und"


def subtitle_sidecar_path(dst: Path, stream: probe.ProbeStream) -> Path:
    lang = _safe_sidecar_part(_stream_language(stream))
    return dst.with_name(f"{dst.stem}.s{stream.index}.{lang}.mks")


def extract_subtitle_sidecars(
    src: Path, dst: Path, subtitle_streams: list[probe.ProbeStream]
) -> tuple[bool, str, list[Path]]:
    outputs: list[Path] = []
    for stream in _sidecar_subtitle_streams(subtitle_streams):
        out = subtitle_sidecar_path(dst, stream)
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-map",
            f"0:{stream.index}",
            "-c:s",
            "copy",
            "-f",
            "matroska",
            str(out),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, str(e), outputs
        if result.returncode != 0:
            out.unlink(missing_ok=True)
            return False, result.stderr, outputs
        outputs.append(out)
    return True, "", outputs


def build_cmd(
    src: Path,
    dst: Path,
    preset: dict[str, Any],
    info: probe.ProbeInfo,
    hdr_policy: str,
    audio_streams: list[probe.ProbeStream] | None = None,
    subtitle_streams: list[probe.ProbeStream] | None = None,
    crop: CropRect | None = None,
) -> list[str]:
    audio_streams = audio_streams if audio_streams is not None else info.audio_streams[:1]
    subtitle_streams = subtitle_streams if subtitle_streams is not None else _default_subtitle_streams(info)

    video = info.first_video
    col_args = (
        color_args(video, hdr_policy)
        if video
        else [
            "-colorspace",
            "bt709",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-color_range",
            "tv",
        ]
    )

    # avg_frame_rate reflects actual presentation cadence; r_frame_rate is the
    # codec-declared timebase and can be double for interlaced or VFR sources,
    # which would inflate the BPP ceiling.
    fps = (probe.parse_fps(video.avg_frame_rate) or probe.parse_fps(video.r_frame_rate)) if video else 0.0

    encoder = preset.get("encoder", "vt")
    vt_pixel_format, vt_profile = _videotoolbox_video_format(video) if encoder == "vt" else ("", "")

    field_order = video.field_order.lower() if video else ""
    is_interlaced = field_order not in ("", "progressive", "unknown")

    scale = preset.get("scale")
    vf_filters: list[str] = []
    if crop:
        vf_filters.append(crop.ffmpeg_filter)
    if video and _is_hdr(video) and hdr_policy == "tonemap":
        vf_filters.append(_hdr_tonemap_filter(vt_pixel_format or "p010le"))
    if is_interlaced:
        # One progressive frame per field; 29.97i becomes 59.94p.
        vf_filters.append("bwdif=mode=send_field")
    if scale:
        vf_filters.append(f"scale='if(gt(iw,ih),-2,min(iw,{scale}))':'if(gt(iw,ih),min(ih,{scale}),-2)'")

    # ~10-second max GOP: bounds seek distance anywhere in the film without
    # meaningfully hurting compression on typical content.
    gop_frames = max(int((fps * 2 if is_interlaced else fps) * 10), 1) if fps else 240

    if encoder == "vt":
        w = crop.width if crop else (video.width if video else 0)
        h = crop.height if crop else (video.height if video else 0)
        quality_args: list[str] = []
        bitrate_args: list[str] = []
        maxrate_args: list[str] = []
        if preset.get("q") is not None:
            quality_args = ["-q:v", str(preset["q"])]
        if w and h and fps:
            # bwdif send_field doubles output fps, so double the ceiling too.
            output_fps = fps * 2 if is_interlaced else fps
            bpp_target = preset.get("bpp_target")
            if bpp_target:
                bitrate_args = ["-b:v", str(int(bpp_target * w * h * output_fps))]
                if preset.get("constant_bit_rate"):
                    bitrate_args += ["-constant_bit_rate", "true"]
            bpp_ceiling = preset.get("bpp_ceiling")
            if bpp_ceiling:
                cap = int(bpp_ceiling * w * h * output_fps)
                # 4× bufsize gives VT room to spend bits on complex scenes after quiet ones.
                maxrate_args = ["-maxrate", str(cap), "-bufsize", str(cap * 4)]

        video_args = [
            "-c:v",
            "hevc_videotoolbox",
            "-tag:v",
            "hvc1",  # AVFoundation/QuickTime require hvc1 for hardware decode
            *quality_args,
            *bitrate_args,
            "-profile:v",
            vt_profile,
            "-pix_fmt",
            vt_pixel_format,  # 10-bit output while preserving 4:2:0/4:2:2 chroma
            *col_args,
            "-spatial_aq",
            "1",  # adaptive quantisation: more bits to complex regions
            "-fps_mode",
            "passthrough",
            "-prio_speed",
            "false",
            "-g",
            str(gop_frames),
            "-force_key_frames",
            "chapters",
            *maxrate_args,
        ]
    else:  # x265
        # aq-mode=3: HEVC auto-variance AQ, better than mode 2 for film grain.
        # psy-rd/psy-rdoq preserve texture that rate-distortion would otherwise discard.
        # no-open-gop: disables cross-GOP references for cleaner seeking/cutting.
        # keyint: matches VT's max GOP for consistent seek behaviour across presets.
        x265_params = f"aq-mode=3:aq-strength=0.8:psy-rd=2.0:psy-rdoq=1.0:no-open-gop=1:keyint={gop_frames}"
        video_args = [
            "-c:v",
            "libx265",
            "-tag:v",
            "hvc1",  # AVFoundation/QuickTime require hvc1 for hardware decode
            "-crf",
            str(preset["crf"]),
            "-preset",
            "slow",
            "-profile:v",
            "main10",
            "-pix_fmt",
            "yuv420p10le",
            *col_args,
            "-x265-params",
            x265_params,
            "-fps_mode",
            "passthrough",
            "-force_key_frames",
            "chapters",
        ]

    map_args = ["-map", "0:V:0"]
    audio_args: list[str] = []
    for out_idx, audio_stream in enumerate(audio_streams):
        map_args += ["-map", f"0:{audio_stream.index}"]
        source_kbps = round(audio_stream.bit_rate / 1000) if audio_stream.bit_rate else None
        if source_kbps is None or not source_kbps:
            est = probe.probe_audio_br(src, _audio_ordinal(info, audio_stream))
            source_kbps = round(est / 1000) if est is not None and est else None
        audio_br = probe.target_audio_bitrate(audio_stream.channels or 2, source_kbps or None)
        audio_args += [f"-c:a:{out_idx}", "aac_at", f"-b:a:{out_idx}", audio_br]

    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-stats_period",
        "1",
        "-progress",
        "pipe:1",
        "-i",
        str(src),
        *map_args,
        # Text subs converted to mov_text (the only MP4-compatible subtitle format).
        # Selected VobSub/DVD subs are extracted as subtitle-only Matroska sidecars.
        # Other image-based subs are dropped — they cannot be stored in MP4.
        # Font attachments are also dropped; MP4 has no attachment stream type.
        *_mp4_sub_args(subtitle_streams),
        *video_args,
        *(["-vf", ",".join(vf_filters)] if vf_filters else []),
        *audio_args,
        # faststart rewrites the file to place the moov atom at the front,
        # enabling AirPlay and HTTP streaming to begin playback immediately.
        "-movflags",
        "+faststart",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        str(dst),
    ]


def fmt_size(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1e9:.2f} GB"
    if n >= 1_000_000:
        return f"{n / 1e6:.1f} MB"
    return f"{n / 1000:.0f} KB"


def _csv_range(values: list[int]) -> str:
    return ",".join(str(value) for value in values)


def _stream_language(stream: probe.ProbeStream) -> str:
    return str(stream.tags.get("language") or "und")


def _stream_title(stream: probe.ProbeStream) -> str:
    title = str(stream.tags.get("title") or "").strip()
    return f" ({title})" if title else ""


def _describe_audio_choice(num: int, stream: probe.ProbeStream) -> str:
    ch_label = probe.CHANNEL_NAMES.get(stream.channels, f"{stream.channels}ch") if stream.channels else "?"
    br = f", {round(stream.bit_rate / 1000)} kbps" if stream.bit_rate else ""
    return (
        f"  {num}. stream {stream.index}: {_stream_language(stream)} "
        f"{stream.codec_name} {ch_label}{br}{_stream_title(stream)}"
    )


def _describe_subtitle_choice(num: int, stream: probe.ProbeStream) -> str:
    codec = stream.codec_name.lower()
    note = " -> sidecar .mks" if codec in SIDECAR_SUB_CODECS else ""
    return f"  {num}. {_stream_language(stream)} {codec}{note}"


def _parse_csv_numbers(value: str, max_num: int, *, allow_none: bool) -> list[int] | None:
    raw = value.strip().lower()
    if allow_none and raw in {"0", "none", "no"}:
        return []
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if not parts:
        return None
    nums: list[int] = []
    for part in parts:
        if not part.isdigit():
            return None
        num = int(part)
        if num < 1 or num > max_num or num in nums:
            return None
        nums.append(num)
    return nums


def _choose_streams(
    label: str,
    streams: list[probe.ProbeStream],
    default_nums: list[int],
    describe,
    *,
    allow_none: bool = False,
    compatible_codecs: set[str] | None = None,
) -> list[probe.ProbeStream]:
    if not streams:
        return []

    print(f"{label} tracks:")
    for num, stream in enumerate(streams, 1):
        print(describe(num, stream))

    prompt = f"Keep {label.lower()} tracks [{_csv_range(default_nums)}]"
    if allow_none:
        prompt += " (0 for none)"
    prompt += ": "

    while True:
        try:
            answer = input(prompt)
        except EOFError:
            answer = ""
        nums = default_nums if not answer.strip() else _parse_csv_numbers(answer, len(streams), allow_none=allow_none)
        if nums is None:
            print(f"Enter comma-separated numbers from 1 to {len(streams)}.")
            continue
        selected = [streams[num - 1] for num in nums]
        if compatible_codecs is not None and compatible_codecs:
            incompatible = [s.codec_name.lower() for s in selected if s.codec_name.lower() not in compatible_codecs]
            if incompatible:
                print(
                    f"Cannot keep these subtitle codecs in MP4 or supported sidecars: {', '.join(sorted(set(incompatible)))}."
                )
                continue
        return selected


def select_tracks(
    info: probe.ProbeInfo, *, interactive: bool | None = None
) -> tuple[list[probe.ProbeStream], list[probe.ProbeStream]]:
    if interactive is None:
        interactive = bool(sys.stdin.isatty())
    default_audio = info.audio_streams[:1]
    default_subs = _default_subtitle_streams(info)
    if not interactive:
        return default_audio, default_subs

    if len(info.audio_streams) > 1:
        audio = _choose_streams(
            "Audio",
            info.audio_streams,
            [1],
            _describe_audio_choice,
        )
    else:
        audio = default_audio
    compatible_sub_nums = [
        num for num, stream in enumerate(info.subtitle_streams, 1) if stream.codec_name.lower() in SELECTABLE_SUB_CODECS
    ]
    subtitles = _choose_streams(
        "Subtitle",
        info.subtitle_streams,
        compatible_sub_nums,
        _describe_subtitle_choice,
        allow_none=True,
        compatible_codecs=SELECTABLE_SUB_CODECS,
    )
    return audio, subtitles


def _truncate_middle(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    head = (max_len - 3) // 2
    tail = max_len - 3 - head
    return f"{text[:head]}...{text[-tail:]}"


def _fit_terminal_line(line: str, columns: int) -> str:
    limit = max(columns - 1, 1)
    return line if len(line) <= limit else line[:limit]


def _progress_line(status: ProgressStatus, pct: float, columns: int) -> str:
    for bar_w in (24, 18, 12, 8):
        filled = int(bar_w * pct)
        bar = "█" * filled + "░" * (bar_w - filled)
        speed = status.speed or "-"
        fps = f"{status.fps:.1f}" if status.fps is not None else "-"
        prefix = f"  [{bar}] {pct * 100:5.1f}% {fps:>7} {speed:>7}  "
        name_len = columns - len(prefix) - 1
        if name_len >= 12 or bar_w == 8:
            return _fit_terminal_line(prefix + _truncate_middle(status.name, max(name_len, 1)), columns)
    raise AssertionError("unreachable")


def _progress_header(columns: int) -> str:
    prefix = "  Progress"
    suffix = "     FPS   Speed  File"
    return _fit_terminal_line(f"{prefix:<35}{suffix}", columns)


def locked_print(message: str, lock: threading.Lock | None = None) -> None:
    if lock:
        with lock:
            print(message)
    else:
        print(message)


def _applescript_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def notify_user(title: str, message: str) -> None:
    if sys.platform != "darwin" or not sys.stdout.isatty() or shutil.which("osascript") is None:
        return
    script = f'display notification "{_applescript_string(message)}" with title "{_applescript_string(title)}"'
    try:
        subprocess.run(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        pass


def run_encode(
    cmd: list[str],
    duration: float,
    label: str | None = None,
    progress_display: ProgressDisplay | None = None,
) -> tuple[bool, str]:
    """Run ffmpeg, streaming progress on stdout, errors on stderr."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stderr is not None
    stdout = proc.stdout
    stderr = proc.stderr

    stderr_buf: list[str] = []
    stderr_thread = threading.Thread(target=lambda: stderr_buf.append(stderr.read()), daemon=True)
    stderr_thread.start()

    bar_w = 34
    next_milestone = 10
    for progress in iter_ffmpeg_progress(stdout):
        secs = progress.out_time_seconds
        if secs is None or duration <= 0:
            continue
        pct = min(secs / duration, 1.0)
        if label is not None and label and progress_display:
            progress_display.update(label, pct, progress.fps, progress.speed)
            continue
        if label is not None and label:
            milestone = int(pct * 100)
            if milestone >= next_milestone:
                locked_print(f"  {label}: {min(milestone, 100)}%")
                next_milestone += 10
            continue
        filled = int(bar_w * pct)
        bar = "█" * filled + "░" * (bar_w - filled)
        print(f"\r  [{bar}] {pct * 100:5.1f}%", end="", flush=True)

    if label is None or not label:
        print()
    returncode = proc.wait()
    stderr_thread.join(timeout=1)
    assert isinstance(returncode, int)
    return returncode == 0, stderr_buf[0] if stderr_buf else ""


def _tail_lines(text: str, max_lines: int = 12) -> list[str]:
    return text.strip().splitlines()[-max_lines:]


def report_failure(
    src: Path,
    phase: str,
    reason: str,
    *,
    stderr: str = "",
    progress_display: ProgressDisplay | None = None,
    label: str | None = None,
) -> None:
    lines = [
        f"Failed: {src.name}",
        f"  Phase: {phase}",
        f"  Reason: {reason}",
    ]
    if stderr.strip():
        lines.append("  ffmpeg stderr:")
        lines.extend(f"    {line}" for line in _tail_lines(stderr))

    message = "\n".join(lines)
    sys.stdout.flush()
    if label is not None and label and progress_display:
        progress_display.fail(label, message)
    else:
        print(message, file=sys.stderr)


def _check_source_quality(src: Path, info: probe.ProbeInfo, assume_yes: bool) -> bool:
    """Return whether this source should be encoded."""
    analysis = probe.analyze_info(src, info)
    if "error" in analysis:
        return True

    v_info = analysis["video"]
    v_flags = analysis["video_flags"]
    if "not_h265" in v_flags:
        return True

    score, reasons = probe.quality_score(v_info, v_flags)
    if score < QUALITY_SKIP_THRESHOLD:
        return True

    level = "already excellent" if score >= 85 else "already a decent encode"
    if assume_yes:
        print(f"  Quality score: {score}/100 — {', '.join(reasons)}")
        print(f"  Source looks {level}; forcing encode because --yes was provided.")
        return True

    print(f"Skipping quality source: {src.name}")
    print(f"  Quality score: {score}/100 — {', '.join(reasons)}")
    print(f"  Source looks {level}. Use --yes to force a re-encode.")
    # notify_user("Encode skipped", f"{src.name} already scores {score}/100.")
    return False


def _flag_keys(flags: list[str]) -> set[str]:
    return {flag.split(":")[0] for flag in flags}


def _stream_counts(info: probe.ProbeInfo) -> dict[str, int]:
    counts: dict[str, int] = {}
    for stream in info.streams:
        if stream.codec_type == "video" and probe.is_image_video_stream(stream):
            continue
        counts[stream.codec_type] = counts.get(stream.codec_type, 0) + 1
    return counts


def _file_quality(path: Path) -> tuple[FileQuality | None, str]:
    try:
        info = probe.with_estimated_bitrates(path, probe.ffprobe(path))
    except (RuntimeError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        return None, str(e)

    analysis = probe.analyze_info(path, info)
    if "error" in analysis:
        return None, str(analysis["error"])

    video_flags = analysis["video_flags"]
    audio_flags = analysis["audio_flags"]
    score, _ = probe.quality_score(analysis["video"], video_flags)
    return (
        FileQuality(
            score=score,
            size_bytes=path.stat().st_size,
            video_bpp=analysis["video"].get("bpp", 0.0),
            flags=_flag_keys(video_flags + audio_flags),
            stream_counts=_stream_counts(info),
            subtitle_codecs=[stream.codec_name.lower() for stream in info.subtitle_streams],
        ),
        "",
    )


def _expected_mp4_subtitle_codecs(src_codecs: list[str]) -> list[str]:
    expected: list[str] = []
    for codec in src_codecs:
        if codec in LOSSLESS_MOV_TEXT_SUB_CODECS:
            expected.append("mov_text")
        else:
            expected.append(codec)
    return expected


def _strictly_better_output(
    src: Path,
    dst: Path,
    selected_audio_count: int | None = None,
    selected_subtitle_codecs: list[str] | None = None,
) -> tuple[bool, str]:
    src_quality, src_error = _file_quality(src)
    if src_quality is None:
        return False, f"could not probe source quality: {src_error}"

    dst_quality, dst_error = _file_quality(dst)
    if dst_quality is None:
        return False, f"could not probe encoded quality: {dst_error}"

    src_counts = dict(src_quality.stream_counts)
    if selected_audio_count is not None:
        src_counts["audio"] = selected_audio_count
    if selected_subtitle_codecs is not None:
        src_counts["subtitle"] = len(selected_subtitle_codecs)
    src_subtitle_codecs = (
        selected_subtitle_codecs if selected_subtitle_codecs is not None else src_quality.subtitle_codecs
    )

    for stream_type, src_count in sorted(src_counts.items()):
        dst_count = dst_quality.stream_counts.get(stream_type, 0)
        if dst_count < src_count:
            return False, f"encoded file has fewer {stream_type} streams ({dst_count} < {src_count})"

    expected_subtitles = _expected_mp4_subtitle_codecs(src_subtitle_codecs)
    if dst_quality.subtitle_codecs != expected_subtitles:
        return False, (
            "encoded file changed subtitle codecs "
            f"(expected {', '.join(expected_subtitles) or 'none'}, "
            f"got {', '.join(dst_quality.subtitle_codecs) or 'none'})"
        )

    new_flags = dst_quality.flags - src_quality.flags
    if new_flags:
        return False, f"encoded file introduced quality flags: {', '.join(sorted(new_flags))}"
    if dst_quality.size_bytes > src_quality.size_bytes:
        pct = (dst_quality.size_bytes - src_quality.size_bytes) / src_quality.size_bytes * 100
        return False, f"encoded file is larger than source (+{pct:.1f}%)"
    if src_quality.video_bpp and dst_quality.video_bpp > src_quality.video_bpp:
        pct = (dst_quality.video_bpp - src_quality.video_bpp) / src_quality.video_bpp * 100
        return False, f"encoded video BPP is higher than source (+{pct:.1f}%)"
    if dst_quality.score <= src_quality.score:
        return False, f"encoded quality score {dst_quality.score} is not greater than source score {src_quality.score}"

    return True, f"quality score improved {src_quality.score} -> {dst_quality.score}"


def _delete_preflight_reason(subtitle_streams: list[probe.ProbeStream]) -> str | None:
    unsupported_subs = [
        stream.codec_name.lower()
        for stream in subtitle_streams
        if stream.codec_name.lower() not in SELECTABLE_SUB_CODECS
    ]
    if unsupported_subs:
        codecs = ", ".join(sorted(set(unsupported_subs)))
        return f"source has subtitle codecs that are not preserved in MP4 or supported sidecars: {codecs}"

    return None


def _is_interlaced(video: probe.ProbeStream | None) -> bool:
    field_order = video.field_order.lower() if video else ""
    return field_order not in ("", "progressive", "unknown")


def collect_sources(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    sources: list[Path] = []
    for path in paths:
        if path.is_dir():
            candidates = sorted(
                p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in probe.VIDEO_EXTENSIONS
            )
        elif path.is_file():
            candidates = [path]
        else:
            print(f"Skipping missing path: {path}", file=sys.stderr)
            continue
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                sources.append(candidate)
    return sources


def prepare_task(
    preset_name: str,
    src: Path,
    hdr_policy: str,
    assume_yes: bool,
    no_crop: bool = False,
) -> EncodeTask | None:
    if not src.is_file():
        print(f"Skipping non-file: {src}", file=sys.stderr)
        return None

    if "-encoded-" in src.stem:
        print(f"Skipping already-encoded file: {src.name}")
        return None

    PRESETS[preset_name]
    dst = src.with_name(f"{src.stem}-encoded-{preset_name}.mp4")

    if dst.exists():
        print(f"Output already exists, skipping: {dst.name}")
        return None

    print(f"Source: {src.name}")
    try:
        info = probe.ffprobe(src)
    except RuntimeError as e:
        print(f"Error probing {src.name}: {e}", file=sys.stderr)
        return None
    info = probe.with_estimated_bitrates(src, info)

    duration = info.format.duration
    video = info.first_video
    if video and _is_hdr(video) and hdr_policy == "abort":
        print(f"Skipping HDR source: {src.name}  (use --hdr-policy retag or --hdr-policy tonemap)")
        return None

    if not _check_source_quality(src, info, assume_yes):
        return None

    crop_rect = None
    if video and not no_crop:
        crop_rect = detect_crop(src, duration, video.width, video.height)
        if crop_rect:
            print(f"  Crop: {crop_rect.width}x{crop_rect.height}+{crop_rect.x}+{crop_rect.y}")

    audio_streams, subtitle_streams = select_tracks(info)
    return EncodeTask(
        src=src,
        dst=dst,
        info=info,
        duration=duration,
        audio_streams=audio_streams,
        subtitle_streams=subtitle_streams,
        crop=crop_rect,
    )


def encode_task(
    task: EncodeTask,
    preset: dict[str, Any],
    hdr_policy: str,
    progress_display: ProgressDisplay | None = None,
    label: str | None = None,
    delete_source: bool = False,
    tmp_root: Path | None = None,
) -> bool:
    if delete_source:
        return encode_task_delete(task, preset, hdr_policy, progress_display, label, tmp_root)

    cmd = build_cmd(
        task.src,
        task.dst,
        preset,
        task.info,
        hdr_policy,
        task.audio_streams,
        task.subtitle_streams,
        task.crop,
    )
    if label is not None and label and progress_display:
        progress_display.start(label)
    elif label is not None and label:
        print(f"Start:  {task.src.name}")
    else:
        print(f"Output: {task.dst.name}")

    ok, err = run_encode(cmd, task.duration, label, progress_display)

    if not ok:
        task.dst.unlink(missing_ok=True)
        report_failure(
            task.src,
            "ffmpeg encode",
            "ffmpeg exited with a non-zero status",
            stderr=err,
            progress_display=progress_display,
            label=label,
        )
        return False

    sidecars_ok, sidecar_err, sidecar_paths = extract_subtitle_sidecars(task.src, task.dst, task.subtitle_streams)
    if not sidecars_ok:
        report_failure(
            task.src,
            "subtitle sidecar extraction",
            "could not extract selected DVD/VobSub subtitles",
            stderr=sidecar_err,
            progress_display=progress_display,
            label=label,
        )
        return False

    src_sz = task.src.stat().st_size
    dst_sz = task.dst.stat().st_size
    pct = (dst_sz - src_sz) / src_sz * 100
    done = f"Done:   {task.src.name}  {fmt_size(src_sz)} → {fmt_size(dst_sz)}  ({'+' if pct >= 0 else ''}{pct:.1f}%)"
    if sidecar_paths:
        names = ", ".join(path.name for path in sidecar_paths)
        done += f"  subtitles: {names}"
    if label is not None and label and progress_display:
        progress_display.finish(label, done)
    else:
        print(done)
    return True


def _move_file(src: Path, dst: Path) -> str | None:
    """Move *src* to *dst*. Returns None on success, or an error message."""
    try:
        shutil.move(str(src), str(dst))
        return None
    except (OSError, shutil.Error) as e:
        return str(e)


def encode_task_delete(
    task: EncodeTask,
    preset: dict[str, Any],
    hdr_policy: str,
    progress_display: ProgressDisplay | None = None,
    label: str | None = None,
    tmp_root: Path | None = None,
) -> bool:
    preflight_reason = _delete_preflight_reason(task.subtitle_streams)
    if preflight_reason is not None and preflight_reason:
        report_failure(
            task.src,
            "delete safety preflight",
            f"{preflight_reason}; run without --delete to keep the encoded MP4 alongside the source",
            progress_display=progress_display,
            label=label,
        )
        return False

    tmp_dir = Path(tempfile.mkdtemp(prefix="movies-encode-", dir=tmp_root))
    tmp_dst = tmp_dir / task.dst.name
    cleanup_tmp = True

    try:
        cmd = build_cmd(
            task.src,
            tmp_dst,
            preset,
            task.info,
            hdr_policy,
            task.audio_streams,
            task.subtitle_streams,
            task.crop,
        )
        if label is not None and label and progress_display:
            progress_display.start(label)
        elif label is not None and label:
            print(f"Start:  {task.src.name}")
        else:
            print(f"Output: {task.dst.name}")

        ok, err = run_encode(cmd, task.duration, label, progress_display)
        if not ok:
            report_failure(
                task.src,
                "ffmpeg encode",
                "ffmpeg exited with a non-zero status",
                stderr=err,
                progress_display=progress_display,
                label=label,
            )
            return False

        better, reason = _strictly_better_output(
            task.src,
            tmp_dst,
            selected_audio_count=len(task.audio_streams),
            selected_subtitle_codecs=[
                stream.codec_name.lower() for stream in _mp4_subtitle_streams(task.subtitle_streams)
            ],
        )
        if not better:
            report_failure(
                task.src,
                "post-encode validation",
                f"encoded file rejected; source kept: {reason}",
                progress_display=progress_display,
                label=label,
            )
            return False

        sidecars_ok, sidecar_err, sidecar_paths = extract_subtitle_sidecars(task.src, task.dst, task.subtitle_streams)
        if not sidecars_ok:
            report_failure(
                task.src,
                "subtitle sidecar extraction",
                "could not extract selected DVD/VobSub subtitles; source kept",
                stderr=sidecar_err,
                progress_display=progress_display,
                label=label,
            )
            return False

        src_sz = task.src.stat().st_size
        dst_sz = tmp_dst.stat().st_size
        pct = (dst_sz - src_sz) / src_sz * 100

        try:
            task.src.unlink()
        except OSError as e:
            report_failure(
                task.src,
                "source deletion",
                f"could not delete source: {e}",
                progress_display=progress_display,
                label=label,
            )
            return False

        move_err = _move_file(tmp_dst, task.dst)
        if move_err is not None:
            cleanup_tmp = False
            report_failure(
                task.src,
                "final move",
                f"could not move encoded file to {task.dst}: {move_err}; "
                f"source was deleted; encoded file kept at {tmp_dst}",
                progress_display=progress_display,
                label=label,
            )
            return False

        done = (
            f"Done:   {task.src.name}  {fmt_size(src_sz)} -> {fmt_size(dst_sz)}  "
            f"({'+' if pct >= 0 else ''}{pct:.1f}%)  deleted source"
        )
        if sidecar_paths:
            names = ", ".join(path.name for path in sidecar_paths)
            done += f"  subtitles: {names}"
        if label is not None and label and progress_display:
            progress_display.finish(label, done)
        else:
            print(done)
        return True
    finally:
        if cleanup_tmp:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def live_progress_enabled(mode: str) -> bool:
    if mode == "live":
        return True
    if mode == "plain":
        return False
    return bool(sys.stdout.isatty())


def run_tasks(
    tasks: list[EncodeTask],
    preset: dict[str, Any],
    hdr_policy: str,
    progress_mode: str,
    delete_source: bool,
    tmp_root: Path | None,
) -> bool:
    progress_display = ProgressDisplay(live_progress_enabled(progress_mode))
    ok = True
    try:
        for task in tasks:
            ok = encode_task(task, preset, hdr_policy, progress_display, task.src.name, delete_source, tmp_root) and ok
    finally:
        progress_display.close()
    return ok


def run_from_args(args: argparse.Namespace) -> None:
    if args.delete and args.yes:
        raise ValueError("--delete cannot be combined with --yes")
    if args.tmp_dir and not args.delete:
        raise ValueError("--tmp-dir requires --delete")
    if args.tmp_dir and not args.tmp_dir.is_dir():
        raise ValueError(f"--tmp-dir is not a directory: {args.tmp_dir}")

    preset = PRESETS[args.preset]
    sources = collect_sources(args.paths)
    if not sources:
        sys.exit("No input files found.")

    print(f"Preset: {args.preset} — {preset['desc']}")

    tasks = [
        task
        for src in sources
        if (task := prepare_task(args.preset, src, args.hdr_policy, args.yes, no_crop=args.no_crop))
    ]
    if not tasks:
        print("No files to encode.")
        return

    file_label = "file" if len(tasks) == 1 else "files"
    print(f"Queue: {len(tasks)} {file_label}")

    if not run_tasks(tasks, preset, args.hdr_policy, args.progress, args.delete, args.tmp_dir):
        sys.exit("One or more encodes failed.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Encode files with Apple-friendly HEVC/AAC presets.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("preset", choices=PRESETS.keys())
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument(
        "--hdr-policy",
        choices=("abort", "retag", "tonemap"),
        default="abort",
        help="how to handle HDR-tagged sources",
    )
    parser.add_argument(
        "--no-crop",
        action="store_true",
        help="disable automatic black-border detection and cropping",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="force encoding sources that would otherwise be skipped as already decent",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="encode to a temporary directory, then delete the source only if the output probes strictly better",
    )
    parser.add_argument(
        "--tmp-dir",
        type=Path,
        help="scratch directory for --delete temporary encodes; defaults to the system temp directory",
    )
    parser.add_argument(
        "--progress",
        choices=("auto", "live", "plain"),
        default="auto",
        help="progress renderer; live forces ANSI in-place updates",
    )
    args = parser.parse_args(argv)
    try:
        run_from_args(args)
    except ValueError as e:
        parser.error(str(e))


if __name__ == "__main__":
    main()
