"""Deterministic mechanical typo detection for note prose.

The correction list is a pinned copy of ``typos-dict`` 0.14.1.  Rows with
multiple corrections are intentionally ignored: an ambiguous alternative is
not a high-confidence proposal.  This module only emits exact source
occurrences; decisions, Markdown phrase protections, and review policy live
outside the detector.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from tool.notes import markdown

DETECTOR_VERSION = "typos-dict-0.14.1"
POLICY_VERSION = "notes-mechanical-v1"
DATA_SHA256 = "9f527a304e4ea717a4dc1b6fc5f3a4d50b8c872f17792097e28422bb595226db"
_DATA_PATH = Path(__file__).with_name("data") / "typos_words.csv"
_WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*\Z", re.UNICODE)
_TOKEN_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)


@dataclass(frozen=True, slots=True)
class Finding:
    """One exact detector proposal at a Python Unicode offset."""

    start: int
    end: int
    original: str
    replacement: str
    detector: str = DETECTOR_VERSION


def _valid_word(value: str) -> bool:
    return bool(value and _WORD_RE.fullmatch(value) and any(char.isalpha() for char in value))


def load_rules(path: Path = _DATA_PATH) -> dict[str, str]:
    """Load only unambiguous, single-token rows from the vendored dataset."""
    rules: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8") as stream:
        for row in csv.reader(stream):
            if len(row) != 2:
                continue
            source, replacement = row
            if not _valid_word(source) or not _valid_word(replacement):
                continue
            if source == replacement or source.casefold() == replacement.casefold():
                continue
            rules[source] = replacement
    return rules


def _identifier_char(char: str) -> bool:
    category = unicodedata.category(char)
    return char == "_" or char.isalnum() or category.startswith("M")


def _looks_like_identifier_boundary(text: str, start: int, end: int) -> bool:
    """Reject a dictionary word embedded in an identifier-like token."""
    if start > 0 and _identifier_char(text[start - 1]):
        return True
    if end < len(text) and _identifier_char(text[end]):
        return True
    # Hyphenated and dotted names are conservative exclusions.  Markdown
    # paths and URLs are already protected, but this covers bare identifiers.
    if start > 0 and text[start - 1] in "-.":
        return True
    return bool(end < len(text) and text[end] in "-.")


def _case_variant(source: str, replacement: str) -> str | None:
    """Apply only deliberate lower/title/upper case variants.

    Mixed-case source forms are abstained from rather than silently folded
    into a lowercase approval.  Curly apostrophes are preserved when the
    correction changes only the apostrophe glyph as part of a known word.
    """
    if source == source.casefold():
        result = replacement
    elif source.isupper():
        result = replacement.upper()
    elif source[:1].isupper() and source[1:] == source[1:].lower():
        result = replacement[:1].upper() + replacement[1:]
    else:
        return None
    if "’" in source and "'" in result:
        result = result.replace("'", "’")
    return result


class MechanicalTypoDetector:
    """Fast local detector backed by a curated exact correction map."""

    version = DETECTOR_VERSION
    policy_version = POLICY_VERSION

    def __init__(self, rules: dict[str, str] | None = None) -> None:
        self._rules = load_rules() if rules is None else dict(rules)

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    def replacement_for(self, source: str) -> str | None:
        direct = self._rules.get(source)
        if direct is not None:
            return direct
        folded = self._rules.get(source.casefold())
        if folded is None:
            return None
        return _case_variant(source, folded)

    def detect(self, text: str) -> list[Finding]:
        """Return source-order proposals outside the existing Markdown guards."""
        protected = markdown.find_protected_ranges(text)
        findings: list[Finding] = []
        for match in _TOKEN_RE.finditer(text):
            start, end = match.span()
            if _looks_like_identifier_boundary(text, start, end):
                continue
            if any(protected_start < end and start < protected_end for protected_start, protected_end in protected):
                continue
            original = match.group()
            replacement = self.replacement_for(original)
            if replacement is None or replacement == original or replacement.casefold() == original.casefold():
                continue
            if any(char.isspace() for char in replacement):
                continue
            findings.append(Finding(start, end, original, replacement))
        return findings
