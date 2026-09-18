from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tool.music.constants import MAGENTA, RED
from tool.music.verdict import (
    DSD,
    HI_RES_NO_HF_DB,
    LOW_ENERGY_DB,
    NO_HF_DB,
    analyze_steepness,
    compute_verdict,
)


def _make_spec(*, rms_overall=-10.0, rms_peak_overall=-5.0, **overrides):
    """Build a spectrum dict with sensible defaults for a clean file."""
    spec = {
        "rms_overall": rms_overall,
        "rms_peak_overall": rms_peak_overall,
        "rms_peak_15000": -20.0,
        "rms_peak_17000": -25.0,
        "rms_peak_19000": -30.0,
        "rms_peak_20500": -35.0,
        "rms_peak_21500": -40.0,
    }
    spec.update(overrides)
    return spec


class TestComputeVerdictCodecChecks:
    def test_lossy_codec_returns_na(self):
        for codec in ["aac", "mp3", "opus", "vorbis"]:
            text, _color, dim = compute_verdict(codec, Path("test.flac"), 44100)
            assert text == "N/A (Lossy Codec)"
            assert dim is True

    def test_dsd_returns_dsd(self):
        for codec in DSD:
            text, color, dim = compute_verdict(codec, Path("test.dsf"), 2822400)
            assert text == "Lossless Codec (DSD)"
            assert color == MAGENTA
            assert dim is False

    def test_unknown_codec(self):
        text, _color, dim = compute_verdict("weird_format", Path("test.bin"), 44100)
        assert text == "Unknown Codec"
        assert dim is True

    def test_low_sample_rate(self):
        text, _color, _dim = compute_verdict("flac", Path("test.flac"), 22050)
        assert text == "Inconclusive (Low SR)"

    def test_none_sample_rate(self):
        text, _color, _dim = compute_verdict("flac", Path("test.flac"), None)
        assert text == "Inconclusive (Low SR)"


class TestComputeVerdictWithMockedAnalysis:
    def test_analysis_failure(self):
        with patch("tool.music.verdict.analyze_steepness", return_value=None):
            text, color, _dim = compute_verdict("flac", Path("test.flac"), 44100)
            assert text == "Error"
            assert color == RED

    def test_low_energy_overall_rms(self):
        spec = _make_spec(rms_peak_overall=LOW_ENERGY_DB - 2)
        with patch("tool.music.verdict.analyze_steepness", return_value=spec):
            text, _color, _dim = compute_verdict("flac", Path("test.flac"), 44100)
            assert text == "Inconclusive (Low Energy)"

    def test_low_energy_no_hf(self):
        spec = _make_spec(rms_peak_15000=NO_HF_DB - 2)
        with patch("tool.music.verdict.analyze_steepness", return_value=spec):
            text, _color, _dim = compute_verdict("flac", Path("test.flac"), 44100)
            assert text == "Inconclusive (Low Energy)"

    def test_brickwall_detected(self):
        # Steep drop between 15000 and 17000 bands
        spec = _make_spec(rms_peak_15000=-20.0, rms_peak_17000=-50.0)
        with patch("tool.music.verdict.analyze_steepness", return_value=spec):
            text, color, _dim = compute_verdict("flac", Path("test.flac"), 44100)
            assert text == "Suspected Transcode (Lossy)"
            assert color == RED

    def test_brickwall_at_higher_band(self):
        # Steep drop between 20500 and 21500 bands
        spec = _make_spec(rms_peak_20500=-35.0, rms_peak_21500=-70.0)
        with patch("tool.music.verdict.analyze_steepness", return_value=spec):
            text, _color, _dim = compute_verdict("flac", Path("test.flac"), 44100)
            assert text == "Suspected Transcode (Lossy)"

    def test_no_brickwall(self):
        # Gentle roll-off, no single drop > BRICKWALL_DROP_DB
        spec = _make_spec()
        with patch("tool.music.verdict.analyze_steepness", return_value=spec):
            text, color, _dim = compute_verdict("flac", Path("test.flac"), 44100)
            assert text == "No Brickwall Detected"
            assert color == MAGENTA

    def test_hi_res_upsampled(self):
        spec = _make_spec(rms_peak_25000=HI_RES_NO_HF_DB - 5)
        with patch("tool.music.verdict.analyze_steepness", return_value=spec):
            text, _color, _dim = compute_verdict("flac", Path("test.flac"), 96000)
            assert text == "Likely Upsampled"

    def test_hi_res_genuine(self):
        spec = _make_spec(rms_peak_25000=HI_RES_NO_HF_DB + 5)
        with patch("tool.music.verdict.analyze_steepness", return_value=spec):
            text, _color, _dim = compute_verdict("flac", Path("test.flac"), 96000)
            assert text == "No Brickwall Detected"

    def test_custom_brickwall_threshold(self):
        # A drop of 10 dB with default threshold (25) would not trigger
        spec = _make_spec(rms_peak_15000=-20.0, rms_peak_17000=-30.0)
        with patch("tool.music.verdict.analyze_steepness", return_value=spec):
            text, _, _ = compute_verdict("flac", Path("test.flac"), 44100)
            assert text == "No Brickwall Detected"
        # But with custom threshold of 5 it would
        with patch("tool.music.verdict.analyze_steepness", return_value=spec):
            text, _, _ = compute_verdict("flac", Path("test.flac"), 44100, brickwall_threshold=5.0)
            assert text == "Suspected Transcode (Lossy)"


