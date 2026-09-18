from __future__ import annotations

from tool.ig.browser import _kill as _shadowdriver_kill


async def _kill() -> None:
    await _shadowdriver_kill()
