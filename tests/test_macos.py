from __future__ import annotations

import unittest
from unittest.mock import patch

from tool.sd import macos
from tool.sd.errors import SdError, UnsupportedPlatformError


class GateTests(unittest.TestCase):
    def test_is_macos_matches_sys_platform(self) -> None:
        with patch("tool.sd.macos.sys.platform", "darwin"):
            self.assertTrue(macos.is_macos())
        with patch("tool.sd.macos.sys.platform", "linux"):
            self.assertFalse(macos.is_macos())

    def test_require_macos_passes_on_darwin(self) -> None:
        with patch("tool.sd.macos.sys.platform", "darwin"):
            self.assertIsNone(macos.require_macos("test action"))

    def test_require_macos_fails_closed_elsewhere(self) -> None:
        with (
            patch("tool.sd.macos.sys.platform", "linux"),
            self.assertRaisesRegex(UnsupportedPlatformError, "macOS"),
        ):
            macos.require_macos("test action")

    def test_unsupported_platform_is_sd_error(self) -> None:
        self.assertTrue(issubclass(UnsupportedPlatformError, SdError))

    def test_helpers_fail_closed_off_darwin(self) -> None:
        with patch("tool.sd.macos.sys.platform", "linux"):
            self.assertRaises(UnsupportedPlatformError, macos.plist_command, ["diskutil"])
            self.assertRaises(UnsupportedPlatformError, macos.disk_info_plist, "disk7")
            self.assertRaises(UnsupportedPlatformError, macos.physical_list_plist)
            self.assertRaises(UnsupportedPlatformError, macos.root_info_plist)
            self.assertRaises(UnsupportedPlatformError, macos.apfs_list_plist)
            self.assertRaises(UnsupportedPlatformError, macos.validate_sudo)
            self.assertRaises(UnsupportedPlatformError, macos.sudo_prefix)
            self.assertRaises(UnsupportedPlatformError, macos.unmount_disk, "/dev/disk7")
            self.assertRaises(UnsupportedPlatformError, macos.start_caffeinate)


class ConstantsTests(unittest.TestCase):
    def test_tool_paths_are_owned_here(self) -> None:
        self.assertEqual(macos.DISKUTIL, "/usr/sbin/diskutil")
        self.assertEqual(macos.SUDO, "/usr/bin/sudo")
        self.assertEqual(macos.CAFFEINATE, "/usr/bin/caffeinate")


if __name__ == "__main__":
    unittest.main()
