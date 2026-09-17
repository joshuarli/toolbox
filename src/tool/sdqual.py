from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import time

DISKUTIL = "/usr/sbin/diskutil"
SUDO = "/usr/bin/sudo"
CAFFEINATE = "/usr/bin/caffeinate"

DISK_RE = re.compile(r"^(?:/dev/)?(disk\d+)$")
ANY_DISK_IDENTIFIER_RE = re.compile(r"^(disk\d+)(?:s\d+)*$")


class SdqualError(RuntimeError):
    """Expected user-facing sdqual error."""


class SafetyError(SdqualError):
    """The target could not be proven safe to overwrite."""


@dataclass(frozen=True, slots=True)
class DiskInfo:
    identifier: str
    device_node: str
    whole_disk: bool
    writable: bool
    internal: bool
    ejectable: bool
    removable: bool
    removable_media: bool
    removable_media_or_external: bool
    bus_protocol: str
    media_name: str
    media_type: str
    total_size: int
    block_size: int

    @classmethod
    def from_plist(cls, data: Mapping[str, object]) -> DiskInfo:
        identifier = data.get("DeviceIdentifier")
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("diskutil plist is missing DeviceIdentifier")

        def boolean(key: str) -> bool:
            value = data.get(key, False)
            return value if isinstance(value, bool) else False

        def string(key: str) -> str:
            value = data.get(key, "")
            return value if isinstance(value, str) else ""

        def integer(key: str) -> int:
            value = data.get(key, 0)
            return value if isinstance(value, int) and not isinstance(value, bool) else 0

        device_node = data.get("DeviceNode")
        if not isinstance(device_node, str) or not device_node:
            device_node = f"/dev/{identifier}"

        writable_media = data.get("WritableMedia", True)
        writable = boolean("Writable") and writable_media is True

        return cls(
            identifier=identifier,
            device_node=device_node,
            whole_disk=boolean("WholeDisk"),
            writable=writable,
            internal=boolean("Internal"),
            ejectable=boolean("Ejectable"),
            removable=boolean("Removable"),
            removable_media=boolean("RemovableMedia"),
            removable_media_or_external=boolean("RemovableMediaOrExternalDevice"),
            bus_protocol=string("BusProtocol"),
            media_name=string("MediaName"),
            media_type=string("MediaType"),
            total_size=integer("TotalSize") or integer("Size"),
            block_size=integer("DeviceBlockSize"),
        )

    @property
    def raw_node(self) -> str:
        return f"/dev/r{self.identifier}"

    @property
    def is_removable_or_external(self) -> bool:
        # A built-in SD reader can report Internal=True while its media remains
        # ejectable/removable. External fixed media is also accepted by design.
        return (
            not self.internal
            or self.ejectable
            or self.removable
            or self.removable_media
            or self.removable_media_or_external
        )


@dataclass(frozen=True, slots=True)
class FioTest:
    name: str
    rw: str
    block_size: str
    runtime_seconds: int | None = None
    ramp_seconds: int | None = None
    read_only: bool = False
    rwmixread: int | None = None
    norandommap: bool = False
    verify_crc32c: bool = False
    verify_only_mode: bool = False
    end_fsync: bool = False

    @classmethod
    def integrity(cls) -> FioTest:
        return cls(
            name="full-device-integrity",
            rw="write",
            block_size="1m",
            verify_crc32c=True,
            end_fsync=True,
        )

    @classmethod
    def verify_only(cls) -> FioTest:
        return cls(
            name="integrity-verify-only",
            rw="write",
            block_size="1m",
            verify_crc32c=True,
            verify_only_mode=True,
        )

    def argv(self, fio: str, raw_device: str) -> list[str]:
        argv = [
            fio,
            f"--name={self.name}",
            f"--filename={raw_device}",
            "--ioengine=psync",
            "--direct=1",
            "--iodepth=1",
            "--group_reporting=1",
            "--output-format=normal",
            "--eta=always",
            "--eta-interval=5",
            f"--rw={self.rw}",
            f"--bs={self.block_size}",
        ]
        if self.runtime_seconds is not None:
            argv.extend(["--time_based=1", f"--runtime={self.runtime_seconds}"])
            if self.ramp_seconds:
                argv.append(f"--ramp_time={self.ramp_seconds}")
        if self.read_only:
            argv.append("--readonly=1")
        if self.rwmixread is not None:
            argv.append(f"--rwmixread={self.rwmixread}")
        if self.norandommap:
            argv.append("--norandommap=1")
        if self.end_fsync:
            argv.append("--end_fsync=1")
        if self.verify_crc32c:
            argv.append("--verify=crc32c")
            if self.verify_only_mode:
                argv.append("--verify_only=1")
            else:
                argv.extend(["--do_verify=1", "--verify_fatal=0"])
        return argv


