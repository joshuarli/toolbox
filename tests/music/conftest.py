import functools
import json
import re
import subprocess
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


def _make_audio(path: str, codec: str, *, sample_rate: int = 44100) -> None:
    """Create a 0.5-second sine tone at *path* with the given ffmpeg codec."""
    encoder = {"flac": "flac", "mp3": "libmp3lame", "aac": "aac"}.get(codec, codec)
    subprocess.run(
        [
            "ffmpeg",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.5",
            "-sample_rate",
            str(sample_rate),
            "-c:a",
            encoder,
            "-y",
            path,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


@pytest.fixture
def flac_file(tmp_path):
    p = tmp_path / "test.flac"
    _make_audio(str(p), "flac")
    return str(p)


@pytest.fixture
def mp3_file(tmp_path):
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
    p = tmp_path / "test.opus"
    _make_audio(str(p), "libopus", sample_rate=48000)
    return str(p)


@pytest.fixture
def ogg_file(tmp_path):
    p = tmp_path / "test.ogg"
    _make_audio(str(p), "libvorbis")
    return str(p)


@pytest.fixture
def load_fixture():
    """Load a recorded AcoustID / MusicBrainz response from tests/music/fixtures/."""

    def load(name: str):
        return json.loads((FIXTURES / name).read_text())

    return load
