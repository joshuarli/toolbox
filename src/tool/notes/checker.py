"""Precision-first spelling analysis for Markdown notes.

The fundamental rule is that *unknown does not mean misspelled*. A word is
left alone when any configured dictionary accepts it (union of English,
British English, and French by default), and an unknown word only becomes a
proposal when independent evidence says it is a typing slip:

1. **Known misspelling.** The pinned ``typos-dict`` table has an entry for it
   *and* the system spellchecker's first guess is the same word. Agreement of
   two independent sources is what makes these ``bulk_safe`` (eligible for a
   vault-wide ``A``).
2. **Single typing slip.** The system spellchecker's first guess is one
   plausible keystroke away (adjacent-key substitution, doubled or neighbouring
   extra key, dropped interior letter, or transposition), the first letter is
   unchanged, the guess list is short and unambiguous, and the unknown form is
   rare in the vault. These are note-local only.

Everything that looks like a name, identifier, or foreign vocabulary is
skipped before any correction is considered: capitalized words mid-sentence,
acronyms and mixed case, tokens glued to digits/underscores/dots/hyphens,
standalone emphasized labels, and fragments of text broken by PDF extraction.
Each sentence is attributed to the configured language most of its words
exclusively belong to, so French sentences are corrected with French guesses
and ``typos-dict`` (English) only applies to English sentences.

The vault is scanned once per content hash (``NoteScan``); per-word dictionary
verdicts and guesses are cached in the private state database, so reruns only
touch changed notes. Offsets are Python Unicode code-point offsets into the
exact decoded source.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tool.notes import markdown
from tool.notes.speller import Speller, SystemSpeller
from tool.notes.state import StateStore
from tool.notes.typos import TYPOS_VERSION, load_rules

# Part of the note-scan cache key: bump whenever ``NoteScan`` contents change.
CHECKER_VERSION = "notes-spell-v2"
# Accepting dictionaries, in priority order. The first entry's base language is
# the default sentence language; British spellings are valid English.
DEFAULT_LANGUAGES = ("en", "en_GB", "fr")
# A single-slip proposal needs a word this long; shorter typos are too easy to
# confuse with abbreviations and foreign words unless ``typos-dict`` knows them.
_MIN_SLIP_LENGTH = 5
# An unknown form used more often than this across the vault is the user's
# vocabulary (a name, product, or term), not a slip.
_MAX_SLIP_USES = 2
# Long guess lists mean the system checker is fishing, not correcting.
_MAX_GUESSES = 3
# A capitalized sentence-initial slip is only corrected to a word the vault
# already uses this often; otherwise it is as likely a brand or title.
_MIN_TITLE_TARGET_USES = 5
# Productive English affixes: an unknown word built from a known stem
# (``lagering``, ``virality``, ``unslipped``) is coinage, not a slip.
_SUFFIXES = (
    "ing", "ed", "er", "ers", "s", "es", "ly", "ity", "ness", "able", "y", "ish",
    "ify", "ize", "ise", "ful", "less", "ment", "ism", "ist",
)
_PREFIXES = ("un", "re", "over", "under", "pre", "non", "anti", "dis", "sub", "super", "multi", "micro")
_SYSTEM_WORDS = Path("/usr/share/dict/words")

_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")
# Punctuation allowed to wrap a plain prose word. Anything else in the same
# whitespace-delimited chunk (digits, ``_``, ``.``/``-`` joins, soft hyphens,
# combining marks, ``/``, ``@``...) makes the token part of an identifier or
# compound, which is never corrected.
_OPENERS = frozenset("([{\"'“‘*_~>¿¡")
_CLOSERS = frozenset(")]}\"'”’*_~,.;:!?…")
_WRAPPERS = "".join(sorted(_OPENERS | _CLOSERS))
# Characters skipped when looking back for a sentence boundary.
_LEAD_MARKUP = frozenset(" \t*_#>([{\"'“‘`~-+")
_SENTENCE_END_RE = re.compile(r"[.!?:]\s|\n")

_QWERTY_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")
_KEY_POSITION = {
    char: (row, column + 0.5 * row) for row, keys in enumerate(_QWERTY_ROWS) for column, char in enumerate(keys)
}


@dataclass(frozen=True, slots=True)
class Finding:
    """One proposed replacement at an exact source range of one note."""

    note_id: str
    start: int
    end: int
    line: int
    column: int
    original: str
    replacement: str
    reason: str
    language: str
    bulk_safe: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.note_id,
            "line": self.line,
            "column": self.column,
            "start": self.start,
            "end": self.end,
            "original": self.original,
            "replacement": self.replacement,
            "reason": self.reason,
            "language": self.language,
            "bulk_safe": self.bulk_safe,
        }


@dataclass(frozen=True, slots=True)
class Token:
    """An unknown prose word with the context the judge needs.

    ``skip`` is set when structure alone rules the word out (a likely name,
    identifier, or broken fragment); such tokens are kept only for ``--verbose``.
    """

    start: int
    end: int
    word: str
    language: str
    sentence_initial: bool
    skip: str | None = None


@dataclass(frozen=True, slots=True)
class NoteScan:
    """Content-hash-cached, vault-independent result of scanning one note."""

    content_hash: str
    # Exact word forms in the whole note, code included: vault-wide usage
    # separates the user's vocabulary from one-off slips.
    counts: dict[str, int]
    tokens: tuple[Token, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "counts": self.counts,
            "tokens": [[t.start, t.end, t.word, t.language, t.sentence_initial, t.skip] for t in self.tokens],
        }

    @classmethod
    def from_json(cls, content_hash: str, payload: dict[str, Any]) -> NoteScan:
        return cls(
            content_hash,
            {str(word): int(count) for word, count in payload["counts"].items()},
            tuple(Token(int(s), int(e), str(w), str(lang), bool(initial), skip) for s, e, w, lang, initial, skip in payload["tokens"]),
        )


@dataclass(frozen=True, slots=True)
class Verdict:
    """The judge's answer for one token: a replacement, or why there is none."""

    replacement: str | None
    reason: str
    bulk_safe: bool = False


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _base(language: str) -> str:
    return language.split("_", 1)[0].split("-", 1)[0].lower()


