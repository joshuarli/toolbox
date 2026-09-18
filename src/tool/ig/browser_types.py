from __future__ import annotations

from typing import Any, Protocol


class TabLike(Protocol):
    context: str

    async def command(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0
    ) -> dict[str, Any]: ...

    async def get_cookies(self) -> dict[str, Any]: ...

    async def evaluate(self, js: str) -> Any: ...

    async def activate(self) -> None: ...

    async def close(self) -> None: ...
