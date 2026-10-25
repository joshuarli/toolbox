#!/usr/bin/env python3
"""
Shared media probing, stream analysis, and quality scoring.
Used by movies.encode and movies.scan_encodes.
"""

import json
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Transfer functions that indicate an HDR master. These tags must be replaced
# with bt709 when transcoding to SDR, or the player applies the wrong EOTF.
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v", ".ts", ".wmv", ".flv"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"}

# BPP low-side checks are normalized against this 1080p reference so SD content
# is not penalized as thin. High-side checks also consider raw BPP because excess
# bits at SD still translate directly into wasted file size.
BPP_REF_PIXELS = 1920 * 1080


def _norm_bpp(bpp: float, width: int, height: int) -> float:
    """Resolution-normalize BPP relative to 1080p reference."""
    pixels = width * height
    return bpp * math.sqrt(pixels / BPP_REF_PIXELS) if pixels else bpp


VCODEC = {
    "hevc": ("A", "H.265/HEVC"),
    "av1": ("A", "AV1"),
    "vp9": ("A", "VP9"),
    "h264": ("B", "H.264/AVC"),
    "vp8": ("B", "VP8"),
    "mpeg4": ("C", "MPEG-4/Xvid"),
    "msmpeg4v3": ("C", "DivX"),
    "msmpeg4v2": ("C", "DivX"),
    "mpeg2video": ("C", "MPEG-2"),
    "mpeg1video": ("C", "MPEG-1"),
    "vc1": ("C", "VC-1"),
    "wmv3": ("C", "WMV3"),
    "wmv2": ("C", "WMV2"),
    "wmv1": ("C", "WMV1"),
    "theora": ("C", "Theora"),
    "h263": ("C", "H.263"),
}

# Per-tier BPP color zones: (red_low, yellow_low, yellow_high, red_high)
BPP_RANGES = {
    "A": (0.006, 0.010, 0.070, 0.200),
    "B": (0.020, 0.040, 0.200, 0.400),
    "C": (0.040, 0.060, 0.350, 0.700),
}

ACODEC = {
    "flac": ("L", "FLAC"),
    "truehd": ("L", "TrueHD"),
    "alac": ("L", "ALAC"),
    "pcm_s16le": ("L", "PCM/16"),
    "pcm_s24le": ("L", "PCM/24"),
    "pcm_s32le": ("L", "PCM/32"),
    "dts": ("A", "DTS"),
    "aac": ("A", "AAC"),
    "eac3": ("A", "E-AC3"),
    "ac3": ("A", "AC3"),
    "opus": ("A", "Opus"),
    "mp3": ("B", "MP3"),
    "vorbis": ("B", "Vorbis"),
    "mp2": ("C", "MP2"),
    "wmav2": ("C", "WMA"),
    "wmav1": ("C", "WMA"),
    "wmapro": ("C", "WMA Pro"),
}

MIN_KBPS_PER_CH = {
    "opus": 16,
    "aac": 24,
    "vorbis": 24,
    "ac3": 32,
    "eac3": 32,
    "dts": 32,
    "mp3": 48,
    "mp2": 64,
}

CHANNEL_NAMES = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}
TEXT_SUB_CODECS = {"subrip", "ass", "ssa", "webvtt", "mov_text", "text", "ttml", "srt"}
IMAGE_SUB_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "pgssub", "vobsub", "dvbsub", "dvb_teletext"}
LOW_CONFIDENCE_AUDIO_SAMPLE_BPS = 16_000
AUDIO_PACKET_SAMPLE_TIMEOUT_SECS = 15
AUDIO_PACKET_FULL_TIMEOUT_SECS = 60


def parse_fps(r: str) -> float:
    try:
        num, den = r.split("/")
        den = int(den)
        return int(num) / den if den else 0.0
    except Exception:
        return 0.0


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except TypeError, ValueError:
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except TypeError, ValueError:
        return default


def _to_str(value: Any, default: str = "") -> str:
    return default if value is None else str(value)


