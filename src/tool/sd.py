"""Destructively qualify an SD card or removable raw disk with fio.

Platform policy: this module holds pure logic (plist parsing, safety
validation, fio argv construction, interactive prompting) plus thin
orchestration. Every macOS specificity -- ``diskutil``/``sudo``/``caffeinate``
paths and the subprocesses that touch them -- lives in :mod:`tool.macos`
behind a platform gate. The only macOS touchpoints here are calls into that
module and the ``sys.platform`` dispatch in :func:`discover_candidates`.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import macos, preflight
from .errors import SafetyError, SdError

__all__ = (
    "CandidateDisk",
    "DiskInfo",
    "FioTest",
    "SafetyError",
    "SdError",
    "TestResult",
)

DISK_RE = re.compile(r"^(?:/dev/)?(disk\d+)$")
ANY_DISK_IDENTIFIER_RE = re.compile(r"^(disk\d+)(?:s\d+)*$")
UNIX_WHOLE_DISK_RE = re.compile(r"^(sd[a-z]|mmcblk\d+|nvme\d+n\d+|disk\d+)$")


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
            if isinstance(value, bool):
                return 0
            return value if isinstance(value, int) else 0

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
        # A built-in SD reader can report Internal=True while its media
        # remains ejectable/removable. External fixed media is accepted.
        return (
            not self.internal
            or self.ejectable
            or self.removable
            or self.removable_media
            or self.removable_media_or_external
        )


@dataclass(frozen=True, slots=True)
class CandidateDisk:
    identifier: str
    device_node: str
    media_name: str
    total_size: int
    detail: str


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
            # Bare flag: fio 3.42 rejects `--readonly=1`.
            argv.append("--readonly")
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
    parser.description = (
        "Destructively qualify an SD card or removable raw disk with fio."
    )
    parser.add_argument(
        "disk",
        nargs="?",
        default=None,
        help="whole disk identifier: disk4 or /dev/disk4 (omit for interactive picker)",
    )
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
        raise SafetyError(
            f"{info.identifier} backs the running macOS system; refusing it"
        )


def disk_info(target: str) -> DiskInfo:
    """Fetch and parse the macOS ``diskutil info`` plist for a target."""
    return DiskInfo.from_plist(macos.disk_info_plist(target))


def physical_disks() -> set[str]:
    """Return macOS whole-disk identifiers reported as physical."""
    data = macos.physical_list_plist()
    value = data.get("WholeDisks")
    if not isinstance(value, list):
        raise SafetyError("diskutil did not return the physical WholeDisks list")
    result = {item for item in value if isinstance(item, str)}
    if not result:
        raise SafetyError("diskutil reported no physical whole disks")
    return result


def root_backing_disks() -> set[str]:
    """Return the whole disks backing the running macOS root, fail-closed."""
    root = macos.root_info_plist()
    apfs: dict[str, object] | None = None
    if isinstance(root.get("APFSContainerReference"), str):
        apfs = macos.apfs_list_plist()
    result = root_backing_disks_from_plists(root, apfs)
    if not result:
        raise SafetyError(
            "cannot prove which physical disk backs the running macOS root"
        )
    return result


def candidate_from_disk_info(info: DiskInfo) -> CandidateDisk:
    detail = info.bus_protocol or info.media_type or "(unknown)"
    return CandidateDisk(
        identifier=info.identifier,
        device_node=info.device_node,
        media_name=info.media_name or "(unknown)",
        total_size=info.total_size,
        detail=detail,
    )


def discover_darwin_candidates() -> list[CandidateDisk]:
    """Autodetect eligible disks via diskutil plus a unix device check."""
    physical = physical_disks()
    try:
        roots = root_backing_disks()
    except SdError:
        roots = set()
    candidates: list[CandidateDisk] = []
    for identifier in sorted(physical):
        try:
            info = disk_info(identifier)
        except SdError:
            continue
        try:
            validate_candidate(info, physical, roots)
        except SafetyError:
            continue
        raw_path = Path(info.raw_node)
        try:
            if not raw_path.exists() or not raw_path.is_char_device():
                continue
        except OSError:
            continue
        candidates.append(candidate_from_disk_info(info))
    return candidates


def discover_unixy_candidates(
    dev_dir: Path = Path("/dev"),
) -> list[CandidateDisk]:
    """Generic Unix fallback: enumerate whole-disk-like device nodes."""
    try:
        entries = list(dev_dir.iterdir())
    except OSError as exc:
        raise SdError(f"could not list {dev_dir}: {exc}") from exc
    candidates: list[CandidateDisk] = []
    for entry in sorted(entries, key=lambda item: item.name):
        if UNIX_WHOLE_DISK_RE.fullmatch(entry.name) is None:
            continue
        try:
            if not entry.exists():
                continue
        except OSError:
            continue
        candidates.append(
            CandidateDisk(
                identifier=entry.name,
                device_node=str(entry),
                media_name="(unknown)",
                total_size=0,
                detail="unix",
            )
        )
    return candidates


def discover_candidates() -> list[CandidateDisk]:
    if macos.is_macos():
        return discover_darwin_candidates()
    return discover_unixy_candidates()


def format_candidate(index: int, candidate: CandidateDisk) -> str:
    if candidate.total_size > 0:
        size = format_size(candidate.total_size)
    else:
        size = "unknown size"
    return (
        f"  {index}) {candidate.device_node} — {candidate.media_name}"
        f" — {size} — {candidate.detail}"
    )


def prompt_for_candidate(
    candidates: Sequence[CandidateDisk],
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> CandidateDisk:
    if not candidates:
        raise SdError("no suitable removable disks found; connect a card and try again")
    output_fn("Available removable disks:")
    for position, candidate in enumerate(candidates, start=1):
        output_fn(format_candidate(position, candidate))
    last = len(candidates)
    while True:
        try:
            response = input_fn(f"Select a disk [1-{last}] (or q to quit): ")
        except EOFError as exc:
            raise SdError("no disk selected") from exc
        text = response.strip().lower()
        if text in {"q", "quit", "exit"}:
            raise SdError("cancelled by user")
        try:
            choice = int(text)
        except ValueError:
            output_fn(f"Enter a number 1-{last} or q to quit.")
            continue
        if 1 <= choice <= last:
            return candidates[choice - 1]
        output_fn(f"Enter a number 1-{last} or q to quit.")


def resolve_interactive_disk(
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> str:
    if not sys.stdin.isatty():
        raise SdError(
            "no disk specified and stdin is not interactive; pass diskN explicitly"
        )
    candidates = discover_candidates()
    selected = prompt_for_candidate(candidates, input_fn=input_fn, output_fn=output_fn)
    return selected.identifier


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
    return (
        FioTest(
            "seq-read-1m",
            "read",
            "1m",
            runtime_seconds=seconds,
            ramp_seconds=ramp_seconds,
            read_only=True,
        ),
        FioTest(
            "seq-write-1m",
            "write",
            "1m",
            runtime_seconds=seconds,
            ramp_seconds=ramp_seconds,
            end_fsync=True,
        ),
        FioTest(
            "rand-read-4k",
            "randread",
            "4k",
            runtime_seconds=seconds,
            ramp_seconds=ramp_seconds,
            read_only=True,
            norandommap=True,
        ),
        FioTest(
            "rand-write-4k",
            "randwrite",
            "4k",
            runtime_seconds=seconds,
            ramp_seconds=ramp_seconds,
            norandommap=True,
            end_fsync=True,
        ),
        FioTest(
            "rand-70r30w-4k",
            "randrw",
            "4k",
            runtime_seconds=seconds,
            ramp_seconds=ramp_seconds,
            rwmixread=70,
            norandommap=True,
            end_fsync=True,
        ),
    )


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
        raise SdError("confirmation did not match")


def print_target(info: DiskInfo, *, verify_only: bool) -> None:
    print("=" * 72)
    print("sd — raw-device SD/removable-media qualification")
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
        print("The final pass overwrites and verifies the advertised capacity.")


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


def prepare_target(identifier: str, *, verify_only: bool) -> DiskInfo:
    try:
        normalized = normalize_disk(identifier)
    except ValueError as exc:
        raise SdError(str(exc)) from exc
    before = disk_info(normalized)
    validate_candidate(before, physical_disks(), root_backing_disks())
    print_target(before, verify_only=verify_only)
    confirm_target(normalized, verify_only=verify_only)
    macos.unmount_disk(before.device_node)
    after = same_device(before)
    raw_path = Path(after.raw_node)
    if not raw_path.exists() or not raw_path.is_char_device():
        raise SafetyError(f"raw character device is unavailable: {after.raw_node}")
    return after


def run_test(
    test: FioTest, fio: str, raw_device: str, *, use_sudo: bool = True
) -> TestResult:
    """Run one fio workload; ``use_sudo=False`` runs fio directly (tests)."""
    print(f"\n{' ' + test.name + ' ':=^72}\n")
    if use_sudo:
        macos.validate_sudo()
        command = [*macos.sudo_prefix(), *test.argv(fio, raw_device)]
    else:
        command = test.argv(fio, raw_device)
    started = time.monotonic()
    try:
        returncode = subprocess.run(command, check=False).returncode
    except OSError as exc:
        raise SdError(f"could not execute fio test {test.name}") from exc
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
    use_sudo: bool = True,
) -> list[TestResult]:
    # caffeinate exists only on macOS; fake-disk test runs skip it.
    caffeinate: subprocess.Popen[bytes] | None = None
    if macos.is_macos():
        caffeinate = macos.start_caffeinate()

    results: list[TestResult] = []
    try:
        if verify_only:
            results.append(
                run_test(FioTest.verify_only(), fio, info.raw_node, use_sudo=use_sudo)
            )
            return results
        print(
            f"\nPerformance workloads: QD1, {seconds}s each. "
            "The integrity pass has no time limit."
        )
        for test in benchmark_tests(seconds):
            results.append(run_test(test, fio, info.raw_node, use_sudo=use_sudo))
        print(
            "\nFinal qualification: full-device write → flush → "
            "read → CRC32C verification."
        )
        print("Verification continues after mismatches so damage can surface.")
        results.append(
            run_test(FioTest.integrity(), fio, info.raw_node, use_sudo=use_sudo)
        )
        return results
    finally:
        if caffeinate is not None:
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
        line = (
            f"{result.name:<28} {status:<12} {format_duration(result.elapsed_seconds)}"
        )
        print(line)
    failed = [result for result in results if not result.passed]
    print(f"\nOVERALL: {'FAIL' if failed else 'PASS'}")
    if failed:
        print("Overall health: FAILED — do not trust this card.")
    else:
        print("Overall health: HEALTHY (all checks passed).")
    print("Any verification mismatch or I/O error is a failure.")
    print("Performance is intentionally not graded: compare bandwidth, IOPS, and")
    print("especially p99/p99.9 latency between cards.")
    print("The device is intentionally left unmounted.")
    if not verify_only and not failed:
        print("\nStrongly recommended persistence check:")
        print(f"  diskutil eject /dev/{identifier}")
        print("  physically remove/reinsert the card; find its new diskN; then:")
        print("  tool sd --verify-only diskN")
    return not failed


def run(args: argparse.Namespace) -> int:
    preflight_info = getattr(args, "preflight", None)
    if preflight_info is None:
        preflight_info = preflight.run_preflight()
    fio = preflight_info.fio_path
    fio_version = preflight_info.fio_version
    seconds = getattr(args, "seconds", 30)
    verify_only = bool(getattr(args, "verify_only", False))
    if seconds <= 0:
        raise SdError("--seconds must be > 0")
    print(f"fio: {fio_version} ({fio})")
    print(f"macOS {preflight_info.macos_version} preflight: OK")
    disk_arg = getattr(args, "disk", None)
    if disk_arg is None:
        print("No disk specified; autodetecting removable disks…")
        disk_arg = resolve_interactive_disk()
    info = prepare_target(disk_arg, verify_only=verify_only)
    results = execute_suite(
        info,
        fio,
        seconds=seconds,
        verify_only=verify_only,
    )
    passed = render_summary(
        results,
        verify_only=verify_only,
        identifier=info.identifier,
    )
    return 0 if passed else 1
