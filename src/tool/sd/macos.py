"""macOS-only execution layer for ``tool sd``.

Every macOS specificity in the codebase lives here: the ``diskutil``,
``sudo`` and ``caffeinate`` paths plus each subprocess that touches them.
Importing this module is side-effect free; every public helper calls
:func:`require_macos` first so non-Darwin runtimes fail closed with
:class:`~tool.errors.UnsupportedPlatformError` instead of shelling out to
binaries that do not exist there.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
from collections.abc import Sequence

from tool.sd.errors import SdError, UnsupportedPlatformError

DISKUTIL = "/usr/sbin/diskutil"
SUDO = "/usr/bin/sudo"
CAFFEINATE = "/usr/bin/caffeinate"


def is_macos() -> bool:
    """Return True only on Darwin."""
    return sys.platform == "darwin"


def require_macos(action: str) -> None:
    """Fail closed unless running on macOS."""
    if not is_macos():
        raise UnsupportedPlatformError(f"{action} requires macOS (found {sys.platform})")


def plist_command(argv: Sequence[str]) -> dict[str, object]:
    """Run a plist-emitting command and parse its stdout."""
    command = shlex.join(list(argv))
    require_macos(f"command: {command}")
    try:
        process = subprocess.run(
            list(argv),
            check=True,
            capture_output=True,
        )
        value = plistlib.loads(process.stdout)
    except OSError as exc:
        raise SdError(f"could not run: {command}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip() if exc.stderr else ""
        detail = f": {stderr}" if stderr else ""
        raise SdError(f"command failed: {command}{detail}") from exc
    except plistlib.InvalidFileException as exc:
        raise SdError(f"invalid plist returned by: {command}") from exc
    if not isinstance(value, dict):
        raise SdError(f"unexpected plist returned by: {command}")
    return {str(key): val for key, val in value.items()}


def disk_info_plist(target: str) -> dict[str, object]:
    """Return the raw ``diskutil info`` plist for a disk identifier."""
    return plist_command([DISKUTIL, "info", "-plist", target])


def physical_list_plist() -> dict[str, object]:
    """Return the raw ``diskutil list physical`` plist."""
    return plist_command([DISKUTIL, "list", "-plist", "physical"])


def root_info_plist() -> dict[str, object]:
    """Return the raw ``diskutil info`` plist for the boot volume."""
    return plist_command([DISKUTIL, "info", "-plist", "/"])


def apfs_list_plist() -> dict[str, object]:
    """Return the raw ``diskutil apfs list`` plist."""
    return plist_command([DISKUTIL, "apfs", "list", "-plist"])


def validate_sudo() -> None:
    """Refresh cached sudo credentials, failing closed on denial."""
    require_macos("sudo authentication")
    try:
        returncode = subprocess.run([SUDO, "-v"], check=False).returncode
    except OSError as exc:
        raise SdError("could not execute sudo") from exc
    if returncode != 0:
        raise SdError("sudo authentication failed")


def sudo_prefix() -> list[str]:
    """Return the ``sudo`` argv prefix for privileged raw-device I/O."""
    require_macos("sudo raw-device I/O")
    return [SUDO, "-n"]


def unmount_disk(device_node: str) -> None:
    """Unmount every volume on a device node via ``diskutil unmountDisk``."""
    require_macos(f"unmount {device_node}")
    validate_sudo()
    try:
        returncode = subprocess.run([SUDO, "-n", DISKUTIL, "unmountDisk", device_node], check=False).returncode
    except OSError as exc:
        raise SdError("could not execute diskutil unmountDisk") from exc
    if returncode != 0:
        raise SdError(f"could not unmount {device_node}")


def start_caffeinate() -> subprocess.Popen[bytes]:
    """Prevent idle sleep for the lifetime of the returned process."""
    require_macos("caffeinate")
    try:
        return subprocess.Popen(
            [CAFFEINATE, "-im", "-w", str(os.getpid())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise SdError("could not start caffeinate") from exc
