"""Interactively spellcheck the whole Obsidian vault with Apple's on-device model.

All linguistic judgment comes from the local Foundation Models model, which
acts purely as a *discovery mechanism*: it proposes small ``(block_id,
original, replacement, occurrence)`` edits, Python maps each one back to an
exact source range (see :func:`validate_corrections`), and groups validated
proposals by ``(original, replacement)`` into review candidates.

Deterministic scanning then *expands* each candidate mapping to every safe
occurrence of ``original`` in eligible vault prose. The human approves each
mapping interactively; approved mappings apply everywhere safe without
further AI calls. Up to three discovery passes run until no new candidates
appear.

Discovery packs prose from *many* notes into each model request
(:func:`plan_chunks`). On-device generation costs seconds per call almost
independently of how much prose the call carries, so a request per note — or
worse, per paragraph — wastes nearly all of its cost on overhead. Each unit
remembers the note it came from, so a chunk spanning notes still maps every
edit back to its exact source range (:func:`validate_chunk`).

Each pass streams file-groups in path order: discover one group, review its
candidates immediately, then move on. An approved mapping applies vault-wide
at once, so later groups are read from text that already contains earlier
fixes — a typo approved early is never re-reported later, and the first
prompt arrives after the first productive group instead of after the whole
vault scan. No materialized units survive a review boundary, so later offsets
can never go stale under review edits.

Usage::

    tool notes spellcheck
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import os
import re
import sys
import tempfile
import time
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

from tool.notes import markdown
from tool.notes.common import VAULT, ContextTooLargeError, NotesError, resolve_vault
from tool.notes.foundation_models import (
    ModelClient,
    ProposedCorrection,
    create_live_client,
    render_prompt,
)
from tool.notes.localize import collect_markdown_files
from tool.notes.markdown import Block

# A validated edit is word-scale by construction; anything longer is a
# rewrite attempt, not a spelling correction, and is rejected outright.
_MAX_EDIT_CHARS = 200
# Word boundaries for trimming a quoted proposal down to the span that differs.
_WORD_RE = re.compile(r"\S+")
# A spelling correction changes letters, never Markdown structure. Any proposal
# that adds or drops one of these characters is a markup edit wearing a typo's
# clothes — `_frappato_ → frappato` strips emphasis, `a|b → ab` breaks a table
# cell — so the counts must match exactly on both sides.
_MARKUP_CHARS = "_*`#~[]<>|\\$"
# Context-window reserve covering instructions, guided-generation schema,
# prompt framing, and response headroom; prompts never fill the window.
# Fallback overhead estimate when exact measurement fails (instructions +
# schema measure ~779 tokens on current models, plus framing).
_FALLBACK_OVERHEAD_TOKENS = 1024
# Headroom for the generated corrections list. A section yielding more
# corrections than this overflows the window and is split further on retry.
_RESPONSE_RESERVE_TOKENS = 512
# Halvings attempted after a context-window overflow before a section is
# skipped with a loud warning (split further → fewer corrections per call).
_MAX_SECTION_SPLITS = 3
# Conservative chars-per-token guess used only when exact token counting fails.
_FALLBACK_CHARS_PER_TOKEN = 3
# Units sampled to calibrate the packing ratio (a few thousand characters is
# plenty for a stable estimate; the exact check still governs every chunk).
_RATIO_SAMPLE_UNITS = 12
# Chunks between progress lines when stderr is not a terminal.
_PROGRESS_INTERVAL = 25
# Context characters shown on each side of an occurrence in review output.
_CONTEXT_RADIUS = 100
_RED = "\033[31m"
_GREEN = "\033[32m"
_RESET = "\033[0m"
# Upper bound on discovery passes; generative inference is nondeterministic,
# so a fixed cap guarantees convergence.
_MAX_PASSES = 3
# Occurrences sampled per candidate display, preferably from distinct files.
_SAMPLE_SIZE = 3
# A token occurring more than this many times vault-wide is established
# vocabulary, not a typo waiting to happen. A single flagged context for
# such a token is a context-dependent judgment (observed live: grammar such
# as `say → says`), never a vault-wide typo — a real one would be flagged
# everywhere the model looks.
_ESTABLISHED_OCCURRENCES = 100
# Independent flagged contexts required before an established token is
# surfaced at all. One context is a judgment call; two is the start of a
# pattern.
_MIN_EVIDENCE_FOR_ESTABLISHED = 2


@dataclass(frozen=True, slots=True)
class AcceptedCorrection:
    """A validated edit: replace ``original`` in exactly ``[start:end)``."""

    block_id: int
    original: str
    replacement: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class ModelEvidence:
    """One validated model proposal, mapped to an exact range in one note."""

    path: Path
    start: int
    end: int
    original: str
    replacement: str


@dataclass(frozen=True, slots=True)
class VaultOccurrence:
    """One exact safe occurrence of a candidate spelling in eligible prose."""

    path: Path
    start: int
    end: int


@dataclass
class Candidate:
    """One ``original → replacement`` mapping under interactive review."""

    original: str
    replacement: str
    evidence: list[ModelEvidence] = field(default_factory=list)
    occurrences: list[VaultOccurrence] = field(default_factory=list)


@dataclass
class ReviewStats:
    """Rigorous session counts for the final summary."""

    occurrences_corrected: int = 0
    notes_modified: set[Path] = field(default_factory=set)
    candidates_skipped: int = 0
    spellings_ignored: int = 0
    passes: int = 0


class Reviewer(Protocol):
    """Decides one candidate's fate; tests inject scripted implementations."""

    def decide(self, candidate: Candidate) -> str:
        """Return one of ``a`` (all), ``y`` (one), ``s`` (skip spelling), ``n`` (next), ``q`` (quit)."""
        ...


