"""Note-oriented spelling review and structured output for the notes vault.

``checker.SpellChecker`` returns structured ``Finding`` records; this module
turns them into either an interactive review (one note at a time, repeated
instances of one ``original -> replacement`` pair grouped per note) or
machine-readable output (``--format json|jsonl|diff``) that never prompts or
writes notes.

User decisions override the checker: a rejection suppresses one exact pair in
one note, and a protection suppresses every proposal inside a remembered word
or phrase in its scope.

Offsets are Python Unicode code-point offsets into the exact decoded UTF-8
source. File snapshot hashes are checked again before every write, so an
external edit cannot be silently overwritten.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import select
import sys
import tempfile
import termios
import tty
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

from tool.notes import markdown
from tool.notes.checker import DEFAULT_LANGUAGES, Finding, SpellChecker
from tool.notes.common import VAULT, NotesError, resolve_vault
from tool.notes.localize import collect_markdown_files, resolve_roots
from tool.notes.state import Decision, Operation, StateStore, default_state_path, sha256_bytes

_CONTEXT_RADIUS = 80
_RED = "\033[31m"
_GREEN = "\033[32m"
_RESET = "\033[0m"
FORMATS = ("review", "json", "jsonl", "diff")


class Checker(Protocol):
    """What the review needs from ``checker.SpellChecker`` (fakes implement this in tests)."""

    def prepare(self, notes: Mapping[str, str], log: Callable[[str], None] | None = None) -> None:
        """Index the whole vault before any note is analyzed."""
        ...

    def analyze(self, note_id: str, text: str, only: frozenset[str] | None = None) -> list[Finding]:
        """Return source-ordered findings for one note."""
        ...


@dataclass(frozen=True, slots=True)
class Occurrence:
    """One exact proposed edit in one reviewed file snapshot."""

    path: Path
    note_id: str
    start: int
    end: int
    original: str
    replacement: str
    snapshot: str


@dataclass(slots=True)
class Candidate:
    """One exact mapping grouped only within the current note.

    ``bulk_safe`` is true only when every occurrence is a context-free known
    misspelling; only those may be applied across the vault with ``A``.
    """

    path: Path
    note_id: str
    original: str
    replacement: str
    reason: str = ""
    bulk_safe: bool = False
    occurrences: list[Occurrence] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


@dataclass(slots=True)
class ReviewStats:
    notes_checked: int = 0
    notes_skipped: int = 0
    prompts: int = 0
    occurrences_corrected: int = 0
    notes_modified: set[Path] = field(default_factory=set)
    decisions_added: int = 0
    operations_undone: int = 0
    conflicts: int = 0


@dataclass(frozen=True, slots=True)
class RememberChoice:
    source: str
    scope_kind: str
    scope_value: str


@dataclass(frozen=True, slots=True)
class BatchPlan:
    original: str
    replacement: str
    occurrences: tuple[Occurrence, ...]
    excluded: int
    excluded_reasons: tuple[tuple[str, int], ...]

    @property
    def files(self) -> tuple[str, ...]:
        return tuple(sorted({occurrence.note_id for occurrence in self.occurrences}))


class Reviewer(Protocol):
    def decide(self, candidate: Candidate) -> str:
        """Return ``a``, ``A``, ``n``, ``d``, ``u``, or ``q``."""
        ...


@dataclass(slots=True)
class NoteAnalysis:
    path: Path
    note_id: str
    text: str
    snapshot: str
    groups: list[Candidate]
    excluded: dict[tuple[str, str], dict[str, int]]


class ExternalEditError(NotesError):
    """The reviewed snapshot no longer matches the file on disk."""


def _note_id(vault: Path, path: Path) -> str:
    try:
        return path.relative_to(vault).as_posix()
    except ValueError as exc:
        raise NotesError(f"note is outside the vault: {path}") from exc


def _read_snapshot(path: Path) -> tuple[bytes, str, str]:
    try:
        data = path.read_bytes()
        return data, data.decode("utf-8"), sha256_bytes(data)
    except UnicodeDecodeError as exc:
        raise NotesError(f"note is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise NotesError(f"could not read note {path}: {exc}") from exc


def load_corpus(vault: Path, paths: Iterable[Path]) -> dict[str, str]:
    """Read notes by vault-relative id without normalizing newlines; unreadable notes are reported and skipped."""
    corpus: dict[str, str] = {}
    for path in paths:
        try:
            _, text, _ = _read_snapshot(path)
        except NotesError as exc:
            print(f"warning: {exc}", file=sys.stderr)
            continue
        corpus[_note_id(vault, path)] = text
    return corpus


def _overlaps(start: int, end: int, ranges: Iterable[tuple[int, int]]) -> bool:
    return any(range_start < end and start < range_end for range_start, range_end in ranges)


def _whole_token(text: str, start: int, end: int) -> bool:
    for position in (start - 1, end):
        if position < 0 or position >= len(text):
            continue
        char = text[position]
        if char == "_" or char.isalnum() or unicodedata.category(char).startswith("M"):
            return False
    return True


def _scope_applies(decision: Decision, note_id: str) -> bool:
    if decision.scope_kind == "vault":
        return True
    if decision.scope_kind == "note":
        return decision.scope_value == note_id
    prefix = decision.scope_value.strip("/")
    return not prefix or note_id == prefix or note_id.startswith(prefix + "/")


def _phrase_ranges(text: str, phrase: str, protected: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    if not phrase:
        return []
    ranges: list[tuple[int, int]] = []
    cursor = 0
    while True:
        start = text.find(phrase, cursor)
        if start == -1:
            break
        end = start + len(phrase)
        if not _overlaps(start, end, protected):
            ranges.append((start, end))
        cursor = start + 1
    return ranges


def _protection_contains(
    text: str,
    occurrence: tuple[int, int],
    decisions: Sequence[Decision],
    note_id: str,
    protected: Sequence[tuple[int, int]],
) -> bool:
    start, end = occurrence
    for decision in decisions:
        if decision.kind != "protection" or not _scope_applies(decision, note_id):
            continue
        for phrase_start, phrase_end in _phrase_ranges(text, decision.source, protected):
            if phrase_start <= start and end <= phrase_end:
                return True
    return False


class NoteAnalyzer:
    """Per-note boundary between checker findings, saved decisions, and review groups."""

    def __init__(self, vault: Path, checker: Checker, state: StateStore) -> None:
        self.vault = vault
        self.checker = checker
        self.state = state

    def analyze(self, path: Path, only: frozenset[str] | None = None) -> NoteAnalysis | None:
        try:
            _, text, snapshot = _read_snapshot(path)
        except NotesError as exc:
            print(f"warning: {exc}", file=sys.stderr)
            return None
        note_id = _note_id(self.vault, path)
        findings = self.checker.analyze(note_id, text, only)
        decisions = self.state.active_decisions() if findings else []
        # Defense in depth: the real checker never proposes inside Markdown
        # structure, but every write path re-verifies it.
        protected = markdown.find_protected_ranges(text) if findings else []
        groups: dict[tuple[str, str], Candidate] = {}
        excluded: dict[tuple[str, str], dict[str, int]] = {}

        def exclude(key: tuple[str, str], reason: str) -> None:
            reasons = excluded.setdefault(key, {})
            reasons[reason] = reasons.get(reason, 0) + 1

        for finding in findings:
            if not (0 <= finding.start < finding.end <= len(text)):
                continue
            if text[finding.start : finding.end] != finding.original:
                continue
            key = (finding.original, finding.replacement)
            if _overlaps(finding.start, finding.end, protected):
                exclude(key, "protected Markdown region")
                continue
            if not _whole_token(text, finding.start, finding.end):
                exclude(key, "part of an identifier-like token")
                continue
            rejected = any(
                decision.kind == "rejection"
                and _scope_applies(decision, note_id)
                and decision.source == finding.original
                and decision.replacement == finding.replacement
                for decision in decisions
            )
            if rejected:
                exclude(key, "rejected by a saved note decision")
                continue
            if _protection_contains(text, (finding.start, finding.end), decisions, note_id, protected):
                exclude(key, "protected by a saved word or phrase decision")
                continue
            occurrence = Occurrence(
                path, note_id, finding.start, finding.end, finding.original, finding.replacement, snapshot
            )
            group = groups.get(key)
            if group is None:
                group = groups[key] = Candidate(
                    path, note_id, finding.original, finding.replacement, finding.reason, finding.bulk_safe
                )
            group.bulk_safe = group.bulk_safe and finding.bulk_safe
            group.occurrences.append(occurrence)
            group.findings.append(finding)
        return NoteAnalysis(path, note_id, text, snapshot, list(groups.values()), excluded)


def _display_chunk(text: str) -> str:
    return text.replace("\r", " ").replace("\n", " ").replace("\t", " ")


def snippet_diff_lines(
    text: str, start: int, end: int, replacement: str, radius: int = _CONTEXT_RADIUS
) -> tuple[str, str]:
    before_start = max(0, start - radius)
    after_end = min(len(text), end + radius)
    prefix = "..." if before_start else ""
    suffix = "..." if after_end < len(text) else ""
    before = _display_chunk(text[before_start:start])
    after = _display_chunk(text[end:after_end])
    original = _display_chunk(text[start:end])
    replacement = _display_chunk(replacement)
    return (
        f"{prefix}{before}{_RED}{original}{_RESET}{after}{suffix}",
        f"{prefix}{before}{_GREEN}{replacement}{_RESET}{after}{suffix}",
    )


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def render_candidate(analysis: NoteAnalysis, candidate: Candidate, index: int, total: int) -> str:
    lines = [
        "",
        analysis.note_id,
        f"{candidate.original} → {candidate.replacement}",
        f"{len(candidate.occurrences)} eligible occurrence(s) in this note",
        f"Reason: {candidate.reason}",
        "",
    ]
    for occurrence in candidate.occurrences:
        old, new = snippet_diff_lines(analysis.text, occurrence.start, occurrence.end, candidate.replacement)
        lines.extend(
            [
                f"{_line_number(analysis.text, occurrence.start):>4}  - {old}",
                f"      + {new}",
            ]
        )
    vault_action = "  [A] apply across vault" if candidate.bulk_safe else ""
    lines.extend(
        [
            "",
            f"Proposal {index}/{total}",
            f"[a] apply in note{vault_action}",
            "[n] reject here   [d] remember…",
            "[u] undo          [q] quit",
        ]
    )
    return "\n".join(lines)


def render_batch_preview(plan: BatchPlan, analyses: dict[str, NoteAnalysis]) -> str:
    lines = [
        "",
        "Vault-wide batch preview",
        f"{plan.original} → {plan.replacement}",
        f"{len(plan.occurrences)} eligible occurrence(s) in {len(plan.files)} note(s)",
    ]
    if plan.excluded:
        lines.append(f"{plan.excluded} matching occurrence(s) excluded by saved decisions")
        for reason, count in plan.excluded_reasons:
            lines.append(f"  {count}: {reason}")
    lines.append("")
    for occurrence in plan.occurrences:
        analysis = analyses[occurrence.note_id]
        old, new = snippet_diff_lines(analysis.text, occurrence.start, occurrence.end, plan.replacement)
        lines.extend(
            [
                f"{occurrence.note_id}:{_line_number(analysis.text, occurrence.start)}",
                f"  - {old}",
                f"  + {new}",
            ]
        )
    return "\n".join(lines)


def _apply_edits(text: str, edits: Sequence[Occurrence]) -> str:
    ordered = sorted(edits, key=lambda occurrence: occurrence.start)
    cursor = 0
    parts: list[str] = []
    for occurrence in ordered:
        if occurrence.start < cursor or text[occurrence.start : occurrence.end] != occurrence.original:
            raise ExternalEditError(f"reviewed occurrence is stale in {occurrence.note_id}")
        parts.extend((text[cursor : occurrence.start], occurrence.replacement))
        cursor = occurrence.end
    parts.append(text[cursor:])
    return "".join(parts)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Replace one note atomically while retaining its permissions."""
    if path.is_symlink():
        raise NotesError(f"refusing to replace symlinked note: {path}")
    temporary: str | None = None
    try:
        mode = os.stat(path).st_mode & 0o7777
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except OSError as exc:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
        raise NotesError(f"could not write note {path}: {exc}") from exc


