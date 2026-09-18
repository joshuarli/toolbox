from __future__ import annotations

import contextlib
import io
import unittest
from unittest.mock import patch

from tool import cli
from tool.sd import preflight


def _preflight_info() -> preflight.PreflightInfo:
    return preflight.PreflightInfo(
        macos_version="26.5.2",
        macos_major=26,
        fio_path="/opt/homebrew/bin/fio",
        fio_version="fio-3.42",
        fio_version_tuple=(3, 42),
    )


class CliTests(unittest.TestCase):
    def test_help_names_sd_subcommand(self) -> None:
        text = cli.build_parser().format_help()
        self.assertIn("sd", text)

    @patch("tool.sd.run", return_value=7)
    @patch("tool.cli.preflight.run_preflight", return_value=_preflight_info())
    def test_dispatches_sd(self, mock_preflight, run) -> None:
        self.assertEqual(cli.main(["sd", "disk7"]), 7)
        run.assert_called_once()
        mock_preflight.assert_called_once_with()
        args = run.call_args.args[0]
        self.assertEqual(args.disk, "disk7")
        self.assertEqual(args.seconds, 30)
        self.assertFalse(args.verify_only)
        self.assertEqual(args.preflight.fio_version, "fio-3.42")

    def test_sd_options_parse(self) -> None:
        args = cli.build_parser().parse_args(
            ["sd", "--seconds", "60", "--verify-only", "/dev/disk7"]
        )
        self.assertEqual(args.disk, "/dev/disk7")
        self.assertEqual(args.seconds, 60)
        self.assertTrue(args.verify_only)

    def test_sd_disk_defaults_to_interactive(self) -> None:
        args = cli.build_parser().parse_args(["sd"])
        self.assertIsNone(args.disk)

    @patch("tool.sd.run", return_value=0)
    @patch(
        "tool.cli.preflight.run_preflight",
        side_effect=preflight.PreflightError("old macOS"),
    )
    def test_preflight_failure_returns_2(self, _preflight_mock, _run) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(cli.main(["sd", "disk7"]), 2)
        self.assertIn("ERROR", stderr.getvalue())

    @patch("tool.sd.run", side_effect=KeyboardInterrupt)
    @patch("tool.cli.preflight.run_preflight", return_value=_preflight_info())
    def test_keyboard_interrupt_returns_130(self, _preflight_mock, _run) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(cli.main(["sd", "disk7"]), 130)


if __name__ == "__main__":
    unittest.main()