@dataclass(frozen=True, slots=True)
class TestResult:
    name: str
    returncode: int
    elapsed_seconds: float

    @property
    def passed(self) -> bool:
        return self.returncode == 0


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.description = "Destructively qualify an SD card or removable raw disk with fio."
    parser.add_argument("disk", help="whole disk identifier: disk4 or /dev/disk4")
    parser.add_argument(
        "--seconds",
        type=int,
        default=30,
        help="seconds per performance workload (default: 30)",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="verify data written by a previous full run without rewriting it",
    )


def normalize_disk(value: str) -> str:
    value = value.strip()
    match = DISK_RE.fullmatch(value)
    if match:
        return match.group(1)
    if re.fullmatch(r"(?:/dev/)?disk\d+(?:s\d+)+", value):
        raise ValueError("target must be a whole disk, not a partition (use diskN)")
    raise ValueError("target must be in the form diskN or /dev/diskN")


def whole_disk_for_identifier(identifier: str) -> str | None:
    match = ANY_DISK_IDENTIFIER_RE.fullmatch(identifier)
    return match.group(1) if match else None


def root_backing_disks_from_plists(
    root_info: Mapping[str, object],
    apfs_info: Mapping[str, object] | None,
) -> set[str]:
    root_id = root_info.get("DeviceIdentifier")
    parent = root_info.get("ParentWholeDisk")
    container_ref = root_info.get("APFSContainerReference")

    if isinstance(container_ref, str):
        if apfs_info is None:
            return set()
        containers = apfs_info.get("Containers")
        if not isinstance(containers, list):
            return set()
        for container in containers:
            if not isinstance(container, Mapping):
                continue
            volumes = container.get("Volumes")
            volume_ids: set[object] = set()
            if isinstance(volumes, list):
                volume_ids = {
                    volume.get("DeviceIdentifier")
                    for volume in volumes
                    if isinstance(volume, Mapping)
                }
            if not (
                container.get("ContainerReference") == container_ref
                or root_id in volume_ids
            ):
                continue
            stores = container.get("PhysicalStores")
            if not isinstance(stores, list):
                return set()
            result: set[str] = set()
            for store in stores:
                if not isinstance(store, Mapping):
                    continue
                identifier = store.get("DeviceIdentifier")
                if isinstance(identifier, str):
                    whole = whole_disk_for_identifier(identifier)
                    if whole is not None:
                        result.add(whole)
            return result
        return set()

    if isinstance(parent, str):
        whole = whole_disk_for_identifier(parent)
        return {whole} if whole is not None else set()
    if isinstance(root_id, str):
        whole = whole_disk_for_identifier(root_id)
        return {whole} if whole is not None else set()
    return set()


def validate_target(info: DiskInfo, physical_disks: set[str]) -> None:
    if not info.whole_disk:
        raise SafetyError(f"{info.identifier} is not a whole disk")
    if info.identifier not in physical_disks:
        raise SafetyError(f"{info.identifier} is not a physical whole disk")
    if not info.writable:
        raise SafetyError(f"{info.identifier} is not writable")
    if not info.is_removable_or_external:
        raise SafetyError(f"{info.identifier} is not removable/external; refusing it")
    if info.total_size <= 0 or info.block_size <= 0:
        raise SafetyError(f"diskutil reported invalid geometry for {info.identifier}")


def validate_candidate(
    info: DiskInfo,
    physical_disks: set[str],
    root_disks: set[str],
) -> None:
    validate_target(info, physical_disks)
    if info.identifier in root_disks:
        raise SafetyError(f"{info.identifier} backs the running macOS system; refusing it")


