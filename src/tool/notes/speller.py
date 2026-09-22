"""macOS system spellchecker (``NSSpellChecker``) adapter for ``notes spellcheck``.

The system checker is the vocabulary authority: it knows English and French
morphology, many names and places, and every word the user has taught macOS
("Learn Spelling"). It is never asked to identify languages itself — automatic
identification silently accepts misspellings such as ``bonjor`` — so every
query names an explicit language.

Per-word queries go through the ``AppleSpell`` XPC service. Checking a
newline-joined batch costs ~25 µs per word, while ``guesses`` costs ~10 ms per
word and does not parallelize, so callers batch acceptance checks and request
guesses only for the few words that survive every cheaper filter.
"""

from __future__ import annotations

import bisect
import hashlib
import platform
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from tool.notes.common import NotesError

# Words the user taught macOS via "Learn Spelling"; its mtime is part of the
# cache identity so learned words take effect on the next run.
_LOCAL_DICTIONARY = Path.home() / "Library/Spelling/LocalDictionary"


class Speller(Protocol):
    """Word-level vocabulary oracle; ``key`` identifies its answers for caching."""

    key: str

    def unknown(self, words: Iterable[str], language: str) -> set[str]:
        """Return the subset of *words* the dictionary for *language* rejects."""
        ...

    def guesses(self, word: str, language: str) -> list[str]:
        """Return ranked correction guesses for *word* in *language*."""
        ...


class SystemSpeller:
    """``NSSpellChecker`` with explicit per-query languages."""

    def __init__(self) -> None:
        try:
            from AppKit import NSSpellChecker
        except ImportError as exc:
            raise NotesError("spellcheck requires macOS AppKit (pyobjc-framework-Cocoa)") from exc
        self._checker = NSSpellChecker.sharedSpellChecker()
        try:
            learned = str(_LOCAL_DICTIONARY.stat().st_mtime_ns)
        except OSError:
            learned = "none"
        identity = f"{platform.mac_ver()[0]}:{learned}"
        self.key = "nsspell:" + hashlib.sha256(identity.encode()).hexdigest()[:16]

    def unknown(self, words: Iterable[str], language: str) -> set[str]:
        batch = sorted(set(words))
        if not batch:
            return set()
        # One newline-joined string per batch; each misspelled range is mapped
        # back to its whole input line, so the checker's own word segmentation
        # (for example around apostrophes) cannot produce a word we never sent.
        starts: list[int] = []
        offset = 0
        for word in batch:
            starts.append(offset)
            offset += len(word) + 1
        joined = "\n".join(batch)
        rejected: set[str] = set()
        position = 0
        while position < len(joined):
            found = self._checker.checkSpellingOfString_startingAt_language_wrap_inSpellDocumentWithTag_wordCount_(
                joined, position, language, False, 0, None
            )[0]
            if found.length == 0:
                break
            index = bisect.bisect_right(starts, found.location) - 1
            rejected.add(batch[index])
            # One rejected piece condemns the whole word, so skip the rest of
            # it. Resume at its trailing newline: a search starting exactly at
            # the first letter of the next word misses that word.
            position = starts[index] + len(batch[index])
        return rejected

    def guesses(self, word: str, language: str) -> list[str]:
        found = self._checker.guessesForWordRange_inString_language_inSpellDocumentWithTag_(
            (0, len(word)), word, language, 0
        )
        return [str(guess) for guess in found or ()]
