import subprocess
import tempfile
from pathlib import Path

import pytest

from tool.movies import mkvtomp4

from .conftest import FIXTURE_DIR

ASS_CONTENT = """\
[Script Info]
Title: Test
ScriptType: v4.00+
PlayResX: 384
PlayResY: 288

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.50,0:00:01.50,Default,,0,0,0,,Hello
"""


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise AssertionError(f"Command failed: {cmd}\nstderr:\n{result.stderr}")


def _make_fixture(name: str, cmd: list[str]) -> Path:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / name
    if not path.exists():
        _run([*cmd, str(path)])
    return path


def unsupported_streams_fixture() -> Path:
    """MKV with h264 video, AAC audio, FLAC audio, ASS subtitles."""
    name = "unsupported-streams.mkv"
    path = FIXTURE_DIR / name
    if path.exists():
        return path

    with tempfile.NamedTemporaryFile(mode="w", suffix=".ass", delete=False) as af:
        af.write(ASS_CONTENT)
        ass_path = af.name

    try:
        _run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f", "lavfi", "-i", "testsrc2=duration=2:size=160x90:rate=24",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=2",
                "-i", ass_path,
                "-map", "0:v",
                "-map", "1:a",
                "-map", "2:a",
                "-map", "3:s",
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                "-c:a:0", "aac",
                "-c:a:1", "flac",
                "-c:s", "ass",
                "-metadata:s:a:0", "language=eng",
                "-metadata:s:a:1", "language=jpn",
                "-metadata:s:s:0", "language=eng",
                str(path),
            ]
        )
    finally:
        Path(ass_path).unlink(missing_ok=True)

    return path


# ── tests ────────────────────────────────────────────────────────────────


def test_eligible_mkv() -> None:
    """A clean MKV with h264 + 2x AAC — all streams go to mp4."""
    path = FIXTURE_DIR / "two-audio.mkv"
    report, issues = mkvtomp4.plan(path)
    assert issues == [], f"unexpected issues: {issues}"
    assert len(report["mp4"]) == 3  # 1 video + 2 audio
    assert report["extract"] == []
    assert report["block"] == []


def test_unsupported_video_codec() -> None:
    """mpeg2video is blocked — can't make a useful MP4 without video."""
    path = FIXTURE_DIR / "bloated-mpeg2.mkv"
    report, issues = mkvtomp4.plan(path)
    assert len(issues) == 1
    assert "mpeg2video" in issues[0]
    assert len(report["block"]) == 1


def test_extractions_planned() -> None:
    """FLAC + ASS streams are routed to extract; AAC stays in mp4."""
    path = unsupported_streams_fixture()
    report, issues = mkvtomp4.plan(path)

    assert issues == [], f"unexpected issues: {issues}"
    assert report["block"] == []

    # mp4: video (h264) + audio (aac) = 2 streams
    assert len(report["mp4"]) == 2

    # extract: flac audio + ass subtitle = 2 streams
    assert len(report["extract"]) == 2

    # Verify sidecar extensions
    extract_labels = [label for label, _s, _ext in report["extract"]]
    assert any("flac" in label for label in extract_labels)
    assert any("ass" in label for label in extract_labels)


def test_sidecar_naming() -> None:
    """Sidecar filenames include stream index, language, and extension."""
    path = unsupported_streams_fixture()
    report, _issues = mkvtomp4.plan(path)

    for _label, stream, (ext, _fmt) in report["extract"]:
        sidecar = mkvtomp4._sidecar_name("Test", stream, ext)
        assert f"s{stream.index}" in sidecar
        assert sidecar.endswith(ext)

    # Look up specific streams by codec
    flac_stream = next(s for _l, s, _e in report["extract"] if s.codec_name == "flac")
    ass_stream = next(s for _l, s, _e in report["extract"] if s.codec_name == "ass")

    flac_name = mkvtomp4._sidecar_name("Test", flac_stream, ".flac")
    ass_name = mkvtomp4._sidecar_name("Test", ass_stream, ".ass")

    assert flac_name == "Test.s2.jpn.flac"
    assert ass_name == "Test.s3.eng.ass"


def test_non_mkv_container() -> None:
    """An MP4 file should be rejected because it's not MKV."""
    path = FIXTURE_DIR / "sdr-1280x720.mp4"
    _report, issues = mkvtomp4.plan(path)
    assert issues == ["not an MKV / WebM container"]


def test_missing_file() -> None:
    """A nonexistent path should raise on probe, not crash."""
    path = FIXTURE_DIR / "does-not-exist.mkv"
    with pytest.raises(RuntimeError):
        mkvtomp4.plan(path)


@pytest.mark.slow
def test_apply_remux_and_extract(tmp_path: Path) -> None:
    """--apply remuxes supported streams and extracts sidecars."""
    src_fixture = unsupported_streams_fixture()
    src = tmp_path / src_fixture.name
    src.write_bytes(src_fixture.read_bytes())

    dest = tmp_path / "out"

    # Dry run first — should report without error.
    report, issues = mkvtomp4.plan(src)
    assert issues == []

    # Now apply with --dest.
    mkvtomp4.run(src, apply=True, dest=str(dest))

    # MP4 should exist in dest.
    mp4 = dest / f"{src.stem}.mp4"
    assert mp4.is_file()
    assert mp4.stat().st_size > 0

    # Sidecars should exist in dest.
    for _label, stream, (ext, _fmt) in report["extract"]:
        sidecar = dest / mkvtomp4._sidecar_name(src.stem, stream, ext)
        assert sidecar.is_file(), f"missing sidecar: {sidecar}"
        assert sidecar.stat().st_size > 0, f"empty sidecar: {sidecar}"

    # Original MKV should still be there (we don't delete it).
    assert src.is_file()