class TestAnalyzeSteepness:
    def test_rejects_low_sample_rate(self):
        result = analyze_steepness(Path("test.flac"), 22050)
        assert result is None

    def test_rejects_none_sample_rate(self):
        # analyze_steepness does not guard against None sample_rate — only the
        # public compute_verdict checks that.  Direct calls raise TypeError.
        with pytest.raises(TypeError):
            analyze_steepness(Path("test.flac"), None)  # type: ignore[arg-type]

    @patch("subprocess.run")
    def test_parses_valid_output(self, mock_run):
        # Build stderr with 6 bands for 44100 Hz (no hi-res)
        mock_result = MagicMock()
        lines = []
        for _ in range(6):
            lines.append("RMS level dB: -10.0")
            lines.append("RMS peak dB: -5.0")
        mock_result.stderr = "\n".join(lines)
        mock_result.returncode = 0
        mock_run.return_value = mock_result

        result = analyze_steepness(Path("test.flac"), 44100, 120.0)
        assert result is not None
        assert "rms_overall" in result
        assert "rms_peak_overall" in result
        assert "rms_peak_15000" in result
        assert "rms_peak_21500" in result
        assert "rms_peak_25000" not in result

    @patch("subprocess.run")
    def test_includes_hi_res_band(self, mock_run):
        mock_result = MagicMock()
        lines = []
        for _ in range(7):  # 7 bands for > 48k
            lines.append("RMS level dB: -10.0")
            lines.append("RMS peak dB: -5.0")
        mock_result.stderr = "\n".join(lines)
        mock_result.returncode = 0
        mock_run.return_value = mock_result

        result = analyze_steepness(Path("test.flac"), 96000)
        assert result is not None
        assert "rms_peak_25000" in result

    @patch("subprocess.run")
    def test_handles_inf(self, mock_run):
        mock_result = MagicMock()
        lines = []
        for _ in range(6):
            lines.append("RMS level dB: -inf")
            lines.append("RMS peak dB: -inf")
        mock_result.stderr = "\n".join(lines)
        mock_result.returncode = 0
        mock_run.return_value = mock_result

        result = analyze_steepness(Path("test.flac"), 44100)
        assert result is not None
        assert result["rms_peak_overall"] == pytest.approx(-200.0)

    @patch("subprocess.run")
    def test_returns_none_on_wrong_band_count(self, mock_run):
        mock_result = MagicMock()
        mock_result.stderr = "RMS level dB: -10.0\nRMS peak dB: -5.0"  # only 1 band
        mock_result.returncode = 0
        mock_run.return_value = mock_result

        result = analyze_steepness(Path("test.flac"), 44100)
        assert result is None

    @patch("subprocess.run")
    def test_returns_none_on_nonzero_returncode(self, mock_run):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_run.return_value = mock_result

        result = analyze_steepness(Path("test.flac"), 44100)
        assert result is None

    @patch("subprocess.run")
    def test_returns_none_on_timeout(self, mock_run):
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired("ffmpeg", 120)
        result = analyze_steepness(Path("test.flac"), 44100)
        assert result is None
