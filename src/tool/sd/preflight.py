"""CLI-global preflight: platform gate and external dependency checks."""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
from dataclasses import dataclass

MIN_MACOS_MAJOR = 26
MIN_FIO_MAJOR = 3
MIN_FIO_MINOR = 42

_FIO_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


class PreflightError(RuntimeError):
    """Expected user-facing preflight error."""


@dataclass(frozen=True, slots=True)
class PreflightInfo:
    macos_version: str
    macos_major: int
    fio_path: str
    fio_version: str
    fio_version_tuple: tuple[int, ...]


def parse_macos_major(version: str) -> int:
    """Parse the major component of a macOS version string like ``26.5.2``."""
    major_text = version.strip().split(".")[0] if version.strip() else ""
    try:
        major = int(major_text)
    except ValueError:
        raise PreflightError(
            f"could not parse macOS version {version!r}; tool requires macOS {MIN_MACOS_MAJOR}+"
        ) from None
    return major


def check_platform(
    system: str | None = None,
    mac_version: str | None = None,
) -> tuple[str, int]:
    """Fail closed unless running on a supported macOS release.

    Returns the macOS version string and major version on success.
    """
    if system is None:
        system = platform.system()
    if system != "Darwin":
        found = system or "unknown platform"
        raise PreflightError(f"tool requires macOS {MIN_MACOS_MAJOR}+ (Darwin); found {found}")
    if mac_version is None:
        mac_version = platform.mac_ver()[0]
    if not mac_version:
        raise PreflightError(f"could not determine macOS version; tool requires macOS {MIN_MACOS_MAJOR}+")
    major = parse_macos_major(mac_version)
    if major < MIN_MACOS_MAJOR:
        raise PreflightError(f"tool requires macOS {MIN_MACOS_MAJOR}+; found macOS {mac_version}")
    return mac_version, major


def parse_fio_version(output: str) -> tuple[int, ...]:
    """Parse ``fio --version`` output such as ``fio-3.42``."""
    match = _FIO_VERSION_RE.search(output.strip())
    if match is None:
        raise PreflightError(f"could not parse fio version from: {output.strip()!r}")
    parts = tuple(int(group) for group in match.groups() if group is not None)
    if len(parts) < 2:
        raise PreflightError(f"could not parse fio version from: {output.strip()!r}")
    return parts


def check_fio() -> tuple[str, str, tuple[int, ...]]:
    """Locate fio and fail closed unless it is new enough.

    Returns the fio path, raw version string, and parsed version tuple.
    """
    fio = shutil.which("fio")
    if fio is None:
        raise PreflightError("fio >= 3.42 not found; install it with: brew install fio")
    try:
        process = subprocess.run(
            [fio, "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PreflightError("fio is installed but could not be executed") from exc
    version = process.stdout.strip()
    if not version:
        raise PreflightError("fio returned an empty version string")
    parsed = parse_fio_version(version)
    major = parsed[0]
    minor = parsed[1] if len(parsed) > 1 else 0
    if (major, minor) < (MIN_FIO_MAJOR, MIN_FIO_MINOR):
        raise PreflightError(f"fio >= {MIN_FIO_MAJOR}.{MIN_FIO_MINOR} required; found {version} ({fio})")
    return fio, version, parsed


def run_preflight() -> PreflightInfo:
    """Run every CLI-global gate and return the resolved environment."""
    macos_version, macos_major = check_platform()
    fio_path, fio_version, fio_tuple = check_fio()
    return PreflightInfo(
        macos_version=macos_version,
        macos_major=macos_major,
        fio_path=fio_path,
        fio_version=fio_version,
        fio_version_tuple=fio_tuple,
    )
