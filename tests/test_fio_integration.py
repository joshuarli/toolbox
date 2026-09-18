"""Exercise real fio against fake disk files.

A fake disk is a sparse regular file that fio can act on exactly as it
would a raw device node. These tests pin our generated fio CLI against
actual fio behavior:

- every benchmark workload shape exits 0 on a healthy fake disk and
  reports ``err= 0`` (catches bad flags such as ``--readonly=1``),
- the integrity write-then-verify roundtrip passes,
- flipping a single byte fails the verify-only pass (proving the
  CRC32C verification we rely on for qualification actually bites).

The whole module skips cleanly when fio >= 3.42 is unavailable.
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from collections.abc import Iterator, Sequence
from pathlib import Path

from tool import sd
from tool.sd import preflight

FAKE_DISK_BYTES = 8 * 1024 * 1024
FIO_SIZE = "4m"
TIMEOUT_SECONDS = 180


def require_fio() -> str:
    """Return a new-enough fio path or skip the calling test."""
    try:
        path, _, _ = preflight.check_fio()
    except preflight.PreflightError as exc:
        raise unittest.SkipTest(f"fio >= 3.42 not available: {exc}") from exc
    return path


def _tail(proc: subprocess.CompletedProcess[str]) -> str:
    """Last of fio's output, for failure messages."""
    return (proc.stderr + proc.stdout)[-2000:]


@contextlib.contextmanager
def _quiet_stdio() -> Iterator[None]:
    """Mute Python and file-descriptor stdout/stderr (live fio inherits fds)."""
    with (
        open(os.devnull, "wb") as devnull,
        contextlib.redirect_stdout(io.StringIO()),
    ):
        stdout_fd = os.dup(1)
        stderr_fd = os.dup(2)
        try:
            os.dup2(devnull.fileno(), 1)
            os.dup2(devnull.fileno(), 2)
            yield
        finally:
            os.dup2(stdout_fd, 1)
            os.dup2(stderr_fd, 2)
            os.close(stdout_fd)
            os.close(stderr_fd)


class FioIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fio = require_fio()
        self._tmp = tempfile.TemporaryDirectory(prefix="tool-fakedisk-")
        self.disk = Path(self._tmp.name) / "disk.img"
        with open(self.disk, "wb") as handle:
            handle.truncate(FAKE_DISK_BYTES)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_fio(
        self, test: sd.FioTest, extra: Sequence[str] = ()
    ) -> subprocess.CompletedProcess[str]:
        argv = [*test.argv(self.fio, str(self.disk)), *extra]
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
            # Keep fio's verify .state files inside the temp dir.
            cwd=str(self.disk.parent),
        )

    def _corrupt_byte(self, offset: int) -> None:
        with open(self.disk, "r+b") as handle:
            handle.seek(offset)
            original = handle.read(1)
            handle.seek(offset)
            handle.write(bytes([original[0] ^ 0xFF]))

    def test_benchmark_shapes_exit_zero_on_fake_disk(self) -> None:
        shapes = [
            sd.FioTest("seq-read-1m", "read", "1m", runtime_seconds=1, read_only=True),
            sd.FioTest(
                "seq-write-1m", "write", "1m", runtime_seconds=1, end_fsync=True
            ),
            sd.FioTest(
                "rand-read-4k",
                "randread",
                "4k",
                runtime_seconds=1,
                read_only=True,
                norandommap=True,
            ),
            sd.FioTest(
                "rand-write-4k",
                "randwrite",
                "4k",
                runtime_seconds=1,
                norandommap=True,
                end_fsync=True,
            ),
            sd.FioTest(
                "rand-70r30w-4k",
                "randrw",
                "4k",
                runtime_seconds=1,
                rwmixread=70,
                norandommap=True,
                end_fsync=True,
            ),
        ]
        for shape in shapes:
            with self.subTest(shape=shape.name):
                proc = self._run_fio(shape, [f"--size={FIO_SIZE}"])
                self.assertEqual(proc.returncode, 0, msg=_tail(proc))
                self.assertIn("err= 0", proc.stdout)

    def test_integrity_write_then_verify_roundtrip(self) -> None:
        written = self._run_fio(sd.FioTest.integrity(), [f"--size={FIO_SIZE}"])
        self.assertEqual(written.returncode, 0, msg=_tail(written))
        verified = self._run_fio(sd.FioTest.verify_only(), [f"--size={FIO_SIZE}"])
        self.assertEqual(verified.returncode, 0, msg=_tail(verified))
        self.assertIn("err= 0", verified.stdout)

    def test_verify_only_detects_single_byte_corruption(self) -> None:
        written = self._run_fio(sd.FioTest.integrity(), [f"--size={FIO_SIZE}"])
        self.assertEqual(written.returncode, 0, msg=_tail(written))
        self._corrupt_byte(2 * 1024 * 1024 + 13)
        verified = self._run_fio(sd.FioTest.verify_only(), [f"--size={FIO_SIZE}"])
        self.assertNotEqual(verified.returncode, 0)
        self.assertIn("crc", (verified.stdout + verified.stderr).lower())

    def test_run_test_without_sudo_reports_pass(self) -> None:
        test = sd.FioTest("probe-read", "read", "4k", runtime_seconds=1, read_only=True)
        with _quiet_stdio():
            result = sd.run_test(test, self.fio, str(self.disk), use_sudo=False)
        self.assertTrue(result.passed)


if __name__ == "__main__":
    unittest.main()
