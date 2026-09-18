from mutagen.ac3 import AC3
from mutagen.aiff import AIFF
from mutagen.dsdiff import DSDIFF
from mutagen.dsf import DSF
from mutagen.flac import FLAC
from mutagen.monkeysaudio import MonkeysAudio
from mutagen.mp3 import MP3
from mutagen.musepack import Musepack
from mutagen.oggflac import OggFLAC
from mutagen.oggopus import OggOpus
from mutagen.oggspeex import OggSpeex
from mutagen.oggvorbis import OggVorbis
from mutagen.tak import TAK
from mutagen.trueaudio import TrueAudio
from mutagen.wave import WAVE
from mutagen.wavpack import WavPack

# ── Mutagen type-based quality tiers ──────────────────────────────────────
# Use isinstance(audio, TIER) when you have a mutagen file object.
# These classify at the *container* level; notably MP4 is absent because
# it can hold either AAC (lossy) or ALAC (lossless).

LOSSY_HIGH_TYPES = (OggOpus, OggVorbis)

LOSSY_STANDARD_TYPES = (MP3, AC3, Musepack)

LOSSY_ANCIENT_TYPES = (OggSpeex,)

LOSSY_ALL_TYPES = LOSSY_HIGH_TYPES + LOSSY_STANDARD_TYPES + LOSSY_ANCIENT_TYPES

LOSSLESS_TYPES = (FLAC, WAVE, AIFF, WavPack, TrueAudio, MonkeysAudio, TAK, OggFLAC)

DSD_TYPES = (DSF, DSDIFF)

# ── ffprobe codec-name sets (codec level, not container) ──────────────────
# ffprobe reports the actual audio codec (e.g. "aac", "alac", "pcm_s16le"),
# which is finer-grained than the container type.  These sets are used by
# scan.py for codec classification when only a codec_name string is available.

LOSSY_HIGH = {"aac", "opus", "vorbis"}
LOSSY_STANDARD = {"mp3", "mp2", "ac3", "eac3", "dts", "wma", "wmav1", "wmav2", "wmavoice", "speex", "ra"}
LOSSY_ANCIENT = {"gsm", "adpcm", "g723", "g729", "amr", "amr_nb", "amr_wb", "ilbc"}
LOSSY_ALL = LOSSY_HIGH | LOSSY_STANDARD | LOSSY_ANCIENT

LOSSLESS = {
    "flac",
    "alac",
    "ape",
    "tta",
    "wavpack",
    "wmalossless",
    "mlp",
    "truehd",
    "pcm_s16le",
    "pcm_s16be",
    "pcm_s24le",
    "pcm_s24be",
    "pcm_s32le",
    "pcm_s32be",
    "pcm_f32le",
    "pcm_f32be",
    "pcm_f64le",
    "pcm_f64be",
    "pcm_u8",
    "pcm_s8",
}

DSD = {"dsd_lsbf", "dsd_msbf", "dsd_lsbf_planar", "dsd_msbf_planar"}

AUDIO_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".m4b",
    ".m4p",
    ".aac",
    ".flac",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
    ".aiff",
    ".aif",
    ".wma",
    ".wv",
    ".ape",
    ".dsf",
    ".dff",
    ".tta",
    ".caf",
    ".ac3",
    ".eac3",
    ".dts",
    ".thd",
    ".mlp",
    ".ra",
    ".rm",
    ".spx",
    ".tak",
    ".oga",
}

# (display label, mutagen key) for the four standard tags shown in the MISSING column
TAG_NAMES = [
    ("TITLE", "title"),
    ("ARTIST", "artist"),
    ("ALBUM", "album"),
    ("TRACK", "tracknumber"),
]

# ANSI 256-color codes — gaming-loot quality scale
MAGENTA = 201  # legendary — lossless, DSD
GOLD = 220  # epic — high-bitrate lossy
BLUE = 39  # rare — decent
GREEN = 42  # common — low-but-usable
GREY = 240  # junk — very low / unknown
RED = 196  # broken — ancient codecs, sub-64k