def _letters(value: str) -> str:
    """Casefolded letters without accents or apostrophes: equal ``_letters``
    means two spellings differ only in style (case, accents, contractions)."""
    decomposed = unicodedata.normalize("NFD", value.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch) and ch not in "'’")


def _keys_adjacent(a: str, b: str) -> bool:
    if a == b:
        return True
    if a not in _KEY_POSITION or b not in _KEY_POSITION:
        return False
    (row_a, col_a), (row_b, col_b) = _KEY_POSITION[a], _KEY_POSITION[b]
    return abs(row_a - row_b) <= 1 and abs(col_a - col_b) <= 1


def typing_slip(typo: str, correction: str) -> bool:
    """Return whether *typo* is one plausible keystroke error away from *correction*.

    Accepted: adjacent-key substitution, a duplicated or neighbouring extra key,
    a dropped letter, or two swapped letters — never at the first letter, and
    never at the last letter except for a swap. Word-final edits are usually
    legitimate morphology (``trainy``, ``lagern``), and first-letter edits are
    usually a different word entirely (``kent`` → ``sent``).
    """
    t, c = typo, correction
    if len(t) == len(c):
        diff = [i for i, (x, y) in enumerate(zip(t, c, strict=True)) if x != y]
        if len(diff) == 1:
            i = diff[0]
            return 0 < i < len(t) - 1 and _keys_adjacent(t[i], c[i])
        if len(diff) == 2:
            i, j = diff
            return i > 0 and j == i + 1 and t[i] == c[j] and t[j] == c[i]
        return False
    if len(t) == len(c) + 1:
        for i in range(1, len(t) - 1):
            if t[:i] + t[i + 1 :] == c:
                extra, left, right = t[i], t[i - 1], t[i + 1]
                if _keys_adjacent(extra, left) or _keys_adjacent(extra, right):
                    return True
        return False
    if len(t) + 1 == len(c):
        return any(c[:i] + c[i + 1 :] == t for i in range(1, len(c) - 1))
    return False


