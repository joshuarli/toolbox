from __future__ import annotations

import argparse
import contextlib
import io
import unittest
from unittest.mock import patch

from tool import sdqual


class NormalizeDiskTests(unittest.TestCase):
    def test_accepts_whole_disk_forms(self) -> None:
        self.assertEqual(sdqual.normalize_disk("disk7"), "disk7")
        self.assertEqual(sdqual.normalize_disk("/dev/disk7"), "disk7")

    def test_rejects_partition(self) -> None:
        with self.assertRaisesRegex(ValueError, "whole disk"):
            sdqual.normalize_disk("/dev/disk7s1")

    def test_rejects_raw_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "diskN"):
            sdqual.normalize_disk("/dev/rdisk7")


class DiskInfoTests(unittest.TestCase):
    def test_parses_safe_removable_disk(self) -> None:
        info = sdqual.DiskInfo.from_plist(
            {
                "DeviceIdentifier": "disk7",
                "DeviceNode": "/dev/disk7",
                "WholeDisk": True,
                "Writable": True,
                "WritableMedia": True,
                "Internal": True,
                "Ejectable": True,
                "Removable": True,
                "RemovableMedia": True,
                "RemovableMediaOrExternalDevice": True,
                "BusProtocol": "Secure Digital",
                "MediaName": "SD Card Reader Media",
                "MediaType": "Generic",
                "TotalSize": 64_000_000_000,
                "DeviceBlockSize": 512,
            }
        )
        self.assertEqual(info.identifier, "disk7")
        self.assertTrue(info.is_removable_or_external)
        self.assertEqual(info.raw_node, "/dev/rdisk7")

    def test_rejects_missing_identifier(self) -> None:
        with self.assertRaisesRegex(ValueError, "DeviceIdentifier"):
            sdqual.DiskInfo.from_plist({"WholeDisk": True})


class SafetyTests(unittest.TestCase):
    def make_info(self, **overrides: object) -> sdqual.DiskInfo:
        values: dict[str, object] = {
            "DeviceIdentifier": "disk7",
            "DeviceNode": "/dev/disk7",
            "WholeDisk": True,
            "Writable": True,
            "WritableMedia": True,
            "Internal": False,
            "Ejectable": True,
            "Removable": True,
            "RemovableMedia": True,
            "RemovableMediaOrExternalDevice": True,
            "BusProtocol": "USB",
            "MediaName": "Reader Media",
            "MediaType": "Generic",
            "TotalSize": 64_000_000_000,
            "DeviceBlockSize": 512,
        }
        values.update(overrides)
        return sdqual.DiskInfo.from_plist(values)

    def test_safe_physical_removable_disk_passes(self) -> None:
        sdqual.validate_target(self.make_info(), {"disk7"})

    def test_external_fixed_media_is_allowed(self) -> None:
        info = self.make_info(
            Internal=False,
            Ejectable=False,
            Removable=False,
            RemovableMedia=False,
            RemovableMediaOrExternalDevice=False,
        )
        sdqual.validate_target(info, {"disk7"})

    def test_internal_nonremovable_disk_is_refused(self) -> None:
        info = self.make_info(
            Internal=True,
            Ejectable=False,
            Removable=False,
            RemovableMedia=False,
            RemovableMediaOrExternalDevice=False,
        )
        with self.assertRaisesRegex(sdqual.SafetyError, "removable/external"):
            sdqual.validate_target(info, {"disk7"})

    def test_nonphysical_disk_is_refused(self) -> None:
        with self.assertRaisesRegex(sdqual.SafetyError, "physical"):
            sdqual.validate_target(self.make_info(), {"disk0"})

    def test_partition_like_plist_is_refused(self) -> None:
        with self.assertRaisesRegex(sdqual.SafetyError, "whole disk"):
            sdqual.validate_target(self.make_info(WholeDisk=False), {"disk7"})

    def test_read_only_disk_is_refused(self) -> None:
        with self.assertRaisesRegex(sdqual.SafetyError, "writable"):
            sdqual.validate_target(self.make_info(Writable=False), {"disk7"})

    def test_root_backing_disk_is_refused(self) -> None:
        with self.assertRaisesRegex(sdqual.SafetyError, "running macOS"):
            sdqual.validate_candidate(self.make_info(), {"disk7"}, {"disk7"})


