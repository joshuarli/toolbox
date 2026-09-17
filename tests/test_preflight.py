from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from tool import preflight


class ParseMacosMajorTests(unittest.TestCase):
    def test_parses_dotted_version(self) -> None:
        self.assertEqual(preflight.parse_macos_major("26.5.2"), 26)

    def test_parses_bare_major(self) -> None:
        self.assertEqual(preflight.parse_macos_major("26"), 26)

    def test_rejects_garbage(self) -> None:
        with self.assertRaises(preflight.PreflightError):
            preflight.parse_macos_major("not-a-version")

    def test_rejects_empty(self) -> None:
        with self.assertRaises(preflight.PreflightError):
            preflight.parse_macos_major("")


class CheckPlatformTests(unittest.TestCase):
    def test_accepts_macos_26(self) -> None:
        version, major = preflight.check_platform(system="Darwin", mac_version="26.0")
        self.assertEqual((version, major), ("26.0", 26))

    def test_accepts_later_macos(self) -> None:
        _, major = preflight.check_platform(system="Darwin", mac_version="27.0.1")
        self.assertEqual(major, 27)

    def test_rejects_older_macos(self) -> None:
        with self.assertRaisesRegex(preflight.PreflightError, "26\\+"):
            preflight.check_platform(system="Darwin", mac_version="15.6")

    def test_rejects_non_darwin(self) -> None:
        with self.assertRaisesRegex(preflight.PreflightError, "macOS"):
            preflight.check_platform(system="Linux", mac_version="26.0")

    def test_rejects_unknown_version(self) -> None:
        with self.assertRaises(preflight.PreflightError):
            preflight.check_platform(system="Darwin", mac_version="")


class ParseFioVersionTests(unittest.TestCase):
    def test_parses_plain_version(self) -> None:
        self.assertEqual(preflight.parse_fio_version("fio-3.42"), (3, 42))

    def test_parses_newer_version(self) -> None:
        self.assertEqual(preflight.parse_fio_version("fio-3.44"), (3, 44))

    def test_parses_patch_version(self) -> None:
        self.assertEqual(preflight.parse_fio_version("fio-3.42.1"), (3, 42, 1))

    def test_rejects_garbage(self) -> None:
        with self.assertRaises(preflight.PreflightError):
            preflight.parse_fio_version("no-version-here")


def _completed(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["fio", "--version"], returncode=0, stdout=stdout, stderr=""
    )


class CheckFioTests(unittest.TestCase):
    @patch("tool.preflight.shutil.which", return_value=None)
    def test_missing_fio_is_error(self, _which) -> None:
        with self.assertRaisesRegex(preflight.PreflightError, "brew install fio"):
            preflight.check_fio()

    @patch(
        "tool.preflight.subprocess.run",
        return_value=_completed("fio-3.42\n"),
    )
    @patch("tool.preflight.shutil.which", return_value="/opt/homebrew/bin/fio")
    def test_accepts_minimum_version(self, _which, _run) -> None:
        path, version, parsed = preflight.check_fio()
        self.assertEqual(path, "/opt/homebrew/bin/fio")
        self.assertEqual(version, "fio-3.42")
        self.assertEqual(parsed, (3, 42))

    @patch(
        "tool.preflight.subprocess.run",
        return_value=_completed("fio-3.44\n"),
    )
    @patch("tool.preflight.shutil.which", return_value="/opt/homebrew/bin/fio")
    def test_accepts_newer_version(self, _which, _run) -> None:
        _, _, parsed = preflight.check_fio()
        self.assertEqual(parsed, (3, 44))

    @patch(
        "tool.preflight.subprocess.run",
        return_value=_completed("fio-3.41\n"),
    )
    @patch("tool.preflight.shutil.which", return_value="/opt/homebrew/bin/fio")
    def test_rejects_old_version(self, _which, _run) -> None:
        with self.assertRaisesRegex(preflight.PreflightError, ">= 3.42"):
            preflight.check_fio()

    @patch(
        "tool.preflight.subprocess.run",
        return_value=_completed("\n"),
    )
    @patch("tool.preflight.shutil.which", return_value="/opt/homebrew/bin/fio")
    def test_rejects_empty_version(self, _which, _run) -> None:
        with self.assertRaisesRegex(preflight.PreflightError, "empty"):
            preflight.check_fio()

    @patch(
        "tool.preflight.subprocess.run",
        side_effect=OSError("noexec"),
    )
    @patch("tool.preflight.shutil.which", return_value="/opt/homebrew/bin/fio")
    def test_unrunnable_fio_is_error(self, _which, _run) -> None:
        with self.assertRaises(preflight.PreflightError):
            preflight.check_fio()


class RunPreflightTests(unittest.TestCase):
    @patch(
        "tool.preflight.check_fio",
        return_value=("/opt/homebrew/bin/fio", "fio-3.42", (3, 42)),
    )
    @patch("tool.preflight.check_platform", return_value=("26.5.2", 26))
    def test_returns_combined_info(self, _platform, _fio) -> None:
        info = preflight.run_preflight()
        self.assertEqual(info.macos_version, "26.5.2")
        self.assertEqual(info.macos_major, 26)
        self.assertEqual(info.fio_path, "/opt/homebrew/bin/fio")
        self.assertEqual(info.fio_version, "fio-3.42")
        self.assertEqual(info.fio_version_tuple, (3, 42))


if __name__ == "__main__":
    unittest.main()