def _preflight_plan(vault: Path, plan: BatchPlan) -> dict[str, tuple[Path, bytes, bytes]]:
    by_note: dict[str, list[Occurrence]] = {}
    for occurrence in plan.occurrences:
        by_note.setdefault(occurrence.note_id, []).append(occurrence)
    prepared: dict[str, tuple[Path, bytes, bytes]] = {}
    for note_id, occurrences in by_note.items():
        path = vault / Path(note_id)
        before, text, snapshot = _read_snapshot(path)
        if snapshot != occurrences[0].snapshot:
            raise ExternalEditError(f"{note_id} changed since it was reviewed")
        updated = _apply_edits(text, occurrences)
        prepared[note_id] = (path, before, updated.encode("utf-8"))
    return prepared


def apply_plan(vault: Path, plan: BatchPlan, state: StateStore, kind: str) -> tuple[int, int]:
    """Apply one note or vault batch as one journaled logical operation."""
    prepared = _preflight_plan(vault, plan)
    operation_id = state.begin_operation(
        kind,
        {"original": plan.original, "replacement": plan.replacement, "notes": list(plan.files)},
    )
    try:
        for note_id, (_path, before, after) in prepared.items():
            state.add_operation_file(operation_id, note_id, before, after)
        for note_id, (path, _before, after) in prepared.items():
            atomic_write_bytes(path, after)
            state.mark_operation_file_applied(operation_id, note_id)
        state.finish_operation(operation_id, "completed")
    except BaseException:
        state.finish_operation(operation_id, "partial")
        raise
    return len(plan.occurrences), len(prepared)


