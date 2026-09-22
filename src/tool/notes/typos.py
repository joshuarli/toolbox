"""Curated known-misspelling table from the pinned ``typos-dict`` dataset.

The correction list is a pinned copy of ``typos-dict`` 0.14.1. Rows with
multiple corrections are ignored: an ambiguous alternative is not a
high-confidence proposal. The table is one of two signals the checker combines;
on its own it proposes foreign words and names (``sur``, ``Hass``), so
``checker`` only trusts an entry when the system spellchecker agrees.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

TYPOS_VERSION = "typos-dict-0.14.1"
DATA_SHA256 = "9f527a304e4ea717a4dc1b6fc5f3a4d50b8c872f17792097e28422bb595226db"
_DATA_PATH = Path(__file__).with_name("data") / "typos_words.csv"
_WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*\Z", re.UNICODE)


def _valid_word(value: str) -> bool:
    return bool(value and _WORD_RE.fullmatch(value) and any(char.isalpha() for char in value))


def load_rules(path: Path = _DATA_PATH) -> dict[str, str]:
    """Load only unambiguous, single-token lowercase rows from the vendored dataset."""
    rules: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8") as stream:
        for row in csv.reader(stream):
            if len(row) != 2:
                continue
            source, replacement = row
            if not _valid_word(source) or not _valid_word(replacement):
                continue
            if source.casefold() == replacement.casefold():
                continue
            rules[source] = replacement
    return rules