@dataclass(frozen=True)
class ProbeFormat:
    duration: float = 0.0
    size: int = 0
    bit_rate: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ProbeFormat:
        return cls(
            duration=_to_float(data.get("duration")),
            size=_to_int(data.get("size")),
            bit_rate=_to_int(data.get("bit_rate")),
            raw=data,
        )

    def get(self, key: str, default: Any = None) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        return self.raw.get(key, default)


@dataclass(frozen=True)
class ProbeStream:
    index: int = 0
    codec_type: str = ""
    codec_name: str = "unknown"
    profile: str = ""
    width: int = 0
    height: int = 0
    pix_fmt: str = ""
    bits_per_raw_sample: str = ""
    avg_frame_rate: str = "0/1"
    r_frame_rate: str = "0/1"
    field_order: str = ""
    bit_rate: int = 0
    color_transfer: str = ""
    color_primaries: str = ""
    color_space: str = ""
    color_range: str = ""
    channels: int = 0
    channel_layout: str = ""
    disposition: dict[str, Any] = field(default_factory=dict)
    tags: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ProbeStream:
        return cls(
            index=_to_int(data.get("index")),
            codec_type=_to_str(data.get("codec_type")),
            codec_name=_to_str(data.get("codec_name"), "unknown"),
            profile=_to_str(data.get("profile")),
            width=_to_int(data.get("width")),
            height=_to_int(data.get("height")),
            pix_fmt=_to_str(data.get("pix_fmt")),
            bits_per_raw_sample=_to_str(data.get("bits_per_raw_sample")),
            avg_frame_rate=_to_str(data.get("avg_frame_rate"), "0/1"),
            r_frame_rate=_to_str(data.get("r_frame_rate"), "0/1"),
            field_order=_to_str(data.get("field_order")),
            bit_rate=_to_int(data.get("bit_rate")),
            color_transfer=_to_str(data.get("color_transfer")),
            color_primaries=_to_str(data.get("color_primaries")),
            color_space=_to_str(data.get("color_space")),
            color_range=_to_str(data.get("color_range")),
            channels=_to_int(data.get("channels")),
            channel_layout=_to_str(data.get("channel_layout")),
            disposition=dict(data.get("disposition", {})),
            tags=dict(data.get("tags", {})),
            raw=data,
        )

    def get(self, key: str, default: Any = None) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        return self.raw.get(key, default)

    def with_bit_rate(self, bit_rate: int) -> ProbeStream:
        raw = dict(self.raw, bit_rate=str(bit_rate))
        return ProbeStream.from_json(raw)


@dataclass(frozen=True)
class ProbeInfo:
    streams: list[ProbeStream]
    format: ProbeFormat
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ProbeInfo:
        return cls(
            streams=[ProbeStream.from_json(s) for s in data.get("streams", [])],
            format=ProbeFormat.from_json(data.get("format", {})),
            raw=data,
        )

    def get(self, key: str, default: Any = None) -> Any:
        if key == "streams":
            return self.streams
        if key == "format":
            return self.format
        return self.raw.get(key, default)

    @property
    def video_streams(self) -> list[ProbeStream]:
        return [s for s in self.streams if s.codec_type == "video" and not is_image_video_stream(s)]

    @property
    def audio_streams(self) -> list[ProbeStream]:
        return [s for s in self.streams if s.codec_type == "audio"]

    @property
    def subtitle_streams(self) -> list[ProbeStream]:
        return [s for s in self.streams if s.codec_type == "subtitle"]

    @property
    def first_video(self) -> ProbeStream | None:
        return next(iter(self.video_streams), None)


def is_image_video_stream(stream: ProbeStream) -> bool:
    if stream.codec_type != "video":
        return False

    if any(_to_int(stream.disposition.get(key)) for key in ("attached_pic", "still_image", "timed_thumbnails")):
        return True

    mimetype = str(stream.tags.get("mimetype") or "").lower()
    if mimetype.startswith("image/"):
        return True

    filename = str(stream.tags.get("filename") or "").lower()
    return Path(filename).suffix in IMAGE_EXTENSIONS