def undo_last(vault: Path, state: StateStore) -> tuple[int, int, Operation | None]:
    """Undo the latest operation only when every changed file is still ours."""
    operation = state.latest_undoable()
    if operation is None:
        return 0, 0, None
    restored = 0
    conflicts = 0
    for item in operation.files:
        path = vault / Path(item.note_id)
        try:
            current = path.read_bytes()
        except OSError:
            if item.applied:
                conflicts += 1
            continue
        if sha256_bytes(current) != item.after_hash:
            if not item.applied and sha256_bytes(current) == item.before_hash:
                continue
            conflicts += 1
            continue
        atomic_write_bytes(path, item.before)
        restored += 1
    if conflicts:
        state.mark_undo_conflict(operation.id)
        return restored, conflicts, operation
    state.remove_decisions(list(operation.decision_ids))
    state.mark_undone(operation.id)
    return restored, 0, operation


def _candidate_plan(candidate: Candidate) -> BatchPlan:
    return BatchPlan(candidate.original, candidate.replacement, tuple(candidate.occurrences), 0, ())


def _make_batch_plan(
    candidate: Candidate,
    paths: Sequence[Path],
    analyzer: NoteAnalyzer,
) -> tuple[BatchPlan, dict[str, NoteAnalysis]]:
    """Collect every currently eligible bulk-safe occurrence of one exact pair."""
    occurrences = list(candidate.occurrences)
    analyses: dict[str, NoteAnalysis] = {}
    excluded = 0
    reasons: dict[str, int] = {}
    key = (candidate.original, candidate.replacement)
    only = frozenset({candidate.original})
    for path in paths:
        analysis = analyzer.analyze(path, only)
        if analysis is None:
            continue
        analyses[analysis.note_id] = analysis
        for other in analysis.groups:
            if (other.original, other.replacement) == key and other.bulk_safe:
                occurrences.extend(other.occurrences)
        for reason, count in analysis.excluded.get(key, {}).items():
            excluded += count
            reasons[reason] = reasons.get(reason, 0) + count
    unique = {(occurrence.note_id, occurrence.start, occurrence.end): occurrence for occurrence in occurrences}
    frozen = tuple(sorted(unique.values(), key=lambda occurrence: (occurrence.note_id, occurrence.start)))
    return BatchPlan(
        candidate.original,
        candidate.replacement,
        frozen,
        excluded,
        tuple(sorted(reasons.items())),
    ), analyses


