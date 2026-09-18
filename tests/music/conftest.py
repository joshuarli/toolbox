import subprocess

import pytest


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
