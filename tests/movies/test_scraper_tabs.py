import asyncio

from tool.movies.scraper import _replace_tab


class _FakeTab:
    def __init__(self) -> None:
        self.closed = False
        self.commands: list[str] = []

    async def command(self, method: str):
        self.commands.append(method)

    async def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self, replacement: _FakeTab) -> None:
        self.replacement = replacement

    async def new_tab(self) -> _FakeTab:
        return self.replacement


def test_replace_tab_swaps_live_slot_enables_telemetry_and_closes_old() -> None:
    async def run() -> None:
        old = _FakeTab()
        replacement = _FakeTab()
        tabs = [old]

        result = await _replace_tab(
            _FakeBrowser(replacement),
            tabs,
            0,
            old,
            enable_telemetry=True,
        )

        assert result is replacement
        assert tabs == [replacement]
        assert replacement.commands == ["Performance.enable"]
        assert old.closed

    asyncio.run(run())
