from __future__ import annotations

import argparse
import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tool import preflight, sd


def _preflight_info() -> preflight.PreflightInfo:
    return preflight.PreflightInfo(
        macos_version="26.5.2",
        macos_major=26,
        fio_path="/opt/homebrew/bin/fio",
        fio_version="fio-3.42",
        fio_version_tuple=(3, 42),
    )


class NormalizeDiskTests(unittest.TestCase):
    def test_accepts_whole_disk_forms(self) -> None:
        self.assertEqual(sd.normalize_disk("disk7"), "disk7")
        self.assertEqual(sd.normalize_disk("/dev/disk7"), "disk7")

    def test_rejects_partition(self) -> None:
        with self.assertRaisesRegex(ValueError, "whole disk"):
            sd.normalize_disk("/dev/disk7s1")

    def test_rejects_raw_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "diskN"):
            sd.normalize_disk("/dev/rdisk7")


class DiskInfoTests(unittest.TestCase):
    def test_parses_safe_removable_disk(self) -> None:
        info = sd.DiskInfo.from_plist(
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
            sd.DiskInfo.from_plist({"WholeDisk": True})


class SafetyTests(unittest.TestCase):
    def make_info(self, **overrides: object) -> sd.DiskInfo:
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
        return sd.DiskInfo.from_plist(values)

    def test_safe_physical_removable_disk_passes(self) -> None:
        sd.validate_target(self.make_info(), {"disk7"})

    def test_external_fixed_media_is_allowed(self) -> None:
        info = self.make_info(
            Internal=False,
            Ejectable=False,
            Removable=False,
            RemovableMedia=False,
            RemovableMediaOrExternalDevice=False,
        )
        sd.validate_target(info, {"disk7"})

    def test_internal_nonremovable_disk_is_refused(self) -> None:
        info = self.make_info(
            Internal=True,
            Ejectable=False,
            Removable=False,
            RemovableMedia=False,
            RemovableMediaOrExternalDevice=False,
        )
        with self.assertRaisesRegex(sd.SafetyError, "removable/external"):
            sd.validate_target(info, {"disk7"})

    def test_nonphysical_disk_is_refused(self) -> None:
        with self.assertRaisesRegex(sd.SafetyError, "physical"):
            sd.validate_target(self.make_info(), {"disk0"})

    def test_partition_like_plist_is_refused(self) -> None:
        with self.assertRaisesRegex(sd.SafetyError, "whole disk"):
            sd.validate_target(self.make_info(WholeDisk=False), {"disk7"})

    def test_read_only_disk_is_refused(self) -> None:
        with self.assertRaisesRegex(sd.SafetyError, "writable"):
            sd.validate_target(self.make_info(Writable=False), {"disk7"})

    def test_root_backing_disk_is_refused(self) -> None:
        with self.assertRaisesRegex(sd.SafetyError, "running macOS"):
            sd.validate_candidate(self.make_info(), {"disk7"}, {"disk7"})


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
            sd.root_backing_disks_from_plists(root, apfs),
            {"disk7"},
        )

    def test_non_apfs_root_uses_parent_whole_disk(self) -> None:
        root = {"DeviceIdentifier": "disk5s2", "ParentWholeDisk": "disk5"}
        self.assertEqual(
            sd.root_backing_disks_from_plists(root, None),
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
        self.assertEqual(sd.root_backing_disks_from_plists(root, apfs), set())


class ConfirmationTests(unittest.TestCase):
    def test_normal_mode_requires_exact_erase_token(self) -> None:
        seen: list[str] = []
        sd.confirm_target(
            "disk7",
            verify_only=False,
            input_fn=lambda prompt: seen.append(prompt) or "ERASE disk7",
        )
        self.assertIn("ERASE disk7", seen[0])

    def test_verify_mode_requires_exact_verify_token(self) -> None:
        sd.confirm_target(
            "disk7",
            verify_only=True,
            input_fn=lambda prompt: "VERIFY disk7",
        )

    def test_confirmation_mismatch_is_error(self) -> None:
        with self.assertRaisesRegex(sd.SdError, "confirmation"):
            sd.confirm_target(
                "disk7",
                verify_only=False,
                input_fn=lambda prompt: "no",
            )


class FioCommandTests(unittest.TestCase):
    def test_benchmark_commands_have_expected_shapes(self) -> None:
        tests = {test.name: test for test in sd.benchmark_tests(30)}
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
        self.assertIn("--readonly", seq_read)
        self.assertNotIn("--readonly=1", seq_read)
        self.assertIn("--time_based=1", seq_read)
        self.assertIn("--runtime=30", seq_read)

        seq_write = tests["seq-write-1m"].argv("fio", "/dev/rdisk7")
        self.assertIn("--rw=write", seq_write)
        self.assertIn("--end_fsync=1", seq_write)

        rand_read = tests["rand-read-4k"].argv("fio", "/dev/rdisk7")
        self.assertIn("--rw=randread", rand_read)
        self.assertIn("--bs=4k", rand_read)
        self.assertIn("--norandommap=1", rand_read)
        self.assertIn("--readonly", rand_read)
        self.assertNotIn("--readonly=1", rand_read)

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
        argv = sd.FioTest.integrity().argv("fio", "/dev/rdisk7")
        self.assertIn("--rw=write", argv)
        self.assertIn("--verify=crc32c", argv)
        self.assertIn("--do_verify=1", argv)
        self.assertIn("--verify_fatal=0", argv)
        self.assertIn("--end_fsync=1", argv)
        self.assertNotIn("--time_based=1", argv)
        self.assertFalse(any(x.startswith("--runtime=") for x in argv))

    def test_verify_only_command_has_no_workload_runtime(self) -> None:
        argv = sd.FioTest.verify_only().argv("fio", "/dev/rdisk7")
        self.assertIn("--verify_only=1", argv)
        self.assertIn("--verify=crc32c", argv)
        self.assertNotIn("--do_verify=1", argv)
        self.assertNotIn("--time_based=1", argv)

    def test_seq_read_argv_is_pinned(self) -> None:
        test = sd.FioTest(
            "seq-read-1m",
            "read",
            "1m",
            runtime_seconds=30,
            ramp_seconds=3,
            read_only=True,
        )
        self.assertEqual(
            test.argv("fio", "/dev/rdisk7"),
            [
                "fio",
                "--name=seq-read-1m",
                "--filename=/dev/rdisk7",
                "--ioengine=psync",
                "--direct=1",
                "--iodepth=1",
                "--group_reporting=1",
                "--output-format=normal",
                "--eta=always",
                "--eta-interval=5",
                "--rw=read",
                "--bs=1m",
                "--time_based=1",
                "--runtime=30",
                "--ramp_time=3",
                "--readonly",
            ],
        )

    def test_integrity_argv_is_pinned(self) -> None:
        self.assertEqual(
            sd.FioTest.integrity().argv("fio", "/dev/rdisk7"),
            [
                "fio",
                "--name=full-device-integrity",
                "--filename=/dev/rdisk7",
                "--ioengine=psync",
                "--direct=1",
                "--iodepth=1",
                "--group_reporting=1",
                "--output-format=normal",
                "--eta=always",
                "--eta-interval=5",
                "--rw=write",
                "--bs=1m",
                "--end_fsync=1",
                "--verify=crc32c",
                "--do_verify=1",
                "--verify_fatal=0",
            ],
        )


def _candidate(identifier: str = "disk7") -> sd.CandidateDisk:
    return sd.CandidateDisk(
        identifier=identifier,
        device_node=f"/dev/{identifier}",
        media_name="Reader Media",
        total_size=64_000_000_000,
        detail="Secure Digital",
    )


class PromptTests(unittest.TestCase):
    def test_selects_numbered_choice(self) -> None:
        candidates = [_candidate("disk4"), _candidate("disk6")]
        shown: list[str] = []
        selected = sd.prompt_for_candidate(
            candidates,
            input_fn=lambda prompt: "2",
            output_fn=shown.append,
        )
        self.assertEqual(selected.identifier, "disk6")
        self.assertTrue(any("1)" in line for line in shown))
        self.assertTrue(any("2)" in line for line in shown))

    def test_reprompts_after_invalid_then_selects(self) -> None:
        candidates = [_candidate("disk4"), _candidate("disk6")]
        shown: list[str] = []
        answers = iter(["0", "bogus", "1"])
        selected = sd.prompt_for_candidate(
            candidates,
            input_fn=lambda prompt: next(answers),
            output_fn=shown.append,
        )
        self.assertEqual(selected.identifier, "disk4")
        self.assertTrue(any("Enter a number" in line for line in shown))

    def test_quit_is_cancelled(self) -> None:
        with self.assertRaisesRegex(sd.SdError, "cancelled"):
            sd.prompt_for_candidate(
                [_candidate()],
                input_fn=lambda prompt: "q",
                output_fn=lambda line: None,
            )

    def test_empty_candidates_is_error(self) -> None:
        with self.assertRaisesRegex(sd.SdError, "no suitable"):
            sd.prompt_for_candidate(
                [],
                input_fn=lambda prompt: "1",
                output_fn=lambda line: None,
            )

    def test_eof_is_error(self) -> None:
        def _eof(prompt: str) -> str:
            raise EOFError

        with self.assertRaisesRegex(sd.SdError, "no disk selected"):
            sd.prompt_for_candidate(
                [_candidate()],
                input_fn=_eof,
                output_fn=lambda line: None,
            )


class ResolveInteractiveDiskTests(unittest.TestCase):
    @patch("tool.sd.discover_candidates", return_value=[_candidate("disk4")])
    @patch("tool.sd.sys.stdin")
    def test_returns_selected_identifier(
        self, mock_stdin: MagicMock, _discover: MagicMock
    ) -> None:
        mock_stdin.isatty.return_value = True
        result = sd.resolve_interactive_disk(
            input_fn=lambda prompt: "1",
            output_fn=lambda line: None,
        )
        self.assertEqual(result, "disk4")

    @patch("tool.sd.sys.stdin")
    def test_non_tty_is_error(self, mock_stdin: MagicMock) -> None:
        mock_stdin.isatty.return_value = False
        with self.assertRaisesRegex(sd.SdError, "not interactive"):
            sd.resolve_interactive_disk(
                input_fn=lambda prompt: "1",
                output_fn=lambda line: None,
            )


class DiscoverCandidatesTests(unittest.TestCase):
    def test_unixy_lists_whole_disks_only(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            dev_dir = Path(tmp)
            for name in ("sda", "sda1", "mmcblk0", "mmcblk0p1", "nvme0n1"):
                (dev_dir / name).touch()
            (dev_dir / "tty").touch()
            candidates = sd.discover_unixy_candidates(dev_dir)
            self.assertEqual(
                [c.identifier for c in candidates],
                ["mmcblk0", "nvme0n1", "sda"],
            )

    def test_unixy_missing_dir_is_error(self) -> None:
        with self.assertRaises(sd.SdError):
            sd.discover_unixy_candidates(Path("/nonexistent-tool-test-dir"))

    @patch("tool.sd.discover_unixy_candidates")
    @patch("tool.sd.discover_darwin_candidates")
    def test_dispatches_on_platform(
        self, mock_darwin: MagicMock, mock_unixy: MagicMock
    ) -> None:
        mock_darwin.return_value = [_candidate("disk4")]
        mock_unixy.return_value = [_candidate("sda")]
        with patch("tool.sd.macos.is_macos", return_value=True):
            self.assertEqual(
                [c.identifier for c in sd.discover_candidates()], ["disk4"]
            )
        with patch("tool.sd.macos.is_macos", return_value=False):
            self.assertEqual([c.identifier for c in sd.discover_candidates()], ["sda"])


class DiscoverDarwinTests(unittest.TestCase):
    def _plist(self, identifier: str, **overrides: object) -> dict[str, object]:
        values: dict[str, object] = {
            "DeviceIdentifier": identifier,
            "DeviceNode": f"/dev/{identifier}",
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
        return values

    def _fixed_internal_plist(self, identifier: str) -> dict[str, object]:
        return self._plist(
            identifier,
            Internal=True,
            Ejectable=False,
            Removable=False,
            RemovableMedia=False,
            RemovableMediaOrExternalDevice=False,
        )

    def _backend(self, mock_macos: MagicMock, *, root_fails: bool = False) -> None:
        mock_macos.physical_list_plist.return_value = {"WholeDisks": ["disk0", "disk7"]}
        if root_fails:
            mock_macos.root_info_plist.side_effect = sd.SdError("no root")
        else:
            mock_macos.root_info_plist.return_value = {
                "DeviceIdentifier": "disk0s1",
                "ParentWholeDisk": "disk0",
            }

        def _info(target: str) -> dict[str, object]:
            if target == "disk7":
                return self._plist(target)
            return self._fixed_internal_plist(target)

        mock_macos.disk_info_plist.side_effect = _info

    @patch("tool.sd.Path")
    @patch("tool.sd.macos")
    def test_lists_only_eligible_disks(
        self, mock_macos: MagicMock, mock_path: MagicMock
    ) -> None:
        self._backend(mock_macos)
        raw = mock_path.return_value
        raw.exists.return_value = True
        raw.is_char_device.return_value = True
        candidates = sd.discover_darwin_candidates()
        self.assertEqual([c.identifier for c in candidates], ["disk7"])
        self.assertEqual(candidates[0].device_node, "/dev/disk7")

    @patch("tool.sd.Path")
    @patch("tool.sd.macos")
    def test_missing_raw_device_filters_candidate(
        self, mock_macos: MagicMock, mock_path: MagicMock
    ) -> None:
        self._backend(mock_macos)
        raw = mock_path.return_value
        raw.exists.return_value = True
        raw.is_char_device.return_value = False
        self.assertEqual(sd.discover_darwin_candidates(), [])

    @patch("tool.sd.Path")
    @patch("tool.sd.macos")
    def test_unresolvable_root_still_lists_removable(
        self, mock_macos: MagicMock, mock_path: MagicMock
    ) -> None:
        self._backend(mock_macos, root_fails=True)
        raw = mock_path.return_value
        raw.exists.return_value = True
        raw.is_char_device.return_value = True
        candidates = sd.discover_darwin_candidates()
        self.assertEqual([c.identifier for c in candidates], ["disk7"])


class RunTestSudoTests(unittest.TestCase):
    @patch("tool.sd.macos")
    @patch("tool.sd.subprocess.run")
    def test_use_sudo_prefixes_with_sudo(
        self, mock_run: MagicMock, mock_macos: MagicMock
    ) -> None:
        mock_macos.sudo_prefix.return_value = ["/usr/bin/sudo", "-n"]
        mock_run.return_value = MagicMock(returncode=0)
        with contextlib.redirect_stdout(io.StringIO()):
            result = sd.run_test(
                sd.FioTest("probe", "read", "4k"), "/bin/fio", "/dev/rdisk7"
            )
        command = mock_run.call_args.args[0]
        self.assertEqual(command[:2], ["/usr/bin/sudo", "-n"])
        self.assertIn("--filename=/dev/rdisk7", command)
        mock_macos.validate_sudo.assert_called_once_with()
        self.assertTrue(result.passed)

    @patch("tool.sd.macos")
    @patch("tool.sd.subprocess.run")
    def test_no_sudo_runs_fio_directly(
        self, mock_run: MagicMock, mock_macos: MagicMock
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        with contextlib.redirect_stdout(io.StringIO()):
            result = sd.run_test(
                sd.FioTest("probe", "read", "4k"),
                "/bin/fio",
                "/dev/rdisk7",
                use_sudo=False,
            )
        command = mock_run.call_args.args[0]
        self.assertEqual(command[0], "/bin/fio")
        mock_macos.validate_sudo.assert_not_called()
        self.assertTrue(result.passed)

    @patch("tool.sd.macos")
    @patch("tool.sd.subprocess.run")
    def test_nonzero_exit_is_failure(
        self, mock_run: MagicMock, _macos: MagicMock
    ) -> None:
        mock_run.return_value = MagicMock(returncode=3)
        with contextlib.redirect_stdout(io.StringIO()):
            result = sd.run_test(
                sd.FioTest("probe", "read", "4k"),
                "/bin/fio",
                "/dev/rdisk7",
                use_sudo=False,
            )
        self.assertFalse(result.passed)
        self.assertEqual(result.returncode, 3)


class ExecuteSuiteTests(unittest.TestCase):
    def _passed(self, test, fio: str, raw: str, **kwargs) -> sd.TestResult:
        return sd.TestResult(test.name, 0, 0.1)

    @patch("tool.sd.macos")
    @patch("tool.sd.run_test")
    def test_skips_caffeinate_off_darwin(
        self, mock_run_test: MagicMock, mock_macos: MagicMock
    ) -> None:
        mock_macos.is_macos.return_value = False
        mock_run_test.side_effect = self._passed
        info = SafetyTests().make_info()
        with contextlib.redirect_stdout(io.StringIO()):
            results = sd.execute_suite(
                info, "/bin/fio", seconds=30, verify_only=False, use_sudo=False
            )
        self.assertEqual(len(results), 6)
        self.assertTrue(all(result.passed for result in results))
        mock_macos.start_caffeinate.assert_not_called()

    @patch("tool.sd.run_test")
    @patch("tool.sd.macos")
    def test_uses_caffeinate_on_darwin(
        self, mock_macos: MagicMock, mock_run_test: MagicMock
    ) -> None:
        proc = MagicMock()
        mock_macos.is_macos.return_value = True
        mock_macos.start_caffeinate.return_value = proc
        mock_run_test.side_effect = self._passed
        info = SafetyTests().make_info()
        with contextlib.redirect_stdout(io.StringIO()):
            results = sd.execute_suite(
                info, "/bin/fio", seconds=30, verify_only=True, use_sudo=False
            )
        self.assertEqual(len(results), 1)
        mock_macos.start_caffeinate.assert_called_once_with()
        proc.terminate.assert_called_once_with()


class ParserTests(unittest.TestCase):
    def test_disk_is_optional_for_interactive(self) -> None:
        from tool import cli

        args = cli.build_parser().parse_args(["sd"])
        self.assertIsNone(args.disk)


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
            preflight=_preflight_info(),
        )

    @patch("tool.sd.execute_suite")
    @patch("tool.sd.prepare_target")
    def test_run_passes_verify_only_to_suite(
        self,
        prepare_target,
        execute_suite,
    ) -> None:
        info = SafetyTests().make_info()
        prepare_target.return_value = info
        execute_suite.return_value = [sd.TestResult("integrity-verify-only", 0, 1.0)]
        with contextlib.redirect_stdout(io.StringIO()):
            result = sd.run(self.args(verify_only=True))
        self.assertEqual(result, 0)
        prepare_target.assert_called_once_with("disk7", verify_only=True)
        execute_suite.assert_called_once_with(
            info,
            "/opt/homebrew/bin/fio",
            seconds=30,
            verify_only=True,
        )

    @patch("tool.sd.execute_suite")
    @patch("tool.sd.prepare_target")
    def test_run_returns_failure_if_any_fio_test_fails(
        self,
        prepare_target,
        execute_suite,
    ) -> None:
        info = SafetyTests().make_info()
        prepare_target.return_value = info
        execute_suite.return_value = [sd.TestResult("seq-write-1m", 1, 1.0)]
        with contextlib.redirect_stdout(io.StringIO()):
            result = sd.run(self.args())
        self.assertEqual(result, 1)

    @patch("tool.sd.execute_suite")
    @patch("tool.sd.prepare_target")
    @patch("tool.sd.resolve_interactive_disk", return_value="disk4")
    def test_run_resolves_disk_interactively_when_omitted(
        self,
        mock_resolve,
        prepare_target,
        execute_suite,
    ) -> None:
        info = SafetyTests().make_info()
        prepare_target.return_value = info
        execute_suite.return_value = [sd.TestResult("seq-write-1m", 0, 1.0)]
        args = argparse.Namespace(
            disk=None,
            verify_only=False,
            seconds=30,
            preflight=_preflight_info(),
        )
        with contextlib.redirect_stdout(io.StringIO()):
            result = sd.run(args)
        self.assertEqual(result, 0)
        mock_resolve.assert_called_once_with()
        prepare_target.assert_called_once_with("disk4", verify_only=False)

    @patch("tool.sd.execute_suite")
    @patch("tool.sd.prepare_target")
    @patch("tool.sd.preflight.run_preflight", return_value=_preflight_info())
    def test_run_runs_preflight_when_missing(
        self,
        mock_preflight,
        prepare_target,
        execute_suite,
    ) -> None:
        info = SafetyTests().make_info()
        prepare_target.return_value = info
        execute_suite.return_value = [sd.TestResult("seq-write-1m", 0, 1.0)]
        args = argparse.Namespace(disk="disk7", verify_only=False, seconds=30)
        with contextlib.redirect_stdout(io.StringIO()):
            result = sd.run(args)
        self.assertEqual(result, 0)
        mock_preflight.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