def _normalize_remember(value: object, candidate: Candidate) -> RememberChoice | None:
    if value is None:
        return None
    if isinstance(value, RememberChoice):
        return value
    if isinstance(value, str):
        return RememberChoice(value or candidate.original, "note", candidate.note_id)
    return None


def _add_decision(
    state: StateStore,
    *,
    kind: str,
    source: str,
    replacement: str | None,
    scope_kind: str,
    scope_value: str,
) -> int:
    operation_id = state.begin_operation(
        "decision",
        {"kind": kind, "source": source, "replacement": replacement, "scope": scope_kind},
    )
    decision_id = state.add_decision(
        kind=kind,
        scope_kind=scope_kind,
        scope_value=scope_value,
        source=source,
        replacement=replacement,
    )
    state.link_operation_decision(operation_id, decision_id)
    state.finish_operation(operation_id, "completed")
    return decision_id


def _read_key() -> str:
    """Read one key without lowercasing, preserving ``a`` versus ``A``."""
    if sys.stdin.isatty():
        try:
            return _read_single_key()
        except (OSError, termios.error):
            pass
    try:
        return input("> ").strip()[:1]
    except EOFError:
        return "q"


def _read_single_key() -> str:
    print("> ", end="", flush=True)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        # Read straight from the fd, not the buffered sys.stdin, so a trailing
        # Enter typed out of habit after the key is never pulled into Python's
        # stdin buffer. Then drain any bytes already queued (that Enter) so
        # they can't leak into a later cooked-mode input() call and silently
        # answer it.
        char = os.read(fd, 1).decode(errors="replace")
        while select.select([fd], [], [], 0)[0]:
            os.read(fd, 1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    print(char if char.isprintable() else "")
    if char in ("\x03", "\x04"):
        raise KeyboardInterrupt
    return cast(str, char)


def _flush_stale_enter(timeout: float = 0.3) -> None:
    """Discard a lone Enter the user may have habitually typed after the prior
    single-key answer. That keystroke can lag slightly behind the raw read that
    consumed the key itself, so this waits briefly for it right before a
    cooked-mode ``input()`` prompt starts, rather than letting it arrive late
    and silently answer that prompt with an empty line.
    """
    if not sys.stdin.isatty():
        return
    fd = sys.stdin.fileno()
    try:
        while select.select([fd], [], [], timeout)[0]:
            os.read(fd, 1)
            timeout = 0
    except (OSError, termios.error):
        pass


class InteractiveReviewer:
    """TTY/non-TTY reviewer; raw terminal state is restored in all exits."""

    def decide(self, candidate: Candidate) -> str:
        _ = candidate
        return _read_key()

    def remember(self, candidate: Candidate) -> RememberChoice | None:
        print("Remember the exact source word or a containing phrase.")
        _flush_stale_enter()
        try:
            source = input(f"Phrase (Enter for {candidate.original!r}): ").strip() or candidate.original
            scope = input("Scope [n] note [d] directory [v] vault (default n): ").strip()[:1] or "n"
        except EOFError:
            return None
        if scope == "n":
            scope_kind, scope_value = "note", candidate.note_id
        elif scope == "d":
            parent = Path(candidate.note_id).parent.as_posix()
            scope_kind, scope_value = "directory", "" if parent == "." else parent
        elif scope == "v":
            scope_kind, scope_value = "vault", ""
        else:
            print("Unknown scope; no decision recorded.")
            return None
        return RememberChoice(source, scope_kind, scope_value)


def _remember(reviewer: Reviewer, candidate: Candidate) -> RememberChoice | None:
    callback = getattr(reviewer, "remember", None)
    if callable(callback):
        return _normalize_remember(callback(candidate), candidate)
    return RememberChoice(candidate.original, "note", candidate.note_id)


def _review_note(
    paths: Sequence[Path],
    index: int,
    analyzer: NoteAnalyzer,
    state: StateStore,
    reviewer: Reviewer,
    stats: ReviewStats,
) -> bool:
    """Review one note and return whether the user requested quit."""
    path = paths[index]
    while True:
        analysis = analyzer.analyze(path)
        stats.notes_checked += 1
        if analysis is None:
            stats.notes_skipped += 1
            return False
        if not analysis.groups:
            return False
        position = 0
        restart = False
        while position < len(analysis.groups):
            candidate = analysis.groups[position]
            stats.prompts += 1
            print(render_candidate(analysis, candidate, position + 1, len(analysis.groups)))
            action = reviewer.decide(candidate)
            if action == "q":
                return True
            if action == "u":
                restored, conflicts, _operation = undo_last(analyzer.vault, state)
                if conflicts:
                    stats.conflicts += conflicts
                    print(f"Undo stopped: {conflicts} file(s) changed after the operation.")
                elif restored:
                    stats.operations_undone += 1
                    print(f"Undid the last operation and restored {restored} note(s).")
                else:
                    print("Nothing to undo.")
                restart = True
                break
            if action == "a":
                try:
                    count, files = apply_plan(analyzer.vault, _candidate_plan(candidate), state, "note-apply")
                except ExternalEditError as exc:
                    stats.conflicts += 1
                    print(f"Conflict: {exc}. The note will be re-read.", file=sys.stderr)
                else:
                    stats.occurrences_corrected += count
                    stats.notes_modified.add(candidate.path)
                    print(f"Applied {count} occurrence(s) in {files} note(s).")
                restart = True
                break
            if action == "A":
                if not candidate.bulk_safe:
                    print("A is only offered for known misspellings; this correction depends on context. Use a.")
                    continue
                plan, analyses = _make_batch_plan(candidate, paths, analyzer)
                print(render_batch_preview(plan, analyses))
                if not plan.occurrences:
                    print("No currently eligible occurrences remain; nothing applied.")
                    restart = True
                    break
                try:
                    count, files = apply_plan(analyzer.vault, plan, state, "vault-apply")
                except ExternalEditError as exc:
                    stats.conflicts += 1
                    print(f"Conflict: {exc}. No batch changes were applied.", file=sys.stderr)
                else:
                    stats.occurrences_corrected += count
                    stats.notes_modified.update(analyzer.vault / Path(note_id) for note_id in plan.files)
                    print(f"Applied {count} occurrence(s) in {files} note(s).")
                restart = True
                break
            if action == "n":
                _add_decision(
                    state,
                    kind="rejection",
                    source=candidate.original,
                    replacement=candidate.replacement,
                    scope_kind="note",
                    scope_value=candidate.note_id,
                )
                stats.decisions_added += 1
                position += 1
                continue
            if action == "d":
                choice = _remember(reviewer, candidate)
                if choice is None:
                    print("No decision recorded.")
                    continue
                if choice.source not in analysis.text:
                    print("That exact word or phrase is not present in this note; no decision recorded.")
                    continue
                _add_decision(
                    state,
                    kind="protection",
                    source=choice.source,
                    replacement=None,
                    scope_kind=choice.scope_kind,
                    scope_value=choice.scope_value,
                )
                stats.decisions_added += 1
                position += 1
                continue
            print(f"Unknown action {action!r}; no decision recorded. Choose a, A, n, d, u, or q.")
        if not restart:
            return False


def _list_decisions(args: argparse.Namespace) -> int:
    path = Path(getattr(args, "state", None) or default_state_path()).expanduser()
    with StateStore(path) as state:
        decisions = state.active_decisions()
        print(f"State: {path}")
        if not decisions:
            print("No stored decisions.")
            return 0
        for decision in decisions:
            target = decision.scope_value or "(vault)"
            value = decision.source
            if decision.replacement is not None:
                value += f" → {decision.replacement}"
            print(f"{decision.id}: {decision.kind} [{decision.scope_kind}:{target}] {value}")
        for decision_id in getattr(args, "remove", []) or []:
            if state.deactivate_decision(decision_id):
                print(f"Removed decision {decision_id}.")
            else:
                print(f"Decision {decision_id} is not active.")
    return 0


def _journaled_completed_files(vault: Path, operation: Operation) -> list[str]:
    completed: list[str] = []
    for item in operation.files:
        if item.applied:
            completed.append(item.note_id)
            continue
        try:
            if sha256_bytes((vault / Path(item.note_id)).read_bytes()) == item.after_hash:
                completed.append(item.note_id)
        except OSError:
            continue
    return completed


def _languages(value: str) -> tuple[str, ...]:
    languages = tuple(part.strip() for part in value.split(",") if part.strip())
    if not languages:
        raise argparse.ArgumentTypeError("expected a comma-separated list such as en,en_GB,fr")
    return languages


def _explain_words(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault).resolve()
    with StateStore(args.state) as store:
        checker = SpellChecker(store, languages=args.languages)
        checker.prepare(load_corpus(vault, collect_markdown_files(vault)), log=_log)
        for word in args.words:
            print("\n".join(checker.explain_word(word)))
    return 0


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.description = (
        "Review spelling proposals note by note, or print them with --format. "
        "Local only: macOS system dictionaries plus a curated typo list."
    )
    parser.set_defaults(handler=run, spellcheck_action="review")
    parser.add_argument("--vault", type=Path, default=VAULT, help="notes vault (default: %(default)s)")
    parser.add_argument(
        "--state", type=Path, default=None, help=f"private SQLite state path (default: {default_state_path()})"
    )
    parser.add_argument(
        "--languages",
        type=_languages,
        default=DEFAULT_LANGUAGES,
        help="comma-separated macOS dictionaries whose union is accepted; the first is the default "
        "sentence language (default: %(default)s)",
    )
    parser.add_argument(
        "--format",
        choices=FORMATS,
        default="review",
        help="review interactively (default), or print findings as json, jsonl, or a unified diff; "
        "non-review formats never prompt or modify notes",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="print why every unknown word was or was not proposed (stderr)",
    )
    parser.add_argument(
        "--note",
        dest="notes",
        type=Path,
        action="append",
        default=[],
        metavar="PATH",
        help="limit review/output to this note or directory; repeatable (vault usage is still counted vault-wide)",
    )
    sub = parser.add_subparsers(dest="spellcheck_action")
    decisions = sub.add_parser("decisions", help="inspect or remove saved note/directory/vault decisions")
    decisions.add_argument("--state", type=Path, default=None, help="private SQLite state path")
    decisions.add_argument("--remove", type=int, action="append", default=[], metavar="ID")
    decisions.set_defaults(handler=_list_decisions)
    explain = sub.add_parser("explain", help="show how the checker judges specific words")
    explain.add_argument("words", nargs="+")
    explain.set_defaults(handler=_explain_words)


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _collect_findings(analyzer: NoteAnalyzer, paths: Sequence[Path]) -> list[tuple[NoteAnalysis, list[Finding]]]:
    results: list[tuple[NoteAnalysis, list[Finding]]] = []
    for path in paths:
        analysis = analyzer.analyze(path)
        if analysis is None or not analysis.groups:
            continue
        findings = sorted((f for group in analysis.groups for f in group.findings), key=lambda f: f.start)
        results.append((analysis, findings))
    return results


def _render_diff(analysis: NoteAnalysis, findings: Sequence[Finding]) -> str:
    edits = [
        Occurrence(analysis.path, analysis.note_id, f.start, f.end, f.original, f.replacement, analysis.snapshot)
        for f in findings
    ]
    updated = _apply_edits(analysis.text, edits)
    lines = difflib.unified_diff(
        analysis.text.splitlines(keepends=True),
        updated.splitlines(keepends=True),
        f"a/{analysis.note_id}",
        f"b/{analysis.note_id}",
    )
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)


