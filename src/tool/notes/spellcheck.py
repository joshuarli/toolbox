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

Usage::

    tool notes spellcheck
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import os
import sys
import tempfile
import unicodedata
from collections.abc import Iterator, Sequence
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
# A replacement introducing one of these can corrupt Markdown/HTML/math
# syntax (code spans, links, tags, math, tables, escapes), so it is rejected.
_REPLACEMENT_FORBIDDEN = set("`[]<>\\|$")
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
_SNIPPET_WIDTH = 100
# Upper bound on discovery passes; generative inference is nondeterministic,
# so a fixed cap guarantees convergence.
_MAX_PASSES = 3
# Occurrences sampled per candidate display, preferably from distinct files.
_SAMPLE_SIZE = 3


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


async def _fit_unit(block: Block, budget: int, client: ModelClient) -> list[Block]:
    """Split one paragraph until each piece fits *budget* alone; never skip text."""
    if await _measured_tokens(client, render_prompt(block)) <= budget:
        return [block]
    if len(block.text) <= 1:
        # Unsplittable unit (practically impossible at these budgets):
        # leave it; generation will surface a clear context error.
        return [block]
    pieces = markdown.split_range(block.text, 0, len(block.text), max(1, len(block.text) // 2))
    if len(pieces) <= 1:
        return [block]
    fitted: list[Block] = []
    for start, end in pieces:
        sub = Block(block_id=0, start=block.start + start, end=block.start + end, text=block.text[start:end])
        fitted.extend(await _fit_unit(sub, budget, client))
    return fitted


def _section(text: str, units: list[Block]) -> Block:
    """One exact source slice spanning *units* (paragraphs stay in order)."""
    start, end = units[0].start, units[-1].end
    return Block(block_id=0, start=start, end=end, text=text[start:end])


async def plan_chunks(blocks: Sequence[Block], text: str, client: ModelClient) -> list[Block]:
    """Group paragraph blocks into section-scale chunks fitting the context window.

    Chunk prompts are measured with the real ``token_count`` API against the
    real ``context_size`` — no hard-coded model sizes. Each chunk is a single
    exact source slice with a fresh stable id, so the model echoes one id per
    request and occurrence counting stays unambiguous. Every chunk gets a
    fresh session, so later requests never inherit earlier document text.
    """
    budget = await _prompt_budget(client)
    units: list[Block] = []
    for block in blocks:
        units.extend(await _fit_unit(block, budget, client))
    sections: list[Block] = []
    current: list[Block] = []
    for unit in units:
        candidate: list[Block] = [*current, unit]
        probe = _section(text, candidate)
        if await _measured_tokens(client, render_prompt(probe)) <= budget:
            current = candidate
            continue
        if current:
            sections.append(_section(text, current))
        current = [unit]
    if current:
        sections.append(_section(text, current))
    return [Block(block_id=i + 1, start=s.start, end=s.end, text=s.text) for i, s in enumerate(sections)]


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
) -> tuple[int, int] | None:
    """Map one proposal to an exact document range, or reject it (None)."""
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
    if any(char in _REPLACEMENT_FORBIDDEN for char in replacement):
        return None
    if not _minimal_substitution(original, replacement):
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
    return (start, end)


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
        start, end = mapped
        key = (start, end, proposal.original, proposal.replacement)
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
                original=proposal.original,
                replacement=proposal.replacement,
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


def snippet_for(text: str, start: int, end: int, width: int = _SNIPPET_WIDTH) -> str:
    """One trimmed source line around ``[start:end)`` for human context."""
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


async def discover_corrections(text: str, client: ModelClient, path: Path | None = None) -> list[AcceptedCorrection]:
    """Document-level discovery primitive: validated model evidence for one note.

    Runs token-aware chunking, one fresh model session per chunk, and strict
    validation. A chunk that still overflows the context window (usually a
    response-side blowup from many corrections) is halved and retried a few
    times, then skipped with a loud warning — one pathological note must not
    abort a whole-vault run. Retained as the test seam for vault passes.
    """
    protected = markdown.find_protected_ranges(text)
    blocks = markdown.find_blocks(text)
    if not blocks:
        return []
    sections = await plan_chunks(blocks, text, client)
    fresh_ids = itertools.count(len(sections) + 1)
    pairs: list[tuple[Block, list[ProposedCorrection]]] = []
    for section in sections:
        pairs.extend(await _correct_with_rescue(client, section, path, fresh_ids))
    # Validate per block so occurrence mapping stays exact after rescue splits.
    accepted: list[AcceptedCorrection] = []
    for block, proposals in pairs:
        accepted.extend(validate_corrections(text, [block], protected, proposals))
    return sorted(accepted, key=lambda edit: edit.start)


async def _correct_with_rescue(
    client: ModelClient, section: Block, path: Path | None, fresh_ids: Iterator[int], depth: int = 0
) -> list[tuple[Block, list[ProposedCorrection]]]:
    """Correct one section, halving and retrying on context-window overflow."""
    try:
        return [(section, await client.correct_block(section))]
    except ContextTooLargeError:
        pieces = markdown.split_range(section.text, 0, len(section.text), max(1, len(section.text) // 2))
        if depth >= _MAX_SECTION_SPLITS or len(pieces) <= 1:
            label = path.name if path is not None else "note"
            print(
                f"warning: skipping a prose section in {label} that exceeds the "
                "Foundation Models context window; review it manually",
                file=sys.stderr,
            )
            return []
        rescued: list[tuple[Block, list[ProposedCorrection]]] = []
        for start, end in pieces:
            sub = Block(
                block_id=next(fresh_ids),
                start=section.start + start,
                end=section.start + end,
                text=section.text[start:end],
            )
            rescued.extend(await _correct_with_rescue(client, sub, path, fresh_ids, depth + 1))
        return rescued


async def discover_file(path: Path, text: str, client: ModelClient) -> list[ModelEvidence]:
    """Validated model evidence for one note, with exact source ranges."""
    seen: set[tuple[int, int, str, str]] = set()
    evidence: list[ModelEvidence] = []
    for edit in await discover_corrections(text, client, path):
        key = (edit.start, edit.end, edit.original, edit.replacement)
        if key in seen:
            continue
        seen.add(key)
        evidence.append(
            ModelEvidence(
                path=path, start=edit.start, end=edit.end, original=edit.original, replacement=edit.replacement
            )
        )
    return evidence


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


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def render_candidate(vault: Path, candidate: Candidate, index: int, total: int, corpus: dict[Path, str]) -> str:
    """Render one candidate with a small sample of distinct-file occurrences."""
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
        lines.append(f'    "{snippet_for(corpus[occurrence.path], occurrence.start, occurrence.end)}"')
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


async def _discover_all(corpus: dict[Path, str], client: ModelClient) -> list[ModelEvidence]:
    """Run model discovery over every corpus note, in deterministic path order."""
    evidence: list[ModelEvidence] = []
    for path in sorted(corpus):
        started = asyncio.get_running_loop().time()
        try:
            evidence.extend(await discover_file(path, corpus[path], client))
        except NotesError as exc:
            raise NotesError(f"{path}: {exc}") from exc
        elapsed = asyncio.get_running_loop().time() - started
        if elapsed > 10:
            # Multi-hour vault runs otherwise look hung on monster clippings.
            print(f"  checked {path.name} in {elapsed:.0f}s", file=sys.stderr)
    return evidence


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
        evidence = asyncio.run(_discover_all(corpus, active_client))
        candidates = build_candidates(
            group_candidates(evidence), corpus, cache, approved, skipped_sources, skipped_pairs
        )
        if not candidates:
            print("No new spelling candidates found." if pass_no > 1 else "No spelling corrections found.")
            break
        if pass_no > 1:
            print(f"Found {_plural(len(candidates), 'new candidate', 'new candidates')}.")
        quit_requested = review_candidates(
            vault, candidates, corpus, cache, approved, skipped_sources, skipped_pairs, active_reviewer, stats
        )
        if quit_requested:
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
