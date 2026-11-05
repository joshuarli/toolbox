import functools
import json
import math
import re
import struct
import subprocess
import wave
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _ffmpeg_available() -> bool:
    try:
        subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return True
    except Exception:
        return False


_FFMPEG = _ffmpeg_available()


@pytest.fixture(scope="session")
def ffmpeg() -> bool:
    return _FFMPEG


@functools.cache
def _ffmpeg_encoder_names() -> frozenset[str]:
    """Names from `ffmpeg -encoders`; empty when ffmpeg is missing."""
    try:
        proc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30)
    except Exception:
        return frozenset()
    if proc.returncode != 0:
        return frozenset()
    names = set()
    for line in proc.stdout.splitlines():
        match = re.match(r"^ [.A-Z]+ (\S+)", line)
        if match:
            names.add(match.group(1))
    return frozenset(names)


needs_libvorbis = pytest.mark.skipif(
    "libvorbis" not in _ffmpeg_encoder_names(), reason="libvorbis encoder not available"
)


def _need_encoder(name: str) -> None:
    if name not in _ffmpeg_encoder_names():
        pytest.skip(f"{name} encoder not available")


def _write_sine_wav(path: Path, *, sample_rate: int = 44100, duration: float = 0.5, frequency: int = 440) -> None:
    nframes = int(sample_rate * duration)
    with wave.open(str(path), "w") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        for i in range(nframes):
            sample = int(32767 * 0.5 * math.sin(2 * math.pi * frequency * i / sample_rate))
            wav.writeframes(struct.pack("<h", sample))


def _make_audio(path: str, codec: str, *, sample_rate: int = 44100) -> None:
    """Create a 0.5-second sine tone at *path* with the given ffmpeg codec."""
    dest = Path(path)
    src = dest.with_name(dest.stem + ".src.wav")
    _write_sine_wav(src, sample_rate=sample_rate)
    if codec == "pcm_s16le" and dest.suffix == ".wav":
        src.replace(dest)
        return
    encoder = {"flac": "flac", "mp3": "libmp3lame", "aac": "aac"}.get(codec, codec)
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-i",
                str(src),
                "-c:a",
                encoder,
                "-y",
                str(dest),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    finally:
        if src.exists():
            src.unlink()


@pytest.fixture
def flac_file(tmp_path):
    p = tmp_path / "test.flac"
    _make_audio(str(p), "flac")
    return str(p)


@pytest.fixture
def mp3_file(tmp_path):
    _need_encoder("libmp3lame")
    p = tmp_path / "test.mp3"
    _make_audio(str(p), "mp3")
    return str(p)


@pytest.fixture
def m4a_file(tmp_path):
    p = tmp_path / "test.m4a"
    _make_audio(str(p), "aac")
    return str(p)


@pytest.fixture
def wav_file(tmp_path):
    p = tmp_path / "test.wav"
    _make_audio(str(p), "pcm_s16le")
    return str(p)


@pytest.fixture
def opus_file(tmp_path):
    _need_encoder("libopus")
    p = tmp_path / "test.opus"
    _make_audio(str(p), "libopus", sample_rate=48000)
    return str(p)


@pytest.fixture
def ogg_file(tmp_path):
    _need_encoder("libvorbis")
    p = tmp_path / "test.ogg"
    _make_audio(str(p), "libvorbis")
    return str(p)


@pytest.fixture
def load_fixture():
    """Load a recorded AcoustID / MusicBrainz response from tests/music/fixtures/."""

    def load(name: str):
        return json.loads((FIXTURES / name).read_text())

    return load
