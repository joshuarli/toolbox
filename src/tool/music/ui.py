"""ANSI 256-color terminal formatting shared by scan and tag."""

import sys
from collections.abc import Callable

import blessed

# Resets
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"


def _c(code: int) -> str:
    return f"\033[38;5;{code}m"


def colored(text: str, code: int, *, bold: bool = False, dim: bool = False) -> str:
    parts = [_c(code)]
    if bold:
        parts.append(_BOLD)
    if dim:
        parts.append(_DIM)
    parts.append(text)
    parts.append(_RESET)
    return "".join(parts)


def bold(text: str) -> str:
    return f"{_BOLD}{text}{_RESET}"


def dim(text: str) -> str:
    return f"{_DIM}{text}{_RESET}"


def select_interactive(render: Callable[[int], list[str]], item_count: int) -> int:
    """Fullscreen arrow-key selector. Blocks until the user picks an item.

    *render(idx)* returns the lines to display when item *idx* is highlighted.
    Returns the selected index. Exits the process on q / s / ctrl-c.
    """
    term = blessed.Terminal()
    selected = 0
    last = item_count - 1

    def _display(idx: int) -> None:
        content = "\n".join(render(idx))
        print(term.home + term.clear_eos + content, end="", flush=True)

    with term.fullscreen(), term.hidden_cursor():
        _display(0)
        while True:
            key = term.inkey()
            if key.name == "KEY_UP":
                selected = max(0, selected - 1)
            elif key.name == "KEY_DOWN":
                selected = min(last, selected + 1)
            elif key.name == "KEY_ENTER":
                return selected
            elif str(key).lower() in ("q", "s") or key.name == "KEY_ESCAPE":
                print("\nAborted.")
                sys.exit(0)
            else:
                continue
            _display(selected)