class RootBackingDiskTests(unittest.TestCase):
    def test_resolves_apfs_root_to_physical_whole_disk(self) -> None:
        root = {
            "DeviceIdentifier": "disk9s1s1",
            "ParentWholeDisk": "disk9",
            "APFSContainerReference": "disk9",
        }
        apfs = {
            "Containers": [
                {
                    "ContainerReference": "disk9",
                    "PhysicalStores": [
                        {"DeviceIdentifier": "disk7s2", "Size": 1_000_000}
                    ],
                    "Volumes": [{"DeviceIdentifier": "disk9s1"}],
                }
            ]
        }
        self.assertEqual(
            sdqual.root_backing_disks_from_plists(root, apfs),
            {"disk7"},
        )

    def test_non_apfs_root_uses_parent_whole_disk(self) -> None:
        root = {"DeviceIdentifier": "disk5s2", "ParentWholeDisk": "disk5"}
        self.assertEqual(
            sdqual.root_backing_disks_from_plists(root, None),
            {"disk5"},
        )

    def test_apfs_root_without_physical_stores_fails_closed(self) -> None:
        root = {
            "DeviceIdentifier": "disk9s1",
            "APFSContainerReference": "disk9",
        }
        apfs = {
            "Containers": [
                {
                    "ContainerReference": "disk9",
                    "PhysicalStores": [],
                    "Volumes": [{"DeviceIdentifier": "disk9s1"}],
                }
            ]
        }
        self.assertEqual(sdqual.root_backing_disks_from_plists(root, apfs), set())


class ConfirmationTests(unittest.TestCase):
    def test_normal_mode_requires_exact_erase_token(self) -> None:
        seen: list[str] = []
        sdqual.confirm_target(
            "disk7",
            verify_only=False,
            input_fn=lambda prompt: seen.append(prompt) or "ERASE disk7",
        )
        self.assertIn("ERASE disk7", seen[0])

    def test_verify_mode_requires_exact_verify_token(self) -> None:
        sdqual.confirm_target(
            "disk7",
            verify_only=True,
            input_fn=lambda prompt: "VERIFY disk7",
        )

    def test_confirmation_mismatch_is_error(self) -> None:
        with self.assertRaisesRegex(sdqual.SdqualError, "confirmation"):
            sdqual.confirm_target(
                "disk7",
                verify_only=False,
                input_fn=lambda prompt: "no",
            )


