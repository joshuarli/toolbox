from __future__ import annotations

import unittest
from unittest.mock import patch

from tool import cli


class CliTests(unittest.TestCase):
    def test_help_names_sdqual_subcommand(self) -> None:
        text = cli.build_parser().format_help()
        self.assertIn("sdqual", text)

    @patch("tool.sdqual.run", return_value=7)
    def test_dispatches_sdqual(self, run) -> None:
        self.assertEqual(cli.main(["sdqual", "disk7"]), 7)
        run.assert_called_once()
        args = run.call_args.args[0]
        self.assertEqual(args.disk, "disk7")
        self.assertEqual(args.seconds, 30)
        self.assertFalse(args.verify_only)

    def test_sdqual_options_parse(self) -> None:
        args = cli.build_parser().parse_args(
            ["sdqual", "--seconds", "60", "--verify-only", "/dev/disk7"]
        )
        self.assertEqual(args.disk, "/dev/disk7")
        self.assertEqual(args.seconds, 60)
        self.assertTrue(args.verify_only)


if __name__ == "__main__":
    unittest.main()
