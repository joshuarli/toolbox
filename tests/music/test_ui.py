from unittest.mock import MagicMock, patch

import pytest

from tool.music.ui import bold, colored, dim, select_interactive


def test_colored_basic():
    result = colored("hello", 196)
    assert "\033[38;5;196m" in result
    assert "hello" in result
    assert result.endswith("\033[0m")


def test_colored_bold():
    result = colored("hello", 42, bold=True)
    assert "\033[1m" in result
    assert "\033[38;5;42m" in result


def test_colored_dim():
    result = colored("hello", 240, dim=True)
    assert "\033[2m" in result
    assert "\033[38;5;240m" in result


def test_colored_bold_and_dim():
    result = colored("x", 100, bold=True, dim=True)
    assert "\033[1m" in result
    assert "\033[2m" in result
    # Bold before Dim in the current implementation
    idx_bold = result.index("\033[1m")
    idx_dim = result.index("\033[2m")
    assert idx_bold < idx_dim


def test_bold():
    result = bold("hello")
    assert result == "\033[1mhello\033[0m"


def test_dim():
    result = dim("hello")
    assert result == "\033[2mhello\033[0m"


class TestSelectInteractive:
    def _key(self, name):
        """Build a mock Keystroke with the given name attribute."""
        k = MagicMock()
        k.name = name
        return k

    def test_returns_on_enter(self):
        def render(idx):
            return [f"Item {idx}"]

        with patch("blessed.Terminal") as mock_term_cls:
            mock_term = MagicMock()
            mock_term_cls.return_value = mock_term
            mock_term.inkey.side_effect = [self._key("KEY_ENTER")]
            mock_term.home = ""
            mock_term.clear_eos = ""

            result = select_interactive(render, 3)
            assert result == 0

    def test_navigates_down(self):
        def render(idx):
            return [f"Item {idx}"]

        with patch("blessed.Terminal") as mock_term_cls:
            mock_term = MagicMock()
            mock_term_cls.return_value = mock_term
            mock_term.inkey.side_effect = [
                self._key("KEY_DOWN"),
                self._key("KEY_DOWN"),
                self._key("KEY_ENTER"),
            ]
            mock_term.home = ""
            mock_term.clear_eos = ""

            result = select_interactive(render, 3)
            assert result == 2

    def test_up_clamped(self):
        def render(idx):
            return [f"Item {idx}"]

        with patch("blessed.Terminal") as mock_term_cls:
            mock_term = MagicMock()
            mock_term_cls.return_value = mock_term
            mock_term.inkey.side_effect = [
                self._key("KEY_UP"),
                self._key("KEY_UP"),
                self._key("KEY_ENTER"),
            ]
            mock_term.home = ""
            mock_term.clear_eos = ""

            result = select_interactive(render, 3)
            assert result == 0  # clamped at top

    def test_down_clamped(self):
        def render(idx):
            return [f"Item {idx}"]

        with patch("blessed.Terminal") as mock_term_cls:
            mock_term = MagicMock()
            mock_term_cls.return_value = mock_term
            mock_term.inkey.side_effect = [
                self._key("KEY_DOWN"),
                self._key("KEY_DOWN"),
                self._key("KEY_DOWN"),
                self._key("KEY_DOWN"),
                self._key("KEY_ENTER"),
            ]
            mock_term.home = ""
            mock_term.clear_eos = ""

            result = select_interactive(render, 3)
            assert result == 2  # clamped at bottom

    def test_quit_on_escape(self):
        def render(idx):
            return [f"Item {idx}"]

        with patch("blessed.Terminal") as mock_term_cls:
            mock_term = MagicMock()
            mock_term_cls.return_value = mock_term
            mock_term.inkey.return_value = self._key("KEY_ESCAPE")
            mock_term.home = ""
            mock_term.clear_eos = ""

            with pytest.raises(SystemExit):
                select_interactive(render, 3)