def ffprobe_json(path: Path) -> dict[str, Any]:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "ffprobe failed")
    return json.loads(r.stdout)


def ffprobe(path: Path) -> ProbeInfo:
    return ProbeInfo.from_json(ffprobe_json(path))


def _packet_bitrate(path: Path, audio_ordinal: int, read_interval: str | None, timeout: int) -> int | None:
    cmd = ["ffprobe", "-v", "quiet", "-select_streams", f"a:{audio_ordinal}"]
    if read_interval is not None and read_interval:
        cmd += ["-read_intervals", read_interval]
    cmd += ["-show_packets", "-show_entries", "packet=size,pts_time,duration_time", "-print_format", "json", str(path)]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except OSError, subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return None
    try:
        pkts = json.loads(r.stdout).get("packets", [])
    except json.JSONDecodeError:
        return None
    if len(pkts) < 2:
        return None

    total_bytes = 0
    duration = 0.0
    first_pts: float | None = None
    last_pts: float | None = None
    last_duration = 0.0
    for pkt in pkts:
        total_bytes += _to_int(pkt.get("size"))
        pkt_duration = _to_float(pkt.get("duration_time"))
        if pkt_duration > 0:
            duration += pkt_duration
            last_duration = pkt_duration
        pts = _to_float(pkt.get("pts_time"), -1.0)
        if pts >= 0:
            first_pts = pts if first_pts is None else first_pts
            last_pts = pts

    if total_bytes <= 0:
        return None
    if duration <= 0 and first_pts is not None and last_pts is not None:
        duration = last_pts - first_pts + last_duration
    if duration <= 0:
        return None
    return int(total_bytes * 8 / duration)


def probe_audio_br(path: Path, audio_ordinal: int = 0, sample_secs: int = 8) -> int | None:
    """Estimate audio bitrate from packet sizes when stream metadata omits it.

    A short packet window is fast for normal files, but leading silence in VBR
    codecs can look like near-zero bitrate. Very low samples fall back to a full
    stream packet sum, which is slower but accurate.
    """
    br = _packet_bitrate(path, audio_ordinal, f"%+{sample_secs}", AUDIO_PACKET_SAMPLE_TIMEOUT_SECS)
    if br is None or br >= LOW_CONFIDENCE_AUDIO_SAMPLE_BPS:
        return br

    full_br = _packet_bitrate(path, audio_ordinal, None, AUDIO_PACKET_FULL_TIMEOUT_SECS)
    return full_br or br


def target_audio_bitrate(channels: int, source_kbps: int | None = None) -> str:
    """AAC target: 256k stereo, ~64k/ch surround, capped at 448k."""
    target = 256 if channels <= 2 else min(64 * channels, 448)
    if source_kbps is not None and source_kbps and 64 <= source_kbps < target:
        return f"{source_kbps}k"
    return f"{target}k"


def with_estimated_bitrates(path: Path, data: ProbeInfo) -> ProbeInfo:
    """Fill missing stream bitrates from packet sizes and container bitrate."""
    audio_br_values: list[int] = []
    for audio_ordinal, stream in enumerate(data.audio_streams):
        br = stream.bit_rate
        if not br:
            br = probe_audio_br(path, audio_ordinal) or 0
        audio_br_values.append(br)

    est_video_br = max(data.format.bit_rate - sum(audio_br_values), 0)
    first_video = data.first_video

    audio_ordinal = 0
    streams: list[ProbeStream] = []
    for stream in data.streams:
        if stream.codec_type == "audio":
            br = audio_br_values[audio_ordinal]
            audio_ordinal += 1
            streams.append(stream.with_bit_rate(br) if not stream.bit_rate and br else stream)
        elif first_video and stream.index == first_video.index and not stream.bit_rate and est_video_br:
            streams.append(stream.with_bit_rate(est_video_br))
        else:
            streams.append(stream)

    return ProbeInfo(streams=streams, format=data.format, raw=data.raw)