def plist_command(argv: Sequence[str]) -> dict[str, object]:
    try:
        process = subprocess.run(
            list(argv),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        value = plistlib.loads(process.stdout)
    except OSError as exc:
        raise SdqualError(f"could not run: {shlex.join(argv)}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip() if exc.stderr else ""
        detail = f": {stderr}" if stderr else ""
        raise SdqualError(f"command failed: {shlex.join(argv)}{detail}") from exc
    except plistlib.InvalidFileException as exc:
        raise SdqualError(f"invalid plist returned by: {shlex.join(argv)}") from exc
    if not isinstance(value, dict):
        raise SdqualError(f"unexpected plist returned by: {shlex.join(argv)}")
    return value


def disk_info(target: str) -> DiskInfo:
    return DiskInfo.from_plist(plist_command([DISKUTIL, "info", "-plist", target]))


def physical_disks() -> set[str]:
    data = plist_command([DISKUTIL, "list", "-plist", "physical"])
    value = data.get("WholeDisks")
    if not isinstance(value, list):
        raise SafetyError("diskutil did not return the physical WholeDisks list")
    result = {item for item in value if isinstance(item, str)}
    if not result:
        raise SafetyError("diskutil reported no physical whole disks")
    return result


def root_backing_disks() -> set[str]:
    root = plist_command([DISKUTIL, "info", "-plist", "/"])
    apfs: dict[str, object] | None = None
    if isinstance(root.get("APFSContainerReference"), str):
        apfs = plist_command([DISKUTIL, "apfs", "list", "-plist"])
    result = root_backing_disks_from_plists(root, apfs)
    if not result:
        raise SafetyError("cannot prove which physical disk backs the running macOS root")
    return result


def format_size(size: int) -> str:
    return f"{size / 1e9:.2f} GB ({size / 2**30:.2f} GiB)"


def format_duration(seconds: float) -> str:
    rounded = round(seconds)
    hours, remainder = divmod(rounded, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def benchmark_tests(seconds: int) -> tuple[FioTest, ...]:
    ramp_seconds = min(3, seconds // 10)
    common = {"runtime_seconds": seconds, "ramp_seconds": ramp_seconds}
    return (
        FioTest("seq-read-1m", "read", "1m", read_only=True, **common),
        FioTest("seq-write-1m", "write", "1m", end_fsync=True, **common),
        FioTest(
            "rand-read-4k",
            "randread",
            "4k",
            read_only=True,
            norandommap=True,
            **common,
        ),
        FioTest(
            "rand-write-4k",
            "randwrite",
            "4k",
            norandommap=True,
            end_fsync=True,
            **common,
        ),
        FioTest(
            "rand-70r30w-4k",
            "randrw",
            "4k",
            rwmixread=70,
            norandommap=True,
            end_fsync=True,
            **common,
        ),
    )


def require_fio() -> tuple[str, str]:
    fio = shutil.which("fio")
    if fio is None:
        raise SdqualError("fio not found; install it with: brew install fio")
    try:
        process = subprocess.run(
            [fio, "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SdqualError("fio is installed but could not be executed") from exc
    version = process.stdout.strip()
    if not version:
        raise SdqualError("fio returned an empty version string")
    return fio, version


def sudo_validate() -> None:
    try:
        returncode = subprocess.run([SUDO, "-v"]).returncode
    except OSError as exc:
        raise SdqualError("could not execute sudo") from exc
    if returncode != 0:
        raise SdqualError("sudo authentication failed")


def confirm_target(
    identifier: str,
    *,
    verify_only: bool,
    input_fn: Callable[[str], str] = input,
) -> None:
    verb = "VERIFY" if verify_only else "ERASE"
    expected = f"{verb} {identifier}"
    response = input_fn(f"Type exactly {expected!r} to continue: ")
    if response != expected:
        raise SdqualError("confirmation did not match")


def print_target(info: DiskInfo, *, verify_only: bool) -> None:
    print("=" * 72)
    print("sdqual — raw-device SD/removable-media qualification")
    print("=" * 72)
    print(f"device:     {info.device_node}")
    print(f"raw device: {info.raw_node}")
    print(f"media:      {info.media_name or '(unknown)'}")
    print(f"protocol:   {info.bus_protocol or '(unknown)'}")
    print(f"capacity:   {format_size(info.total_size)}")
    print(f"block size: {info.block_size} bytes")
    print(f"internal:   {'yes' if info.internal else 'no'}")
    print(f"ejectable:  {'yes' if info.ejectable else 'no'}")
    if verify_only:
        print(
            "\nVERIFY ONLY: fio will read and validate verification data "
            "from a previous full run."
        )
    else:
        print("\nWARNING: THIS WILL DESTROY ALL DATA ON THE DEVICE.")
        print("The final pass overwrites and verifies the device's advertised capacity.")


def _identity(info: DiskInfo) -> tuple[object, ...]:
    return (
        info.identifier,
        info.device_node,
        info.total_size,
        info.block_size,
        info.media_name,
        info.bus_protocol,
        info.internal,
        info.ejectable,
        info.removable,
    )


def same_device(before: DiskInfo) -> DiskInfo:
    after = disk_info(before.identifier)
    if _identity(before) != _identity(after):
        raise SafetyError("device identity changed after confirmation/unmount")
    validate_candidate(after, physical_disks(), root_backing_disks())
    return after


def unmount_disk(info: DiskInfo) -> None:
    sudo_validate()
    try:
        returncode = subprocess.run(
            [SUDO, "-n", DISKUTIL, "unmountDisk", info.device_node]
        ).returncode
    except OSError as exc:
        raise SdqualError("could not execute diskutil unmountDisk") from exc
    if returncode != 0:
        raise SdqualError(f"could not unmount {info.device_node}")


def prepare_target(identifier: str, *, verify_only: bool) -> DiskInfo:
    try:
        normalized = normalize_disk(identifier)
    except ValueError as exc:
        raise SdqualError(str(exc)) from exc
    before = disk_info(normalized)
    validate_candidate(before, physical_disks(), root_backing_disks())
    print_target(before, verify_only=verify_only)
    confirm_target(normalized, verify_only=verify_only)
    unmount_disk(before)
    after = same_device(before)
    raw_path = Path(after.raw_node)
    if not raw_path.exists() or not raw_path.is_char_device():
        raise SafetyError(f"raw character device is unavailable: {after.raw_node}")
    return after


def run_test(test: FioTest, fio: str, raw_device: str) -> TestResult:
    print(f"\n{' ' + test.name + ' ':=^72}\n")
    sudo_validate()
    started = time.monotonic()
    try:
        returncode = subprocess.run(
            [SUDO, "-n", *test.argv(fio, raw_device)]
        ).returncode
    except OSError as exc:
        raise SdqualError(f"could not execute fio test {test.name}") from exc
    elapsed = time.monotonic() - started
    status = "PASS" if returncode == 0 else f"FAIL (fio exit {returncode})"
    print(f"\n{test.name}: {status} [{format_duration(elapsed)}]")
    return TestResult(test.name, returncode, elapsed)


def _stop_caffeinate(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def execute_suite(
    info: DiskInfo,
    fio: str,
    *,
    seconds: int,
    verify_only: bool,
) -> list[TestResult]:
    try:
        caffeinate = subprocess.Popen(
            [CAFFEINATE, "-im", "-w", str(os.getpid())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise SdqualError("could not start caffeinate") from exc

    results: list[TestResult] = []
    try:
        if verify_only:
            results.append(run_test(FioTest.verify_only(), fio, info.raw_node))
            return results
        print(
            f"\nPerformance workloads: QD1, {seconds}s each. "
            "The integrity pass has no time limit."
        )
        for test in benchmark_tests(seconds):
            results.append(run_test(test, fio, info.raw_node))
        print(
            "\nFinal qualification: full-device write → flush → "
            "read → CRC32C verification."
        )
        print(
            "Verification continues after mismatches so additional damage can surface."
        )
        results.append(run_test(FioTest.integrity(), fio, info.raw_node))
        return results
    finally:
        _stop_caffeinate(caffeinate)


def render_summary(
    results: Sequence[TestResult],
    *,
    verify_only: bool,
    identifier: str,
) -> bool:
    print(f"\n{' SUMMARY ':=^72}\n")
    for result in results:
        status = "PASS" if result.passed else f"FAIL ({result.returncode})"
        print(
            f"{result.name:<28} "
            f"{status:<12} "
            f"{format_duration(result.elapsed_seconds)}"
        )
    failed = [result for result in results if not result.passed]
    print(f"\nOVERALL: {'FAIL' if failed else 'PASS'}")
    print("Any verification mismatch or I/O error is a failure.")
    print("Performance is intentionally not graded: compare bandwidth, IOPS, and")
    print("especially p99/p99.9 latency between cards.")
    print("The device is intentionally left unmounted.")
    if not verify_only and not failed:
        print("\nStrongly recommended persistence check:")
        print(f"  diskutil eject /dev/{identifier}")
        print("  physically remove/reinsert the card; find its new diskN; then:")
        print("  tool sdqual --verify-only diskN")
    return not failed


def run(args: argparse.Namespace) -> int:
    if platform.system() != "Darwin":
        raise SdqualError("sdqual is macOS-only")
    if args.seconds <= 0:
        raise SdqualError("--seconds must be > 0")
    fio, fio_version = require_fio()
    print(f"fio: {fio_version} ({fio})")
    info = prepare_target(args.disk, verify_only=args.verify_only)
    results = execute_suite(
        info,
        fio,
        seconds=args.seconds,
        verify_only=args.verify_only,
    )
    passed = render_summary(
        results,
        verify_only=args.verify_only,
        identifier=info.identifier,
    )
    return 0 if passed else 1