def emit_structured(output_format: str, results: Sequence[tuple[NoteAnalysis, list[Finding]]]) -> None:
    """Write findings to stdout in note path then offset order; never prompts or writes notes."""
    if output_format == "json":
        payload = [finding.to_json() for _analysis, findings in results for finding in findings]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif output_format == "jsonl":
        for _analysis, findings in results:
            for finding in findings:
                print(json.dumps(finding.to_json(), ensure_ascii=False))
    elif output_format == "diff":
        for analysis, findings in results:
            sys.stdout.write(_render_diff(analysis, findings))
    else:
        raise ValueError(f"unknown structured format: {output_format}")


def _print_verbose(checker: SpellChecker, analyzer: NoteAnalyzer, paths: Sequence[Path]) -> None:
    for path in paths:
        try:
            _, text, _ = _read_snapshot(path)
        except NotesError:
            continue
        note_id = _note_id(analyzer.vault, path)
        for token, verdict in checker.explain_note(note_id, text):
            line = _line_number(text, token.start)
            outcome = f"→ {verdict.replacement}" if verdict.replacement else "skip"
            _log(f"{note_id}:{line}: {token.word} {outcome}: {verdict.reason}")


def run(
    args: argparse.Namespace,
    checker: Checker | None = None,
    reviewer: Reviewer | None = None,
    state: StateStore | None = None,
) -> int:
    """Index the vault, then review interactively or emit structured findings."""
    vault = resolve_vault(args.vault).resolve()
    output_format = getattr(args, "format", "review")
    owned_state = state is None
    store = state or StateStore(getattr(args, "state", None))
    try:
        all_paths = collect_markdown_files(vault)
        roots = resolve_roots(vault, list(getattr(args, "notes", None) or []))
        paths = collect_markdown_files(vault, roots) if roots else all_paths
        active_checker = checker or SpellChecker(store, languages=getattr(args, "languages", DEFAULT_LANGUAGES))
        active_checker.prepare(load_corpus(vault, all_paths), log=_log)
        analyzer = NoteAnalyzer(vault, active_checker, store)
        if getattr(args, "verbose", False) and isinstance(active_checker, SpellChecker):
            _print_verbose(active_checker, analyzer, paths)
        if output_format != "review":
            emit_structured(output_format, _collect_findings(analyzer, paths))
            return 0
        return _review(vault, paths, analyzer, store, reviewer or InteractiveReviewer())
    finally:
        if owned_state:
            store.close()