def _slip_sources(typo: str) -> Iterable[str]:
    """Every word *typo* could be a :func:`typing_slip` of (a superset: case is ignored)."""
    letters = "abcdefghijklmnopqrstuvwxyz"
    for i in range(1, len(typo)):
        for char in letters:
            yield typo[:i] + char + typo[i:]  # typo dropped a letter
    for i in range(1, len(typo) - 1):
        if _keys_adjacent(typo[i], typo[i - 1]) or _keys_adjacent(typo[i], typo[i + 1]):
            yield typo[:i] + typo[i + 1 :]  # typo has an extra key
        for char in letters:
            if _keys_adjacent(typo[i], char):
                yield typo[:i] + char + typo[i + 1 :]  # neighbouring key
        yield typo[:i] + typo[i + 1] + typo[i] + typo[i + 2 :]  # swapped letters


def _derived_from(word: str, vocabulary: set[str]) -> bool:
    """Whether *word* is a known stem plus a productive affix."""
    for suffix in _SUFFIXES:
        stem = word[: -len(suffix)]
        if word.endswith(suffix) and len(stem) >= 4 and (stem in vocabulary or stem + "e" in vocabulary):
            return True
    return any(word.startswith(prefix) and len(word) - len(prefix) >= 4 and word[len(prefix) :] in vocabulary for prefix in _PREFIXES)


def _recase(template: str, value: str) -> str:
    if template[:1].isupper():
        return value[:1].upper() + value[1:]
    return value


def _shape(word: str) -> str | None:
    """``lower`` or ``title``; ``None`` for acronyms and mixed case (names, brands, code)."""
    if word == word.lower():
        return "lower"
    if word[0].isupper() and word[1:] == word[1:].lower():
        return "title"
    return None


def _chunk(text: str, start: int, end: int) -> tuple[int, int]:
    """Whitespace-delimited chunk containing ``text[start:end]``."""
    left = start
    while left > 0 and not text[left - 1].isspace():
        left -= 1
    right = end
    while right < len(text) and not text[right].isspace():
        right += 1
    return left, right


def _next_chunk(text: str, position: int) -> str:
    """The following whitespace-delimited chunk on the same line, or ``""``."""
    index = position
    while index < len(text) and text[index] in " \t":
        index += 1
    if index >= len(text) or text[index] in "\r\n":
        return ""
    _, right = _chunk(text, index, index + 1)
    return text[index:right]


def _hyphenated_before(text: str, left: int) -> bool:
    """Whether the previous chunk ends in a letter and a hyphen (``tradi- tional``,
    possibly across a line break): this chunk is the tail of a split word."""
    index = left - 1
    while index >= 0 and text[index] in " \t\r\n":
        index -= 1
    return index > 0 and text[index] in "-\u00ad" and text[index - 1].isalpha()


def _sentence_initial(text: str, start: int) -> bool:
    index = start - 1
    while index >= 0 and text[index] in _LEAD_MARKUP:
        index -= 1
    return index < 0 or text[index] in "\n.!?:"


def _structural_skip(text: str, start: int, end: int, word: str, initial: bool) -> str | None:
    """Return why a token cannot be a correctable prose word, or ``None``."""
    shape = _shape(word)
    if shape is None:
        return "acronym or mixed case (likely a name, brand, or code)"
    left, right = _chunk(text, start, end)
    prefix, suffix = text[left:start], text[end:right]
    if any(char not in _OPENERS for char in prefix) or any(char not in _CLOSERS for char in suffix):
        return "part of an identifier, compound, or number"
    if prefix and suffix and prefix[-1] in "*_" and suffix[0] in "*_":
        return "standalone emphasized label (likely a name)"
    after = _next_chunk(text, right)
    after_core = after.strip(_WRAPPERS)
    if _hyphenated_before(text, left) or (len(after_core) == 1 and after_core.isalpha() and after_core not in "aAI"):
        return "fragment of a broken or hyphenated word"
    if shape == "title":
        if not initial:
            return "capitalized mid-sentence (likely a name)"
        if after[:1].isupper():
            return "part of a capitalized name"
    return None


# (start, end, word, sentence_initial) of one prose word outside protected Markdown.
_ProseWord = tuple[int, int, str, bool]