def _bit_depth(stream: ProbeStream) -> int:
    """Extract luma bit depth from stream metadata. Returns 0 if unknown."""
    bpcs = stream.bits_per_raw_sample
    if bpcs:
        try:
            return int(bpcs)
        except ValueError, TypeError:
            pass
    pix_fmt = stream.pix_fmt
    for depth in (16, 12, 10):
        if str(depth) in pix_fmt:
            return depth
    return 8 if pix_fmt else 0


def analyze_video(stream: ProbeStream) -> tuple[dict[str, Any], list[str]]:
    codec_name = stream.codec_name.lower()
    tier, codec_display = VCODEC.get(codec_name, ("?", codec_name))

    width = stream.width
    height = stream.height
    # avg_frame_rate reflects actual presentation cadence; r_frame_rate is the
    # codec-declared timebase and can be double for interlaced or VFR sources.
    fps = parse_fps(stream.avg_frame_rate) or parse_fps(stream.r_frame_rate)
    field = stream.field_order
    video_br = stream.bit_rate

    bpp = video_br / (width * height * fps) if (width and height and fps and video_br) else 0.0
    fps_str = f"{fps:.3f}".rstrip("0").rstrip(".")

    info = {
        "codec_name": codec_name,
        "codec": codec_display,
        "tier": tier,
        "width": width,
        "height": height,
        "fps": fps_str,
        "fps_raw": fps,
        "br_mbps": round(video_br / 1e6, 2),
        "bpp": round(bpp, 4),
        "bit_depth": _bit_depth(stream),
        "field_order": field,
    }

    flags: list[str] = []
    if tier == "C":
        flags.append("old_codec")
    elif tier == "B":
        flags.append("not_h265")
    elif tier == "?":
        flags.append(f"unknown_codec:{codec_name}")

    if bpp and tier in BPP_RANGES:
        rl, _yl, _yh, rh = BPP_RANGES[tier]
        nbpp = _norm_bpp(bpp, width, height)
        if bpp > rh or nbpp > rh:
            flags.append("bloated")
        elif nbpp < rl:
            flags.append("thin")

    if field and field not in ("progressive", "unknown"):
        flags.append(f"interlaced:{field}")

    return info, flags


def analyze_audio(stream: ProbeStream) -> tuple[dict[str, Any], list[str]]:
    codec_name = stream.codec_name.lower()
    profile = stream.profile
    channels = stream.channels
    br = stream.bit_rate
    lang = stream.tags.get("language", "")
    layout = stream.channel_layout

    tier, codec_display = ACODEC.get(codec_name, ("?", codec_name))

    if codec_name == "dts" and "MA" in profile.upper():
        tier = "L"
        codec_display = "DTS-HD MA"
    elif codec_name == "dts" and profile:
        codec_display = f"DTS ({profile})"

    ch_label = CHANNEL_NAMES.get(channels, f"{channels}ch") if channels else layout or "?"
    br_kbps = round(br / 1000) if br else None

    info = {
        "codec_name": codec_name,
        "codec": codec_display,
        "tier": tier,
        "ch": ch_label,
        "channels": channels,
        "br_kbps": br_kbps,
        "lang": lang,
    }

    flags: list[str] = []
    if tier == "C":
        flags.append("old_audio")
    elif tier == "B" and codec_name == "mp3":
        flags.append("mp3_audio")

    if tier != "L" and br_kbps is not None and br_kbps and channels:
        min_kbps = MIN_KBPS_PER_CH.get(codec_name, 32) * channels
        if br_kbps < min_kbps:
            flags.append("low_audio_br")

    return info, flags


def mp4_sub_args(streams: list[ProbeStream]) -> list[str]:
    """Return ffmpeg map+codec args for MP4-compatible subtitle streams.

    Text-based subtitles (SRT, ASS, WebVTT) are mapped and converted to
    mov_text — the only subtitle format MP4 containers natively support.
    Image-based subtitles (PGS, VobSub) cannot be stored in MP4 and are
    silently dropped. ASS/SSA styling (fonts, positioning, colour) is lost
    in the mov_text conversion; plain dialogue text is preserved.
    Returns a flat list of ffmpeg args, empty if no compatible subs found.
    """
    text_indices = [
        i for i, s in enumerate(streams) if s.codec_type == "subtitle" and s.codec_name.lower() in TEXT_SUB_CODECS
    ]
    if not text_indices:
        return []
    args: list[str] = []
    for i in text_indices:
        args += ["-map", f"0:{i}"]
    args += ["-c:s", "mov_text"]
    return args