async def _measured_tokens(client: ModelClient, text: str) -> int:
    """Exact token count, with a conservative estimate if counting fails.

    Correctness never depends on the count (it only sizes chunks), so a
    failed measurement degrades to an overestimate rather than an error.
    """
    try:
        return max(1, await client.count_tokens(text))
    except Exception:
        return max(1, len(text) // _FALLBACK_CHARS_PER_TOKEN)


async def _prompt_budget(client: ModelClient) -> int:
    """Prompt token budget: context minus measured overhead minus response headroom."""
    try:
        overhead = await client.prompt_overhead_tokens()
    except Exception:
        overhead = _FALLBACK_OVERHEAD_TOKENS
    budget = client.context_size - overhead - _RESPONSE_RESERVE_TOKENS
    if budget < 1:
        raise NotesError(
            f"Foundation Models context window ({client.context_size} tokens) is too small for spellchecking."
        )
    return budget


@dataclass(frozen=True, slots=True)
class Unit:
    """One paragraph-scale prose unit, bound to the note it was read from.

    ``start``/``end`` are note-local source offsets, exactly like
    :class:`markdown.Block`; ``path`` is what lets a chunk mix notes and still
    map every edit home. ``text`` is the *masked* source slice the model sees
    (see :func:`mask_protected`) — same length as the source, so offsets need no
    translation. A unit only acquires a ``block_id`` when its chunk is rendered.
    """

    path: Path
    start: int
    end: int
    text: str


def mask_protected(text: str, protected: Sequence[tuple[int, int]]) -> str:
    """Blank protected spans in place, keeping every offset identical.

    The model is asked to judge prose, so it is shown prose and nothing else:
    URLs, slash-bearing paths, link and image destinations, code spans, math,
    and HTML become runs of spaces. Those regions are ~13% of the prose the
    model would otherwise read and ~15% of its tokens, and they are exactly the
    text it must never edit, so removing them is both cheaper and less
    distracting than asking it to ignore them.

    Offsets are preserved character for character, so a match the model reports
    still lands on the same source range with no translation table — the
    invariant the whole validator rests on. Newlines survive so the shape of
    the prose it *can* edit is unchanged.
    """
    if not protected:
        return text
    buf = list(text)
    for start, end in protected:
        for index in range(start, end):
            if buf[index] not in "\n\r":
                buf[index] = " "
    return "".join(buf)


def _blocks_of(chunk: Sequence[Unit]) -> list[Block]:
    """Render a chunk's units as model-visible blocks with chunk-local ids."""
    return [Block(block_id=i, start=unit.start, end=unit.end, text=unit.text) for i, unit in enumerate(chunk, 1)]


async def _chunk_tokens(client: ModelClient, chunk: Sequence[Unit]) -> int:
    return await _measured_tokens(client, render_prompt(_blocks_of(chunk)))


def _split_unit(unit: Unit) -> list[Unit]:
    """Halve one unit at a sensible textual seam; single piece if unsplittable."""
    pieces = markdown.split_range(unit.text, 0, len(unit.text), max(1, len(unit.text) // 2))
    if len(pieces) <= 1:
        return [unit]
    return [
        Unit(path=unit.path, start=unit.start + start, end=unit.start + end, text=unit.text[start:end])
        for start, end in pieces
    ]


def _halve(chunk: list[Unit]) -> list[list[Unit]]:
    """Split a chunk into two, or into one unit's pieces when it holds a single unit."""
    if len(chunk) > 1:
        middle = len(chunk) // 2
        return [chunk[:middle], chunk[middle:]]
    return [[piece] for piece in _split_unit(chunk[0])]


async def _fit_chunk(chunk: list[Unit], budget: int, client: ModelClient) -> list[list[Unit]]:
    """Split *chunk* until each piece's rendered prompt fits *budget*; never drop text."""
    if await _chunk_tokens(client, chunk) <= budget:
        return [chunk]
    halves = _halve(chunk)
    if len(halves) <= 1:
        # Unsplittable unit (practically impossible at these budgets): leave it,
        # and let generation surface a clear context error.
        return [chunk]
    fitted: list[list[Unit]] = []
    for half in halves:
        fitted.extend(await _fit_chunk(half, budget, client))
    return fitted


async def _chars_per_token(units: Sequence[Unit], client: ModelClient) -> float:
    """Calibrate a chars-per-token ratio from a sample of the corpus.

    Packing needs a size estimate for every unit, and measuring each one exactly
    would cost thousands of round trips before a single correction is found. One
    sampled measurement is enough, because every assembled chunk is still
    verified with exact counting before it is sent.
    """
    sample = "\n\n".join(unit.text for unit in units[:_RATIO_SAMPLE_UNITS])
    if not sample:
        return float(_FALLBACK_CHARS_PER_TOKEN)
    try:
        tokens = await client.count_tokens(sample)
    except Exception:
        return float(_FALLBACK_CHARS_PER_TOKEN)
    if tokens <= 0:
        return float(_FALLBACK_CHARS_PER_TOKEN)
    return max(1.0, len(sample) / tokens)


async def plan_chunks(units: Sequence[Unit], client: ModelClient) -> list[list[Unit]]:
    """Pack prose units from any notes into model requests that fit the window.

    On-device generation costs seconds per call almost independently of how much
    prose the call carries, so one request covers as much coherent text as the
    context window allows rather than one note at a time. Budgets come from the
    real ``context_size`` and measured instruction/schema overhead — no
    hard-coded model sizes. Every packed chunk is re-measured exactly and split
    while it does not fit, so no text is skipped and no request overflows.
    """
    if not units:
        return []
    budget = await _prompt_budget(client)
    ratio = await _chars_per_token(units, client)
    packed: list[list[Unit]] = []
    current: list[Unit] = []
    used = 0
    for unit in units:
        size = int(len(unit.text) / ratio) + 1
        if current and used + size > budget:
            packed.append(current)
            current, used = [], 0
        current.append(unit)
        used += size
    if current:
        packed.append(current)
    chunks: list[list[Unit]] = []
    for group in packed:
        chunks.extend(await _fit_chunk(group, budget, client))
    return chunks


def _within_distance(one: str, two: str, limit: int) -> bool:
    """Bounded Levenshtein check: True when the edit distance is within *limit*."""
    if one == two:
        return True
    if abs(len(one) - len(two)) > limit:
        return False
    prev = list(range(len(two) + 1))
    for i, char_one in enumerate(one, 1):
        current = [i]
        row_min = i
        for j, char_two in enumerate(two, 1):
            cost = 0 if char_one == char_two else 1
            value = min(prev[j] + 1, current[j - 1] + 1, prev[j - 1] + cost)
            current.append(value)
            row_min = min(row_min, value)
        if row_min > limit:
            return False
        prev = current
    return prev[-1] <= limit


def _markup_signature(text: str) -> tuple[int, ...]:
    """Count of each Markdown-significant character in *text*."""
    return tuple(text.count(char) for char in _MARKUP_CHARS)


def _minimal_span(original: str, replacement: str) -> tuple[str, str, int]:
    """Trim shared leading/trailing words, returning ``(original, replacement, offset)``.

    The instructions ask the model to quote a few surrounding words so a short
    typo stays unambiguous, and it does — but a candidate is a *mapping*, and
    grouping is keyed on it. If the same typo is quoted as "the recieve queue"
    in one note and "a recieve" in another, those would become two separate
    candidates and the same typo would be adjudicated twice. Reducing every
    accepted proposal to the span that actually differs keeps one typo to one
    candidate. ``offset`` is where that span starts within ``original``.

    Only whole identical words are trimmed from either end, so every trimmed
    mapping is a strict sub-span of one that already passed validation.
    """
    old_words = list(_WORD_RE.finditer(original))
    new_words = list(_WORD_RE.finditer(replacement))
    if not old_words or len(old_words) != len(new_words):
        return original, replacement, 0
    first = 0
    while first < len(old_words) and old_words[first].group() == new_words[first].group():
        first += 1
    if first == len(old_words):
        return original, replacement, 0  # identical; rejected as a no-op elsewhere
    last = len(old_words) - 1
    while last > first and old_words[last].group() == new_words[last].group():
        last -= 1
    return (
        original[old_words[first].start() : old_words[last].end()],
        replacement[new_words[first].start() : new_words[last].end()],
        old_words[first].start(),
    )


def _spelling_key(word: str) -> str:
    """Casefolded letters/digits plus normalized interior apostrophe.

    Spelling-only gate: a correction must change this key. Stripping
    punctuation such as ``cuffs..? → cuffs.`` leaves ``cuffs`` on both
    sides and is rejected, while ``dont → don't`` (``dont`` vs ``don't``)
    still counts as spelling.
    """
    normalized = word.replace("’", "'").replace("‘", "'")
    return "".join(char for char in normalized.casefold() if char.isalnum() or char == "'")


def _punct_skeleton(word: str) -> str:
    """Punctuation in *word* other than the spelling apostrophe.

    A spelling fix may add/remove an interior apostrophe but must leave
    every other punctuation mark untouched, so ``teh → the!`` is rejected
    even though its letters change.
    """
    normalized = word.replace("’", "'").replace("‘", "'")
    return "".join(char for char in normalized if not char.isalnum() and char != "'")


def _is_spelling_change(original: str, replacement: str) -> bool:
    """True when every changed token changes spelling, not just punctuation.

    Token counts must match; each differing token must change its
    :func:`_spelling_key` (rejecting punctuation-only and case-only edits
    such as ``cuffs..? → cuffs.`` or ``Hello → hello``) while keeping its
    non-apostrophe punctuation identical (rejecting ``teh → the!``).
    """
    old_words = original.split()
    new_words = replacement.split()
    if len(old_words) != len(new_words) or not old_words:
        return False
    changed = False
    for old, new in zip(old_words, new_words, strict=True):
        if old == new:
            continue
        changed = True
        if _spelling_key(old) == _spelling_key(new):
            return False
        if _punct_skeleton(old) != _punct_skeleton(new):
            return False
    return changed


def _minimal_substitution(original: str, replacement: str) -> bool:
    """True when *replacement* only tweaks letters inside the same words.

    Token counts must match and every changed token must sit within edit
    distance 2 of its counterpart — the shape of a typo fix, not a word
    swap. This is structural similarity, not spelling knowledge: it stops
    instruction-following rewrites (``Qwen3.5 → ChatGPT``) while allowing
    genuine fixes (``recieve → receive``, ``teh → the``).
    """
    old_words = original.split()
    new_words = replacement.split()
    if len(old_words) != len(new_words) or not old_words:
        return False
    changed = False
    for old, new in zip(old_words, new_words, strict=True):
        if old == new:
            continue
        changed = True
        if not _within_distance(old, new, 2):
            return False
    return changed


def _validate_one(
    text: str,
    by_id: dict[int, Block],
    protected: list[tuple[int, int]],
    proposal: ProposedCorrection,
) -> tuple[int, int, str, str] | None:
    """Map one proposal to ``(start, end, original, replacement)``, or reject it (None).

    ``original``/``replacement`` are the proposal trimmed to its minimal
    differing span, so the range refers to that span rather than to any
    surrounding words the model quoted for context.
    """
    block_id, occurrence = proposal.block_id, proposal.occurrence
    original, replacement = proposal.original, proposal.replacement
    if type(block_id) is not int or type(occurrence) is not int:
        return None
    if type(original) is not str or type(replacement) is not str:
        return None
    block = by_id.get(block_id)
    if block is None:
        return None
    if not original or not replacement or original == replacement:
        return None
    if original.strip() == replacement.strip():
        return None
    # Homoglyph-class no-ops (fullwidth lookalikes, ligatures): if the change
    # vanishes under compatibility folding, nothing real would change except
    # the byte sequence itself.
    if unicodedata.normalize("NFKC", original) == unicodedata.normalize("NFKC", replacement):
        return None
    if "\n" in original or "\r" in original or "\n" in replacement or "\r" in replacement:
        return None
    if len(original) > _MAX_EDIT_CHARS or len(replacement) > _MAX_EDIT_CHARS:
        return None
    if not any(char.isalnum() for char in original):
        return None
    if _markup_signature(original) != _markup_signature(replacement):
        return None
    if not _minimal_substitution(original, replacement):
        return None
    if not _is_spelling_change(original, replacement):
        return None
    if occurrence < 1:
        return None
    # Exact substring search only: never fuzzy-match, never find-closest.
    # Occurrence selects among repeated matches. A unique match is
    # unambiguous on its own: the small on-device model miscounts
    # occurrences often enough that rejecting a proven-unique range over a
    # wrong number would trade real fixes for no safety gain (the exact
    # match itself is the proof). Repeated matches still require a valid
    # claimed occurrence.
    offsets: list[int] = []
    cursor = 0
    while True:
        found = block.text.find(original, cursor)
        if found == -1:
            break
        offsets.append(found)
        cursor = found + len(original)
    if not offsets:
        return None
    if len(offsets) == 1:
        chosen = offsets[0]
    else:
        if occurrence < 1 or occurrence > len(offsets):
            return None
        chosen = offsets[occurrence - 1]
    start = block.start + chosen
    end = start + len(original)
    if text[start:end] != original:
        return None
    if any(prot_start < end and start < prot_end for prot_start, prot_end in protected):
        return None
    # Quote-trimming happens last, on a range already proven to be real source
    # text: identical words are removed from both ends, so the trimmed span is
    # still exact and still inside every boundary just checked.
    minimal_original, minimal_replacement, offset = _minimal_span(original, replacement)
    if not _is_spelling_change(minimal_original, minimal_replacement):
        return None
    return start + offset, start + offset + len(minimal_original), minimal_original, minimal_replacement


def validate_corrections(
    text: str,
    blocks: Sequence[Block],
    protected: Sequence[tuple[int, int]],
    proposals: Sequence[ProposedCorrection],
) -> list[AcceptedCorrection]:
    """Keep only proposals that independently prove an exact, safe source range.

    Exact-duplicate proposals collapse to one edit; proposals overlapping an
    accepted range are rejected rather than guessed between — except competing
    replacements for the identical range, which are both kept so grouping can
    surface them as distinct candidates. Malformed or partially bad model
    output can never endanger the rest of the file.
    """
    by_id = {block.block_id: block for block in blocks}
    ranges = list(protected)
    accepted: list[AcceptedCorrection] = []
    seen: set[tuple[int, int, str, str]] = set()
    for proposal in proposals:
        mapped = _validate_one(text, by_id, ranges, proposal)
        if mapped is None:
            continue
        start, end, original, replacement = mapped
        key = (start, end, original, replacement)
        if key in seen:
            continue
        if any(
            start < other_end and other_start < end
            for other_start, other_end, _, _ in seen
            if (other_start, other_end) != (start, end)
        ):
            continue
        seen.add(key)
        accepted.append(
            AcceptedCorrection(
                block_id=proposal.block_id,
                original=original,
                replacement=replacement,
                start=start,
                end=end,
            )
        )
    return sorted(accepted, key=lambda edit: edit.start)


def _is_word_char(char: str) -> bool:
    """Identifier-constituent test for lexical boundaries (Unicode-safe)."""
    return char == "_" or char.isalnum()


def find_eligible_occurrences(text: str, original: str, protected: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Every exact safe occurrence of *original* in eligible prose.

    Matches inside larger identifier-like tokens (``recievePacket``) and
    inside protected Markdown regions are excluded. This is structural
    safety, not linguistic judgment: it only executes mappings the model
    proposed and the user approved.
    """
    if not original:
        return []
    found_ranges: list[tuple[int, int]] = []
    cursor = 0
    while True:
        found = text.find(original, cursor)
        if found == -1:
            break
        end = found + len(original)
        prev = text[found - 1] if found > 0 else ""
        nxt = text[end] if end < len(text) else ""
        if (
            (prev and _is_word_char(prev))
            or (nxt and _is_word_char(nxt))
            or any(prot_start < end and found < prot_end for prot_start, prot_end in protected)
        ):
            pass
        else:
            found_ranges.append((found, end))
        cursor = end
    return found_ranges


def apply_corrections(text: str, edits: Sequence[AcceptedCorrection]) -> str:
    """Splice validated non-overlapping edits into *text*; every other byte is preserved."""
    ordered = sorted(edits, key=lambda edit: edit.start)
    for first, second in itertools.pairwise(ordered):
        if second.start < first.end:
            raise ValueError("overlapping edits cannot be applied together")
    parts: list[str] = []
    cursor = 0
    for edit in ordered:
        parts.append(text[cursor : edit.start])
        parts.append(edit.replacement)
        cursor = edit.end
    parts.append(text[cursor:])
    return "".join(parts)


def _splice_ranges(text: str, ranges: Sequence[tuple[int, int]], replacement: str) -> str:
    """Replace exact ``ranges`` with ``replacement``; nothing else changes."""
    parts: list[str] = []
    cursor = 0
    for start, end in sorted(ranges):
        parts.append(text[cursor:start])
        parts.append(replacement)
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def _display_chunk(text: str) -> str:
    """Collapse newlines/tabs so a context window stays on one review line."""
    return text.replace("\r", " ").replace("\n", " ").replace("\t", " ")


def snippet_diff_lines(
    text: str, start: int, end: int, replacement: str, radius: int = _CONTEXT_RADIUS
) -> tuple[str, str]:
    """Diff-style context around ``[start:end)``: 100 chars each side.

    Returns ``(old, new)`` strings sharing the same before/after window,
    with the original highlighted red and the replacement green. Callers
    add their own ``-``/``+`` prefixes and quoting.
    """
    before_start = max(0, start - radius)
    after_end = min(len(text), end + radius)
    before = _display_chunk(text[before_start:start])
    after = _display_chunk(text[end:after_end])
    original = _display_chunk(text[start:end])
    repl = _display_chunk(replacement)
    prefix = "..." if before_start > 0 else ""
    suffix = "..." if after_end < len(text) else ""
    old = f"{prefix}{before}{_RED}{original}{_RESET}{after}{suffix}"
    new = f"{prefix}{before}{_GREEN}{repl}{_RESET}{after}{suffix}"
    return old, new


def snippet_for(text: str, start: int, end: int, width: int = _CONTEXT_RADIUS) -> str:
    """One trimmed source line around ``[start:end)`` for human context.

    Retained for compatibility; review output now uses
    :func:`snippet_diff_lines`, which shows ``_CONTEXT_RADIUS`` characters
    on each side with red/green highlighting instead of a single
    line-based window.
    """
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end == -1:
        line_end = len(text)
    line = text[line_start:line_end].strip().replace("\t", " ")
    if len(line) <= width:
        return line
    center = min(max(start - line_start, 0), len(line))
    half = width // 2
    low = min(max(center - half, 0), len(line) - width)
    window = line[low : low + width]
    prefix = "..." if low > 0 else ""
    suffix = "..." if low + width < len(line) else ""
    return f"{prefix}{window}{suffix}"


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically replace *path* with *text*, preserving file permissions."""
    try:
        mode = os.stat(path).st_mode
    except OSError as exc:
        raise NotesError(f"could not stat {path}: {exc}") from exc
    data = text.encode("utf-8")
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    except OSError as exc:
        raise NotesError(f"could not write {path}: {exc}") from exc
    try:
        with os.fdopen(fd, "wb") as tmp_file:
            tmp_file.write(data)
        os.chmod(tmp_name, mode & 0o7777)
        os.replace(tmp_name, path)
    except BaseException as exc:
        Path(tmp_name).unlink(missing_ok=True)
        if isinstance(exc, NotesError):
            raise
        raise NotesError(f"could not write {path}: {exc}") from exc


def _note_blocks(chunk: Sequence[Unit], corpus: dict[Path, str]) -> dict[Path, list[Block]]:
    """Group a chunk's model-visible blocks by the note they came from."""
    grouped: dict[Path, list[Block]] = {}
    for block, unit in zip(_blocks_of(chunk), chunk, strict=True):
        grouped.setdefault(unit.path, []).append(block)
    return grouped


def validate_chunk(
    chunk: Sequence[Unit],
    proposals: Sequence[ProposedCorrection],
    corpus: dict[Path, str],
    cache: dict[Path, tuple[str, list[tuple[int, int]]]],
) -> list[ModelEvidence]:
    """Map one chunk's proposals back to exact source ranges, note by note.

    Validation runs per source note with only that note's blocks in scope, so a
    proposal naming a block from a neighbouring note in the same request cannot
    be applied to the wrong file.
    """
    evidence: list[ModelEvidence] = []
    for path, blocks in _note_blocks(chunk, corpus).items():
        text = corpus[path]
        for edit in validate_corrections(text, blocks, _protected_ranges(cache, path, text), proposals):
            evidence.append(
                ModelEvidence(
                    path=path, start=edit.start, end=edit.end, original=edit.original, replacement=edit.replacement
                )
            )
    return evidence


async def _discover_chunk(
    client: ModelClient,
    chunk: list[Unit],
    corpus: dict[Path, str],
    cache: dict[Path, tuple[str, list[tuple[int, int]]]],
    depth: int = 0,
) -> list[ModelEvidence]:
    """One model request over a chunk, halving and retrying on context overflow.

    Overflow usually means the response blew up rather than the prompt, so
    halving the chunk is the fix. A chunk that still overflows after a few
    splits is skipped with a loud warning: one pathological note must not abort
    a whole-vault run.
    """
    try:
        proposals = await client.correct(_blocks_of(chunk))
    except ContextTooLargeError:
        halves = _halve(chunk)
        if depth >= _MAX_SECTION_SPLITS or len(halves) <= 1:
            label = chunk[0].path.name if chunk else "note"
            print(
                f"warning: skipping a prose section in {label} that exceeds the "
                "Foundation Models context window; review it manually",
                file=sys.stderr,
            )
            return []
        rescued: list[ModelEvidence] = []
        for half in halves:
            rescued.extend(await _discover_chunk(client, half, corpus, cache, depth + 1))
        return rescued
    return validate_chunk(chunk, proposals, corpus, cache)


async def discover_corrections(text: str, client: ModelClient, path: Path | None = None) -> list[AcceptedCorrection]:
    """Document-level discovery primitive: validated model evidence for one note.

    Retained as the single-document seam (and the shape most tests want). The
    vault path streams file-groups through :func:`_review_pass`, which packs
    many notes per request the same way.
    """
    units = _units_for(path if path is not None else Path("<note>"), text, markdown.find_protected_ranges(text))
    if not units:
        return []
    corpus = {units[0].path: text}
    cache: dict[Path, tuple[str, list[tuple[int, int]]]] = {}
    accepted: list[AcceptedCorrection] = []
    for chunk in await plan_chunks(units, client):
        for item in await _discover_chunk(client, chunk, corpus, cache):
            accepted.append(
                AcceptedCorrection(
                    block_id=0, original=item.original, replacement=item.replacement, start=item.start, end=item.end
                )
            )
    return sorted(accepted, key=lambda edit: edit.start)


def _units_for(path: Path, text: str, protected: Sequence[tuple[int, int]]) -> list[Unit]:
    """Eligible prose of one note as model-visible units, in document order.

    Blocks are located in the source (so fenced code and front matter stay out),
    then sliced from the masked copy, so the model reads only prose it may edit
    while every offset still refers to the source.
    """
    masked = mask_protected(text, protected)
    return [
        Unit(path=path, start=block.start, end=block.end, text=masked[block.start : block.end])
        for block in markdown.find_blocks(text)
    ]


async def _discover_group(
    group: list[Unit],
    budget: int,
    client: ModelClient,
    corpus: dict[Path, str],
    cache: dict[Path, tuple[str, list[tuple[int, int]]]],
) -> list[ModelEvidence]:
    """Discover one file-group, splitting to fit; every piece costs one request.

    All of the group's requests run before the caller reviews anything, so the
    snapshots the model saw can never go stale under review edits. A model
    failure still names the file that caused it.
    """
    evidence: list[ModelEvidence] = []
    for chunk in await _fit_chunk(group, budget, client):
        try:
            evidence.extend(await _discover_chunk(client, chunk, corpus, cache))
        except NotesError as exc:
            raise NotesError(f"{chunk[0].path}: {exc}") from exc
    return evidence


def _review_pass(
    vault: Path,
    corpus: dict[Path, str],
    cache: dict[Path, tuple[str, list[tuple[int, int]]]],
    client: ModelClient,
    reviewer: Reviewer,
    approved: dict[str, str],
    skipped_sources: set[str],
    skipped_pairs: set[tuple[str, str]],
    stats: ReviewStats,
) -> tuple[bool, bool]:
    """Stream one discovery pass: per file-group, discover then review immediately.

    Groups pack whole files up to the context budget in deterministic path
    order — slightly looser than unit-level packing at file boundaries, but
    whole files keep every snapshot inside the group fresh by construction.
    Each file's units are (re)derived from the current corpus text, so mappings
    approved in earlier groups are already applied to what the model sees next.
    Returns ``(quit_requested, found_any)``.
    """
    sample: list[Unit] = []
    for path in sorted(corpus):
        sample.extend(_units_for(path, corpus[path], _protected_ranges(cache, path, corpus[path])))
        if len(sample) >= _RATIO_SAMPLE_UNITS:
            break
    if not sample:
        return False, False
    budget = asyncio.run(_prompt_budget(client))
    ratio = asyncio.run(_chars_per_token(sample, client))

    cached: dict[Path, tuple[str, list[Unit]]] = {}

    def fresh_units(path: Path) -> list[Unit]:
        """Units for *path*, re-derived whenever its text changed underneath."""
        text = corpus[path]
        entry = cached.get(path)
        if entry is not None and entry[0] == text:
            return entry[1]
        units = _units_for(path, text, _protected_ranges(cache, path, text))
        cached[path] = (text, units)
        return units

    def estimate_groups() -> int:
        """Greedy group count over whole files, the rule the loop below applies.

        Fixes shrink later groups, so a pass can finish just under this; it is
        progress information only, never a correctness input.
        """
        total = 0
        used = 0
        for path in sorted(corpus):
            size = sum(int(len(unit.text) / ratio) + 1 for unit in fresh_units(path))
            if size and used and used + size > budget:
                total += 1
                used = 0
            used += size
        return total + (1 if used else 0)

    total = estimate_groups()
    started = time.monotonic()
    done = 0
    found_any = False

    def process(group: list[Unit]) -> bool:
        """Discover one packed group and review its new candidates; True on quit."""
        nonlocal done, found_any
        evidence = asyncio.run(_discover_group(group, budget, client, corpus, cache))
        done += 1
        _report_progress(done, max(total, done), started)
        candidates = build_candidates(
            group_candidates(evidence), corpus, cache, approved, skipped_sources, skipped_pairs
        )
        prompted: list[Candidate] = []
        for candidate in candidates:
            reason = _auto_skip_reason(candidate)
            if reason is None:
                prompted.append(candidate)
                continue
            stats.candidates_skipped += 1
            skipped_pairs.add((candidate.original, candidate.replacement))
            print(
                f"Skipped {candidate.original} → {candidate.replacement} "
                f"({reason} — looks context-dependent, not a vault-wide typo)."
            )
        if not prompted:
            return False
        found_any = True
        return review_candidates(
            vault, prompted, corpus, cache, approved, skipped_sources, skipped_pairs, reviewer, stats
        )

    group: list[Unit] = []
    used = 0
    for path in sorted(corpus):
        units = fresh_units(path)
        if not units:
            continue
        size = sum(int(len(unit.text) / ratio) + 1 for unit in units)
        if group and used + size > budget:
            if process(group):
                _finish_progress()
                return True, found_any
            group, used = [], 0
            # The review just ran global replacements: re-derive, since this
            # file's snapshot may predate them. Every other future file is
            # re-derived lazily by fresh_units on encounter.
            units = fresh_units(path)
            if not units:
                continue
            size = sum(int(len(unit.text) / ratio) + 1 for unit in units)
        group.extend(units)
        used += size
    if group and process(group):
        _finish_progress()
        return True, found_any
    if done:
        _finish_progress()
    return False, found_any


def _format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _report_progress(done: int, total: int, started: float) -> None:
    """Progress feedback, so a vault run measured in hours never looks hung.

    On a terminal this rewrites one line in place. When stderr is a pipe or a
    log there is nothing to rewrite, so it prints an occasional full line
    instead — silence for hours is worse than a little noise.
    """
    elapsed = _format_elapsed(time.monotonic() - started)
    if sys.stderr.isatty():
        if done < total:
            print(f"\r  {done}/{total} groups checked ({elapsed} elapsed)", end="", file=sys.stderr, flush=True)
        return
    if done % _PROGRESS_INTERVAL == 0:
        print(f"  {done}/{total} groups checked ({elapsed} elapsed)", file=sys.stderr, flush=True)


def _finish_progress() -> None:
    """Clear the progress line so later output starts on a clean row."""
    if sys.stderr.isatty():
        print("\r\033[K", end="", file=sys.stderr, flush=True)


def load_corpus(vault: Path) -> dict[Path, str]:
    """Read every vault note as UTF-8, skipping unreadable files with a warning."""
    corpus: dict[Path, str] = {}
    for path in collect_markdown_files(vault, None):
        try:
            data = path.read_bytes()
        except OSError as exc:
            print(f"warning: skipping {path.name} ({exc})", file=sys.stderr)
            continue
        try:
            corpus[path] = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            print(f"warning: skipping {path.name} (not valid UTF-8: {exc})", file=sys.stderr)
    return corpus


def group_candidates(evidence: list[ModelEvidence]) -> dict[tuple[str, str], list[ModelEvidence]]:
    """Group validated evidence by ``(original, replacement)`` mapping.

    Competing replacements for the same source spelling stay distinct;
    they are surfaced conservatively, never merged or guessed between.
    """
    grouped: dict[tuple[str, str], list[ModelEvidence]] = {}
    for item in evidence:
        grouped.setdefault((item.original, item.replacement), []).append(item)
    return grouped


def _protected_ranges(
    cache: dict[Path, tuple[str, list[tuple[int, int]]]], path: Path, text: str
) -> list[tuple[int, int]]:
    """Protected ranges for *path*, recomputed only when its text changed."""
    cached = cache.get(path)
    if cached is not None and cached[0] == text:
        return cached[1]
    ranges = markdown.find_protected_ranges(text)
    cache[path] = (text, ranges)
    return ranges


def refresh_occurrences(
    candidate: Candidate, corpus: dict[Path, str], cache: dict[Path, tuple[str, list[tuple[int, int]]]]
) -> None:
    """Recompute a candidate's safe vault occurrences from current file text."""
    occurrences: list[VaultOccurrence] = []
    for path in sorted(corpus):
        for start, end in find_eligible_occurrences(
            corpus[path], candidate.original, _protected_ranges(cache, path, corpus[path])
        ):
            occurrences.append(VaultOccurrence(path=path, start=start, end=end))
    candidate.occurrences = occurrences


def build_candidates(
    grouped: dict[tuple[str, str], list[ModelEvidence]],
    corpus: dict[Path, str],
    cache: dict[Path, tuple[str, list[tuple[int, int]]]],
    approved: dict[str, str],
    skipped_sources: set[str],
    skipped_pairs: set[tuple[str, str]],
) -> list[Candidate]:
    """Assemble review candidates from new model evidence.

    Already-approved mappings, skipped source spellings, and skipped pairs
    are never re-prompted. Candidates with no remaining safe occurrences
    (e.g. consumed by an earlier global approval) are dropped silently.
    Ordering favors the highest-leverage mappings first: most safe vault
    occurrences, then most independent model evidence. No model confidence
    scores are invented.
    """
    candidates: list[Candidate] = []
    for (original, replacement), items in grouped.items():
        if original in skipped_sources or (original, replacement) in skipped_pairs:
            continue
        if approved.get(original) == replacement:
            continue
        candidate = Candidate(original=original, replacement=replacement, evidence=list(items))
        refresh_occurrences(candidate, corpus, cache)
        if candidate.occurrences:
            candidates.append(candidate)
    candidates.sort(key=lambda cand: (-len(cand.occurrences), -len(cand.evidence), cand.original, cand.replacement))
    return candidates


def _auto_skip_reason(candidate: Candidate) -> str | None:
    """Reason to drop *candidate* without prompting, or None to surface it.

    One flagged context for established vocabulary is context-dependent by
    the model's own behavior: it saw the token hundreds of times and flagged
    exactly one. Surfacing that as a vault-wide replace turns one judgment
    call into hundreds of edits. True typos are either rare (few safe
    occurrences) or flagged everywhere the model looks.
    """
    if len(candidate.evidence) >= _MIN_EVIDENCE_FOR_ESTABLISHED:
        return None
    if len(candidate.occurrences) > _ESTABLISHED_OCCURRENCES:
        return (
            f"{_plural(len(candidate.evidence), 'flagged context', 'flagged contexts')} for "
            f"{_plural(len(candidate.occurrences), 'occurrence', 'occurrences')}"
        )
    return None


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def render_candidate(vault: Path, candidate: Candidate, index: int, total: int, corpus: dict[Path, str]) -> str:
    """Render one candidate with a small sample of distinct-file occurrences.

    Each sampled occurrence shows a diff-style before/after pair with 100
    characters of context on each side: the original highlighted red on the
    ``-`` line, the replacement green on the ``+`` line.
    """
    occurrences = candidate.occurrences
    files = {occurrence.path for occurrence in occurrences}
    sample: list[VaultOccurrence] = []
    for occurrence in occurrences:
        if all(occurrence.path != shown.path for shown in sample):
            sample.append(occurrence)
        if len(sample) >= _SAMPLE_SIZE:
            break
    if len(sample) < _SAMPLE_SIZE:
        for occurrence in occurrences:
            if occurrence not in sample:
                sample.append(occurrence)
            if len(sample) >= _SAMPLE_SIZE:
                break
    lines = [
        f"[{index}/{total}] {candidate.original} → {candidate.replacement}",
        _plural(len(occurrences), "eligible occurrence", "eligible occurrences")
        + f" in {_plural(len(files), 'note', 'notes')}",
        f"AI flagged this independently in {_plural(len(candidate.evidence), 'context', 'contexts')}",
        "",
    ]
    for occurrence in sample:
        try:
            rel = occurrence.path.relative_to(vault).as_posix()
        except ValueError:
            rel = str(occurrence.path)
        lines.append(f"  {rel}")
        old, new = snippet_diff_lines(
            corpus[occurrence.path], occurrence.start, occurrence.end, candidate.replacement
        )
        lines.append(f'    - "{old}"')
        lines.append(f'    + "{new}"')
        lines.append("")
    lines.append("[a] all  [y] one  [n] next  [s] skip spelling  [q] quit")
    return "\n".join(lines)


def apply_mapping(
    corpus: dict[Path, str],
    cache: dict[Path, tuple[str, list[tuple[int, int]]]],
    original: str,
    replacement: str,
    stats: ReviewStats,
) -> tuple[int, int]:
    """Apply an approved mapping to every safe vault occurrence; return ``(occurrences, files)``."""
    total = 0
    files = 0
    for path in sorted(corpus):
        text = corpus[path]
        ranges = find_eligible_occurrences(text, original, _protected_ranges(cache, path, text))
        if not ranges:
            continue
        updated = _splice_ranges(text, ranges, replacement)
        if updated == text:
            continue
        atomic_write_text(path, updated)
        corpus[path] = updated
        total += len(ranges)
        files += 1
        stats.notes_modified.add(path)
    stats.occurrences_corrected += total
    return total, files


def apply_single_occurrence(
    corpus: dict[Path, str],
    occurrence: VaultOccurrence,
    original: str,
    replacement: str,
    stats: ReviewStats,
) -> None:
    """Apply exactly one reviewed occurrence via its exact source range."""
    text = corpus[occurrence.path]
    if text[occurrence.start : occurrence.end] != original:
        raise NotesError(f"occurrence changed under review, leaving it untouched: {occurrence.path}")
    updated = text[: occurrence.start] + replacement + text[occurrence.end :]
    atomic_write_text(occurrence.path, updated)
    corpus[occurrence.path] = updated
    stats.occurrences_corrected += 1
    stats.notes_modified.add(occurrence.path)


def review_candidates(
    vault: Path,
    candidates: list[Candidate],
    corpus: dict[Path, str],
    cache: dict[Path, tuple[str, list[tuple[int, int]]]],
    approved: dict[str, str],
    skipped_sources: set[str],
    skipped_pairs: set[tuple[str, str]],
    reviewer: Reviewer,
    stats: ReviewStats,
) -> bool:
    """Interactively review one pass's queue; return True when the user quits."""
    total = len(candidates)
    for position, candidate in enumerate(candidates, 1):
        while True:
            refresh_occurrences(candidate, corpus, cache)
            if not candidate.occurrences:
                break  # consumed by an earlier approval; not counted either way
            print(render_candidate(vault, candidate, position, total, corpus))
            action = (reviewer.decide(candidate) or "").strip().lower()[:1]
            if action == "a":
                count, files = apply_mapping(corpus, cache, candidate.original, candidate.replacement, stats)
                approved[candidate.original] = candidate.replacement
                print(f"Replaced {_plural(count, 'occurrence', 'occurrences')} in {_plural(files, 'note', 'notes')}.")
                break
            if action == "y":
                first = candidate.occurrences[0]
                apply_single_occurrence(corpus, first, candidate.original, candidate.replacement, stats)
                print(f"Replaced 1 occurrence in {first.path.relative_to(vault).as_posix()}.")
                continue  # re-present remaining occurrences normally
            if action == "s":
                skipped_sources.add(candidate.original)
                stats.spellings_ignored += 1
                break
            if action == "q":
                return True
            # `n` and anything unrecognized skip this candidate only.
            if action != "n":
                print(f"(unknown action {action!r}; skipping candidate)")
            stats.candidates_skipped += 1
            skipped_pairs.add((candidate.original, candidate.replacement))
            break
    return False


class InteractiveReviewer:
    """Single-keystroke decisions when stdin is a TTY, ``input()`` otherwise."""

    def decide(self, candidate: Candidate) -> str:
        _ = candidate
        while True:
            key = _read_key()
            if key in ("a", "y", "s", "n", "q"):
                return key
            print(f"(press one of a/y/s/n/q, got {key!r})")


def _read_key() -> str:
    """One keystroke on a macOS terminal; line input when not a TTY."""
    if sys.stdin.isatty():
        try:
            return _read_single_key()
        except Exception:
            pass
    try:
        line = input("> ")
    except EOFError:
        return "q"
    return line.strip().lower()[:1]


def _read_single_key() -> str:
    """Read one keystroke in raw mode (caller falls back to ``input()`` on failure)."""
    import termios
    import tty

    print("> ", end="", flush=True)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        char = cast(str, sys.stdin.read(1))
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    print(char if char.isprintable() else "")
    if char in ("\x03", "\x04"):
        raise KeyboardInterrupt
    return (char or "").strip().lower()[:1]


def sweep_approved(
    corpus: dict[Path, str],
    cache: dict[Path, tuple[str, list[tuple[int, int]]]],
    approved: dict[str, str],
    stats: ReviewStats,
) -> None:
    """Auto-apply already-approved mappings to any current safe occurrences."""
    for original, replacement in sorted(approved.items()):
        count, files = apply_mapping(corpus, cache, original, replacement, stats)
        if count:
            print(f"Auto-applied approved mapping {original} → {replacement}: {count} occurrences in {files} notes.")


def configure_parser(p: argparse.ArgumentParser) -> None:
    p.description = (
        "Interactively spellcheck the whole Obsidian vault with Apple's on-device "
        "Foundation Models model: AI discovers typo mappings, you approve each one, "
        "and approved mappings apply to all safe occurrences."
    )
    p.add_argument(
        "--vault",
        type=Path,
        default=VAULT,
        help="notes vault (default: %(default)s)",
    )


def run(
    args: argparse.Namespace,
    client: ModelClient | None = None,
    reviewer: Reviewer | None = None,
) -> int:
    vault = resolve_vault(args.vault).resolve()
    active_client = client if client is not None else create_live_client()
    active_reviewer = reviewer if reviewer is not None else InteractiveReviewer()
    approved: dict[str, str] = {}
    skipped_sources: set[str] = set()
    skipped_pairs: set[tuple[str, str]] = set()
    stats = ReviewStats()

    corpus = load_corpus(vault)
    cache: dict[Path, tuple[str, list[tuple[int, int]]]] = {}
    print(f"Scanning {len(corpus)} notes...")
    quit_requested = False
    for pass_no in range(1, _MAX_PASSES + 1):
        stats.passes += 1
        if pass_no > 1:
            print(f"Pass {pass_no}: checking for remaining spelling errors...")
            corpus = load_corpus(vault)
        sweep_approved(corpus, cache, approved, stats)
        print("Checking prose with Apple Foundation Models...")
        quit_requested, found = _review_pass(
            vault, corpus, cache, active_client, active_reviewer, approved, skipped_sources, skipped_pairs, stats
        )
        if quit_requested:
            break
        if not found:
            print("No new spelling candidates found." if pass_no > 1 else "No spelling corrections found.")
            break

    print()
    print("Spellcheck complete.")
    print()
    print(f"{_plural(len(approved), 'mapping', 'mappings')} approved")
    print(f"{_plural(stats.occurrences_corrected, 'occurrence', 'occurrences')} corrected")
    print(f"{_plural(len(stats.notes_modified), 'note', 'notes')} modified")
    print(f"{_plural(stats.candidates_skipped, 'candidate', 'candidates')} skipped")
    print(f"{_plural(len(skipped_sources), 'spelling', 'spellings')} ignored")
    print(f"{_plural(stats.passes, 'discovery pass', 'discovery passes')}")
    return 0