def _prose_sentences(text: str) -> list[list[_ProseWord]]:
    """Prose words outside protected Markdown regions, grouped into sentences."""
    protected = markdown.find_protected_ranges(text)
    sentences: list[list[_ProseWord]] = [[]]
    protected_index = 0
    previous_end = 0
    for match in _WORD_RE.finditer(text):
        start, end = match.span()
        while protected_index < len(protected) and protected[protected_index][1] <= start:
            protected_index += 1
        if protected_index < len(protected) and protected[protected_index][0] < end:
            continue
        if sentences[-1] and _SENTENCE_END_RE.search(text, previous_end, start):
            sentences.append([])
        sentences[-1].append((start, end, match.group(), _sentence_initial(text, start)))
        previous_end = end
    return [sentence for sentence in sentences if sentence]


class SpellChecker:
    """Vault-aware checker; call :meth:`prepare` once, then :meth:`analyze` per note."""

    def __init__(
        self,
        store: StateStore,
        speller: Speller | None = None,
        languages: tuple[str, ...] = DEFAULT_LANGUAGES,
        rules: Mapping[str, str] | None = None,
        system_words: Path | None = _SYSTEM_WORDS,
    ) -> None:
        if not languages:
            raise ValueError("at least one spelling language is required")
        self.store = store
        self.speller = speller or SystemSpeller()
        self.languages = languages
        self.primary = languages[0]
        self.rules = dict(load_rules() if rules is None else rules)
        self._system_words = system_words
        self._word_key = f"{self.speller.key}|{','.join(languages)}"
        self._scan_key = f"{CHECKER_VERSION}|{TYPOS_VERSION}|{self._word_key}"
        # Bit i set when languages[i] accepts the word.
        self._accepts: dict[str, int] = store.spelling_words(self._word_key)
        self._scans: dict[str, NoteScan] = {}
        self._counts: Counter[str] = Counter()
        self._vocabulary: set[str] | None = None
        self._known: dict[str, int] | None = None

    # -- dictionary verdicts -------------------------------------------------

    def _resolve(self, words: Iterable[str]) -> None:
        missing = {word for word in words if word not in self._accepts}
        if not missing:
            return
        masks = dict.fromkeys(missing, 0)
        for bit, language in enumerate(self.languages):
            rejected = self.speller.unknown(missing, language)
            for word in missing - rejected:
                masks[word] |= 1 << bit
        self._accepts.update(masks)
        self.store.put_spelling_words(self._word_key, masks)
        self._vocabulary = None
        self._known = None

    def accepted_by(self, word: str) -> list[str]:
        self._resolve([word])
        mask = self._accepts[word]
        return [language for bit, language in enumerate(self.languages) if mask >> bit & 1]

    def _guesses(self, word: str, language: str) -> list[str]:
        cached = self.store.spelling_guesses(self.speller.key, language, word)
        if cached is not None:
            return cached
        guesses = self.speller.guesses(word, language)
        self.store.put_spelling_guesses(self.speller.key, language, word, guesses)
        return guesses

    def _vocabulary_words(self) -> set[str]:
        """Known stems for :func:`_derived_from`: the system word list plus
        every vault word some configured dictionary accepts."""
        if self._vocabulary is None:
            vocabulary = {word.casefold() for word, mask in self._accepts.items() if mask}
            if self._system_words is not None:
                try:
                    vocabulary.update(line.strip().casefold() for line in self._system_words.open(encoding="utf-8"))
                except OSError:
                    pass
            self._vocabulary = vocabulary
        return self._vocabulary

    # -- scanning ------------------------------------------------------------

    def _scan(self, text: str, sentences: list[list[_ProseWord]] | None = None) -> NoteScan:
        if sentences is None:
            sentences = _prose_sentences(text)
            self._resolve(word for sentence in sentences for _s, _e, word, _i in sentence)
        accepts = self._accepts
        bases = [_base(language) for language in self.languages]
        base_masks = {base: sum(1 << i for i, b in enumerate(bases) if b == base) for base in dict.fromkeys(bases)}
        tokens: list[Token] = []
        for sentence in sentences:
            # Attribute the sentence to the base language owning the most
            # words no other configured language accepts; ties and weak
            # evidence keep the primary language.
            exclusive = dict.fromkeys(base_masks, 0)
            for _s, _e, word, _initial in sentence:
                owners = [base for base, bits in base_masks.items() if accepts[word] & bits]
                if len(owners) == 1:
                    exclusive[owners[0]] += 1
            best = max(exclusive, key=lambda base: (exclusive[base], base == bases[0]))
            language = self.primary
            if best != bases[0] and exclusive[best] >= 2 and exclusive[best] > exclusive[bases[0]]:
                language = self.languages[bases.index(best)]
            for start, end, word, initial in sentence:
                if not accepts[word]:
                    skip = _structural_skip(text, start, end, word, initial)
                    tokens.append(Token(start, end, word, language, initial, skip))
        return NoteScan(content_hash(text), dict(Counter(_WORD_RE.findall(text))), tuple(tokens))

    def prepare(self, notes: Mapping[str, str], log: Callable[[str], None] | None = None) -> None:
        """Scan every note (reusing cached scans) and build vault-wide word usage."""
        cached = self.store.note_scans(self._scan_key)
        scans: dict[str, NoteScan] = {}
        stale: dict[str, list[list[_ProseWord]]] = {}
        for note_id, text in notes.items():
            digest = content_hash(text)
            hit = cached.get(note_id)
            if hit is not None and hit[0] == digest:
                scans[note_id] = NoteScan.from_json(digest, hit[1])
            else:
                stale[note_id] = _prose_sentences(text)
        if stale:
            if log is not None:
                log(f"Scanning {len(stale)} new or changed note(s) of {len(notes)}...")
            # One batched dictionary lookup for every stale note's prose words.
            self._resolve(word for sentences in stale.values() for sentence in sentences for _s, _e, word, _i in sentence)
            fresh = {note_id: self._scan(notes[note_id], sentences) for note_id, sentences in stale.items()}
            self.store.put_note_scans(
                self._scan_key, {note_id: (scan.content_hash, scan.to_json()) for note_id, scan in fresh.items()}
            )
            scans.update(fresh)
        self._scans = scans
        self._counts = Counter()
        for scan in scans.values():
            self._counts.update(scan.counts)
        self._known = None

    def scan(self, note_id: str, text: str) -> NoteScan:
        scan = self._scans.get(note_id)
        if scan is None or scan.content_hash != content_hash(text):
            scan = self._scan(text)
            self._scans[note_id] = scan
        return scan

    # -- judging -------------------------------------------------------------

    def _uses(self, word: str) -> int:
        """Vault-wide uses of a lowercase word in lower, title, or upper case."""
        forms = {word, word[:1].upper() + word[1:], word.upper()}
        return sum(self._counts[form] for form in forms)

    def _known_uses(self) -> dict[str, int]:
        """Lowercased dictionary-accepted vault words → vault uses, for the slip gate."""
        if self._known is None:
            known: dict[str, int] = {}
            for form, count in self._counts.items():
                if self._accepts.get(form):
                    key = form.lower()
                    known[key] = known.get(key, 0) + count
            self._known = known
        return self._known

    def judge(self, token: Token) -> Verdict:
        """Decide one unknown token; the reason explains either outcome."""
        if token.skip is not None:
            return Verdict(None, token.skip)
        word = token.word
        subject = word.lower() if token.sentence_initial and word[:1].isupper() else word
        language = token.language
        english = _base(language) == _base(self.primary) == "en"
        known = self.rules.get(subject) if english else None
        uses = self._uses(subject)
        title = subject != word
        if known is not None and _letters(known) == _letters(subject):
            return Verdict(None, f"typos-dict suggests {known!r}, a case/accent/apostrophe-only change")
        if known is not None:
            if title and self._counts[word] > _MAX_SLIP_USES:
                return Verdict(None, f"capitalized form used {self._counts[word]} times in the vault (likely a title or name)")
            guesses = self._guesses(subject, language)
            if guesses[:1] == [known]:
                return Verdict(_recase(word, known), "known misspelling (typos-dict), confirmed by system spellchecker", True)
            return Verdict(None, f"typos-dict suggests {known!r} but system spellchecker suggests {guesses[:3]}")
        if len(subject) < _MIN_SLIP_LENGTH:
            return Verdict(None, "too short to correct without a curated entry")
        if uses > _MAX_SLIP_USES:
            return Verdict(None, f"used {uses} times in the vault (treated as vocabulary)")
        # The target must be a word the user actually writes; a capitalized
        # form needs more support because it is as likely a brand or title.
        needed = _MIN_TITLE_TARGET_USES if title else int(english)
        if english and _derived_from(subject, self._vocabulary_words()):
            return Verdict(None, "known stem with a productive affix (likely a coinage)")
        # Cheap local gate before the slow system guess: some accepted word
        # one plausible slip away must already have enough vault uses.
        known = self._known_uses()
        if needed and not any(known.get(source, 0) >= needed for source in _slip_sources(subject)):
            return Verdict(None, f"no known word used {needed}+ time(s) in the vault is one typing slip away")
        guesses = self._guesses(subject, language)
        if not guesses:
            return Verdict(None, f"no {language} suggestion")
        if len(guesses) > _MAX_GUESSES:
            return Verdict(None, f"ambiguous: {len(guesses)} {language} suggestions")
        best = guesses[0]
        if not typing_slip(subject, best):
            return Verdict(None, f"{language} suggestion {best!r} is not a single typing slip")
        rivals = [guess for guess in guesses[1:] if typing_slip(subject, guess)]
        if rivals:
            return Verdict(None, f"ambiguous: {best!r} or {rivals[0]!r}")
        if _letters(best) == _letters(subject):
            return Verdict(None, f"{best!r} is a case/accent/apostrophe-only change")
        if self._uses(best) < needed:
            return Verdict(None, f"suggestion {best!r} is used {self._uses(best)} time(s) in the vault; needs {needed}")
        unknown_in = ", ".join(self.languages)
        return Verdict(_recase(word, best), f"unknown in {unknown_in}; {language} suggestion is one typing slip away")

    def analyze(self, note_id: str, text: str, only: frozenset[str] | None = None) -> list[Finding]:
        """Return source-ordered proposals for one note.

        ``only`` restricts judging to those exact word forms, so a vault-wide
        batch plan does not request guesses for unrelated words.
        """
        findings: list[Finding] = []
        for token in self.scan(note_id, text).tokens:
            if token.skip is not None or (only is not None and token.word not in only):
                continue
            verdict = self.judge(token)
            if verdict.replacement is None or verdict.replacement == token.word:
                continue
            line = text.count("\n", 0, token.start) + 1
            column = token.start - (text.rfind("\n", 0, token.start) + 1) + 1
            findings.append(
                Finding(
                    note_id,
                    token.start,
                    token.end,
                    line,
                    column,
                    token.word,
                    verdict.replacement,
                    verdict.reason,
                    token.language,
                    verdict.bulk_safe,
                )
            )
        return findings

    def explain_note(self, note_id: str, text: str) -> list[tuple[Token, Verdict]]:
        """Every unknown token of one note with its verdict, for ``--verbose``."""
        return [(token, self.judge(token)) for token in self.scan(note_id, text).tokens]

    def explain_word(self, word: str) -> list[str]:
        """Human-readable decision trail for one word as sentence-initial English prose."""
        lines = [word]
        accepted = self.accepted_by(word)
        lines.extend(f"  {language}: {'accepted' if language in accepted else 'unknown'}" for language in self.languages)
        if accepted:
            lines.append("  → accepted; never flagged")
            return lines
        subject = word.lower() if _shape(word) == "title" else word
        if subject in self.rules:
            lines.append(f"  typos-dict: {subject} → {self.rules[subject]}")
        lines.append(f"  vault uses: {self._uses(subject)}")
        # One representative dictionary per base language (en, fr, ...).
        representatives = {_base(language): language for language in reversed(self.languages)}
        for language in representatives.values():
            lines.append(f"  {language} suggestions: {self._guesses(subject, language)[:5]}")
            verdict = self.judge(Token(0, len(word), word, language, sentence_initial=True))
            outcome = f"{word} → {verdict.replacement}" if verdict.replacement else "no proposal"
            lines.append(f"  in a {language} sentence: {outcome} ({verdict.reason})")
        return lines
