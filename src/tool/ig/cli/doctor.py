from __future__ import annotations

from tool.browser_require import require_shadowdriver
from tool.ig.browser import (
    _browser_log_tail,
    _profile_process_commands,
)


async def _doctor() -> None:
    sd = require_shadowdriver()
    browser_exe = await sd.default_chromium_binary()
    profile_dir = await sd.default_profile_dir()
    port_file = profile_dir / "devtools-port"
    log_file = profile_dir / "chromium.log"
    print("browser: chromium")
    print(f"executable: {browser_exe}")
    print(f"exists: {browser_exe.exists()}")
    print(f"profile: {profile_dir}")
    print(f"port file: {port_file.read_text().strip() if port_file.exists() else '(missing)'}")
    for lock_name in ("SingletonLock", "SingletonSocket"):
        lock = profile_dir / lock_name
        print(f"{lock.name}: {'present' if lock.exists() else 'missing'}")
    processes = await _profile_process_commands()
    print(f"profile processes: {len(processes)}")
    for line in processes[:10]:
        print(f"  {line}")
    if len(processes) > 10:
        print(f"  ... {len(processes) - 10} more")
    print(f"log: {log_file}")
    print(await _browser_log_tail(lines=20))
