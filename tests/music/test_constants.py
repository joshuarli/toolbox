from tool.music.constants import (
    AUDIO_EXTENSIONS,
    DSD,
    GOLD,
    GREEN,
    LOSSLESS,
    LOSSY_ALL,
    LOSSY_HIGH,
    LOSSY_STANDARD,
    MAGENTA,
    RED,
    TAG_NAMES,
)
from tool.music.metadata import Metadata


def test_tag_names_are_metadata_fields():
    assert all(hasattr(Metadata(), key) for _, key in TAG_NAMES)


def test_tag_names():
    labels = [label for label, _ in TAG_NAMES]
    assert "TITLE" in labels
    assert "ARTIST" in labels
    assert "ALBUM" in labels
    assert "TRACK" in labels
    keys = [key for _, key in TAG_NAMES]
    assert "title" in keys
    assert "artist" in keys
    assert "album" in keys
    assert "tracknumber" in keys


def test_lossy_high_non_empty():
    assert len(LOSSY_HIGH) > 0


def test_lossy_standard_non_empty():
    assert len(LOSSY_STANDARD) > 0


def test_lossy_all_includes_high():
    assert LOSSY_HIGH.issubset(LOSSY_ALL)


def test_lossless_non_empty():
    assert len(LOSSLESS) > 0


def test_dsd_non_empty():
    assert len(DSD) > 0


def test_audio_extensions():
    assert ".mp3" in AUDIO_EXTENSIONS
    assert ".flac" in AUDIO_EXTENSIONS
    assert ".m4a" in AUDIO_EXTENSIONS
    assert ".wav" in AUDIO_EXTENSIONS
    assert ".webm" in AUDIO_EXTENSIONS


def test_color_codes():
    assert isinstance(MAGENTA, int)
    assert isinstance(GOLD, int)
    assert isinstance(GREEN, int)
    assert isinstance(RED, int)
    assert 0 <= MAGENTA <= 255
    assert 0 <= GOLD <= 255
    assert 0 <= GREEN <= 255
    assert 0 <= RED <= 255


def test_lossless_and_lossy_disjoint():
    # No codec should be in both lossless and lossy sets
    assert LOSSLESS.isdisjoint(LOSSY_ALL)
