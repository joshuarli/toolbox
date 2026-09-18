from unittest.mock import MagicMock, patch

import pytest

from tool.music.loudness import (
    analyze_ebur128,
    compute_mastering_verdict,
)


class TestComputeMasteringVerdict:
    def test_true_peak_clipping(self):
        spec = {"integrated_lufs": -8.0, "lra": 6.0, "true_peak_dbtp": 1.5}
        verdict = compute_mastering_verdict(spec)
        assert verdict is not None
        text, _, _ = verdict
        assert text == "True Peak Clipping"

    def test_true_peak_at_limit_not_clipping(self):
        spec = {"integrated_lufs": -8.0, "lra": 6.0, "true_peak_dbtp": 0.0}
        verdict = compute_mastering_verdict(spec)
        # 0.0 is not > 0.0, so no clipping verdict
        assert verdict is None

    def test_dynamically_flat(self):
        spec = {"integrated_lufs": -8.0, "lra": 3.0, "true_peak_dbtp": -1.0}
        verdict = compute_mastering_verdict(spec)
        assert verdict is not None
        text, _, _ = verdict
        assert text == "Dynamically Flat"

    def test_clean(self):
        spec = {"integrated_lufs": -14.0, "lra": 8.0, "true_peak_dbtp": -2.0}
        verdict = compute_mastering_verdict(spec)
        assert verdict is None

    def test_custom_thresholds(self):
        spec = {"integrated_lufs": -8.0, "lra": 4.0, "true_peak_dbtp": 0.5}
        # Custom true_peak_limit below actual value → clipping
        verdict = compute_mastering_verdict(spec, true_peak_limit=-1.0)
        assert verdict is not None
        assert verdict[0] == "True Peak Clipping"

        # Higher lra_flat_limit catches flat dynamics; must also raise
        # true_peak_limit so the clipping check doesn't fire first.
        verdict = compute_mastering_verdict(spec, true_peak_limit=1.0, lra_flat_limit=10.0)
        assert verdict is not None
        assert verdict[0] == "Dynamically Flat"


class TestAnalyzeEbur128:
    def test_parses_valid_output(self):
        stderr = (
            "  Integrated loudness:\n"
            "    I:         -14.2 LUFS\n"
            "    Threshold: -24.2 LUFS\n"
            "  Loudness range:\n"
            "    LRA:         8.5 LU\n"
            "    Threshold:   -34.2 LUFS\n"
            "  True peak:\n"
            "    Peak:         -1.3 dBFS\n"
        )
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stderr = stderr
            mock_run.return_value = mock_result

            result = analyze_ebur128("test.flac", None)
            assert result is not None
            assert result["integrated_lufs"] == pytest.approx(-14.2)
            assert result["lra"] == pytest.approx(8.5)
            assert result["true_peak_dbtp"] == pytest.approx(-1.3)

    def test_parses_positive_values(self):
        stderr = (
            "  Integrated loudness:\n"
            "    I:         -8.0 LUFS\n"
            "    Threshold: -18.0 LUFS\n"
            "  Loudness range:\n"
            "    LRA:         12.0 LU\n"
            "    Threshold:   -28.0 LUFS\n"
            "  True peak:\n"
            "    Peak:         2.5 dBFS\n"
        )
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stderr = stderr
            mock_run.return_value = mock_result

            result = analyze_ebur128("test.flac", None)
            assert result is not None
            assert result["integrated_lufs"] == pytest.approx(-8.0)
            assert result["true_peak_dbtp"] == pytest.approx(2.5)

    def test_returns_none_on_failure(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = OSError
            result = analyze_ebur128("test.flac", None)
            assert result is None

    def test_returns_none_on_nonzero_returncode(self):
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 1
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = analyze_ebur128("test.flac", None)
            assert result is None

    def test_returns_none_on_missing_regex_match(self):
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stderr = "garbage output without proper values"
            mock_run.return_value = mock_result

            result = analyze_ebur128("test.flac", None)
            assert result is None

    def test_seeks_for_long_files(self):
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stderr = "    I:         -14.0 LUFS\n    LRA:         6.0 LU\n    Peak:         -1.0 dBFS\n"
            mock_run.return_value = mock_result

            result = analyze_ebur128("test.flac", 120.0)
            assert result is not None
            # Verify -ss and -t args were passed
            call_args = mock_run.call_args[0][0]
            assert "-ss" in call_args
            assert "-t" in call_args
            assert "60" in call_args

    def test_no_seek_for_short_files(self):
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stderr = "    I:         -14.0 LUFS\n    LRA:         6.0 LU\n    Peak:         -1.0 dBFS\n"
            mock_run.return_value = mock_result

            result = analyze_ebur128("test.flac", 30.0)
            assert result is not None
            call_args = mock_run.call_args[0][0]
            assert "-ss" not in call_args
