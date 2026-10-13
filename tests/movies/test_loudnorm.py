import json
import subprocess
from pathlib import Path

import pytest

from tool.movies import loudnorm, probe


@pytest.mark.parametrize(
    "result",
    [
        subprocess.CompletedProcess([], 1, "", "ffmpeg could not decode audio"),
        subprocess.CompletedProcess([], 0, "", "no measurement JSON"),
        subprocess.CompletedProcess([], 0, "", '{"input_i": "-18.0"}'),
        subprocess.CompletedProcess([], 0, "", '{"input_i": "-18.0", "target_offset": "nan"}'),
    ],
)
def test_measure_gain_rejects_failed_or_invalid_measurement(monkeypatch, tmp_path: Path, result) -> None:
    monkeypatch.setattr(loudnorm.subprocess, "run", lambda *_args, **_kwargs: result)

    with pytest.raises(loudnorm.LoudnormMeasurementError):
        loudnorm.measure_gain(tmp_path / "source.mkv")


def test_measure_gain_reports_timeout(monkeypatch, tmp_path: Path) -> None:
    def timeout(cmd, **_kwargs):
        raise subprocess.TimeoutExpired(cmd, timeout=1)

    monkeypatch.setattr(loudnorm.subprocess, "run", timeout)

    with pytest.raises(loudnorm.LoudnormMeasurementError, match="timed out"):
        loudnorm.measure_gain(tmp_path / "source.mkv")


def test_measure_gain_returns_values_from_valid_json(monkeypatch, tmp_path: Path) -> None:
    measurement = json.dumps({"input_i": "-20.0", "target_offset": "4.0"})
    result = subprocess.CompletedProcess([], 0, "", f"metadata\n{measurement}\n")
    monkeypatch.setattr(loudnorm.subprocess, "run", lambda *_args, **_kwargs: result)

    assert loudnorm.measure_gain(tmp_path / "source.mkv") == (4.0, -20.0)


def test_normalize_surfaces_measurement_failure_to_cli(monkeypatch, tmp_path: Path, capsys) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"source")
    info = probe.ProbeInfo(streams=[], format=probe.ProbeFormat(duration=1.0))
    monkeypatch.setattr(loudnorm.probe, "ffprobe", lambda _source: info)
    result = subprocess.CompletedProcess([], 1, "", "ffmpeg could not decode audio")
    monkeypatch.setattr(loudnorm.subprocess, "run", lambda *_args, **_kwargs: result)

    with pytest.raises(SystemExit) as exc:
        loudnorm.normalize(source)

    assert exc.value.code != 0
    captured = capsys.readouterr()
    assert "loudness measurement failed" in str(exc.value)
    assert "Already at target loudness" not in captured.out