class FioCommandTests(unittest.TestCase):
    def test_benchmark_commands_have_expected_shapes(self) -> None:
        tests = {test.name: test for test in sdqual.benchmark_tests(30)}
        self.assertEqual(
            set(tests),
            {
                "seq-read-1m",
                "seq-write-1m",
                "rand-read-4k",
                "rand-write-4k",
                "rand-70r30w-4k",
            },
        )

        seq_read = tests["seq-read-1m"].argv("fio", "/dev/rdisk7")
        self.assertIn("--filename=/dev/rdisk7", seq_read)
        self.assertIn("--rw=read", seq_read)
        self.assertIn("--bs=1m", seq_read)
        self.assertIn("--readonly=1", seq_read)
        self.assertIn("--time_based=1", seq_read)
        self.assertIn("--runtime=30", seq_read)

        seq_write = tests["seq-write-1m"].argv("fio", "/dev/rdisk7")
        self.assertIn("--rw=write", seq_write)
        self.assertIn("--end_fsync=1", seq_write)

        rand_read = tests["rand-read-4k"].argv("fio", "/dev/rdisk7")
        self.assertIn("--rw=randread", rand_read)
        self.assertIn("--bs=4k", rand_read)
        self.assertIn("--norandommap=1", rand_read)
        self.assertIn("--readonly=1", rand_read)

        rand_write = tests["rand-write-4k"].argv("fio", "/dev/rdisk7")
        self.assertIn("--rw=randwrite", rand_write)
        self.assertIn("--end_fsync=1", rand_write)

        mixed = tests["rand-70r30w-4k"].argv("fio", "/dev/rdisk7")
        self.assertIn("--rw=randrw", mixed)
        self.assertIn("--rwmixread=70", mixed)

        for argv in (seq_read, seq_write, rand_read, rand_write, mixed):
            self.assertIn("--ioengine=psync", argv)
            self.assertIn("--direct=1", argv)
            self.assertIn("--iodepth=1", argv)

    def test_integrity_command_is_unbounded_full_write_then_verify(self) -> None:
        argv = sdqual.FioTest.integrity().argv("fio", "/dev/rdisk7")
        self.assertIn("--rw=write", argv)
        self.assertIn("--verify=crc32c", argv)
        self.assertIn("--do_verify=1", argv)
        self.assertIn("--verify_fatal=0", argv)
        self.assertIn("--end_fsync=1", argv)
        self.assertNotIn("--time_based=1", argv)
        self.assertFalse(any(x.startswith("--runtime=") for x in argv))

    def test_verify_only_command_has_no_workload_runtime(self) -> None:
        argv = sdqual.FioTest.verify_only().argv("fio", "/dev/rdisk7")
        self.assertIn("--verify_only=1", argv)
        self.assertIn("--verify=crc32c", argv)
        self.assertNotIn("--do_verify=1", argv)
        self.assertNotIn("--time_based=1", argv)


class RunModeTests(unittest.TestCase):
    def args(
        self,
        *,
        verify_only: bool = False,
        seconds: int = 30,
    ) -> argparse.Namespace:
        return argparse.Namespace(
            disk="disk7",
            verify_only=verify_only,
            seconds=seconds,
        )

    @patch("tool.sdqual.execute_suite")
    @patch("tool.sdqual.prepare_target")
    @patch(
        "tool.sdqual.require_fio",
        return_value=("/opt/homebrew/bin/fio", "fio-3.42"),
    )
    @patch("tool.sdqual.platform.system", return_value="Darwin")
    def test_run_passes_verify_only_to_suite(
        self,
        _system,
        _fio,
        prepare_target,
        execute_suite,
    ) -> None:
        info = SafetyTests().make_info()
        prepare_target.return_value = info
        execute_suite.return_value = [
            sdqual.TestResult("integrity-verify-only", 0, 1.0)
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            result = sdqual.run(self.args(verify_only=True))
        self.assertEqual(result, 0)
        prepare_target.assert_called_once_with("disk7", verify_only=True)
        execute_suite.assert_called_once_with(
            info,
            "/opt/homebrew/bin/fio",
            seconds=30,
            verify_only=True,
        )

    @patch("tool.sdqual.execute_suite")
    @patch("tool.sdqual.prepare_target")
    @patch(
        "tool.sdqual.require_fio",
        return_value=("/opt/homebrew/bin/fio", "fio-3.42"),
    )
    @patch("tool.sdqual.platform.system", return_value="Darwin")
    def test_run_returns_failure_if_any_fio_test_fails(
        self,
        _system,
        _fio,
        prepare_target,
        execute_suite,
    ) -> None:
        info = SafetyTests().make_info()
        prepare_target.return_value = info
        execute_suite.return_value = [sdqual.TestResult("seq-write-1m", 1, 1.0)]
        with contextlib.redirect_stdout(io.StringIO()):
            result = sdqual.run(self.args())
        self.assertEqual(result, 1)


if __name__ == "__main__":
    unittest.main()