def _review(vault: Path, paths: Sequence[Path], analyzer: NoteAnalyzer, store: StateStore, reviewer: Reviewer) -> int:
    pending = store.pending_operations()
    if pending:
        print(f"warning: {len(pending)} interrupted operation(s) remain journaled:", file=sys.stderr)
        for operation in pending:
            completed = _journaled_completed_files(vault, operation)
            names = ", ".join(completed) if completed else "(none)"
            print(f"  operation {operation.id}: completed files: {names}", file=sys.stderr)
        print("  press u during review to undo a journaled operation.", file=sys.stderr)
    stats = ReviewStats()
    print(f"Reviewing {len(paths)} notes...")
    for index in range(len(paths)):
        try:
            if _review_note(paths, index, analyzer, store, reviewer, stats):
                print("Spellcheck paused; completed actions and decisions were kept.")
                return 0
        except KeyboardInterrupt:
            print("\nSpellcheck interrupted; completed actions and decisions were kept.", file=sys.stderr)
            return 130
        except NotesError as exc:
            print(f"warning: {exc}", file=sys.stderr)
            stats.notes_skipped += 1
    if stats.prompts == 0:
        print("No spelling proposals.")
    else:
        print(
            f"Spellcheck complete: {stats.occurrences_corrected} occurrence(s) corrected, "
            f"{stats.decisions_added} decision(s) saved, {stats.notes_skipped} note(s) skipped."
        )
    return 0