def describe_sub(stream: ProbeStream) -> str:
    codec = stream.codec_name.lower()
    lang = stream.tags.get("language", "und")
    if codec in TEXT_SUB_CODECS:
        kind = "soft"
    elif codec in IMAGE_SUB_CODECS:
        kind = "image"
    else:
        kind = codec or "?"
    return f"{lang} ({kind})"


def quality_score(
    video_info: dict[str, Any],
    video_flags: list[str],
) -> tuple[int, list[str]]:
    """Score 0-100: how good the source encode already is.

    Higher = already a quality encode; re-encoding risks generation loss.
    Returns (score, [human-readable reason strings]).

    Breakdown:
      50 pts  codec efficiency (AV1=50, HEVC/VP9=35, H.264=30, older=0)
      40 pts  BPP health       (green=40, acceptable=20, flagged=0)
      20 pts  bit depth        (10-bit+=20, 8-bit=10, unknown=0)
      Scores are capped at 100.
    """
    score = 0
    reasons: list[str] = []

    codec = video_info.get("codec_name", "")
    tier = video_info.get("tier", "?")
    flag_keys = {f.split(":")[0] for f in video_flags}

    if codec == "av1":
        score += 50
        reasons.append(f"modern codec ({video_info.get('codec', codec)})")
    elif codec in ("hevc", "vp9"):
        score += 35
        reasons.append(f"modern codec ({video_info.get('codec', codec)})")
    elif tier == "B":
        score += 30
        reasons.append(f"decent codec ({video_info.get('codec', codec)})")

    raw_bpp = video_info.get("bpp", 0.0)
    nbpp = _norm_bpp(raw_bpp, video_info.get("width", 0), video_info.get("height", 0))
    ranges = BPP_RANGES.get(tier)
    if nbpp and ranges and "bloated" not in flag_keys and "thin" not in flag_keys:
        _rl, yl, yh, _rh = ranges
        if yl <= nbpp <= yh and (tier == "A" or raw_bpp <= yh):
            score += 40
            reasons.append(f"healthy BPP ({raw_bpp:.4f})")
        else:
            score += 20
            reasons.append(f"acceptable BPP ({raw_bpp:.4f})")

    bd = video_info.get("bit_depth", 0)
    if bd >= 10:
        score += 20
        reasons.append(f"{bd}-bit")
    elif bd == 8:
        score += 10

    return min(score, 100), reasons


def analyze_info(path: Path, data: ProbeInfo) -> dict[str, Any]:
    """Analyze already-probed media, filling missing bitrate metadata as needed."""
    data = with_estimated_bitrates(path, data)
    video_streams = data.video_streams
    audio_streams = data.audio_streams
    sub_streams = data.subtitle_streams

    if not video_streams:
        return {"error": "no video stream"}

    v_info, v_flags = analyze_video(video_streams[0])

    audio_info_list: list[dict[str, Any]] = []
    audio_flags_all: list[str] = []
    for stream in audio_streams:
        ai, af = analyze_audio(stream)
        audio_info_list.append(ai)
        audio_flags_all.extend(af)

    return {
        "video": v_info,
        "video_flags": v_flags,
        "audio": audio_info_list,
        "audio_flags": audio_flags_all,
        "subs": [describe_sub(s) for s in sub_streams],
        "duration_min": round(data.format.duration / 60, 1),
        "size_gb": round(data.format.size / 1e9, 2),
    }


def analyze(path: Path) -> dict[str, Any]:
    """Full probe of a media file. Returns structured analysis for display or scoring."""
    try:
        data = ffprobe(path)
    except (RuntimeError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        return {"error": str(e)}
    return analyze_info(path, data)
