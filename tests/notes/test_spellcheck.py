"""Review, persistence, and output behavior of ``notes spellcheck``.

These tests drive the review loop through a fake checker so they pin the UI
contract (grouping, a/A/n/d/u/q, journaling, structured output) independent
of dictionary contents; ``test_checker.py`` covers linguistic behavior.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from tool.notes import spellcheck
from tool.notes.checker import Finding
from tool.notes.state import StateStore


class FakeChecker:
    """Proposes every exact occurrence of each configured original."""

    def __init__(self, rules: dict[str, str | tuple[str, ...]], *, bulk_safe: bool = True) -> None:
        self.rules = rules
        self.bulk_safe = bulk_safe
        self.calls = 0

    def prepare(self, notes: Mapping[str, str], log: Callable[[str], None] | None = None) -> None:
        _ = (notes, log)

    def analyze(self, note_id: str, text: str, only: frozenset[str] | None = None) -> list[Finding]:
        self.calls += 1
        findings: list[Finding] = []
        for original, replacements in self.rules.items():
            if only is not None and original not in only:
                continue
            values = (replacements,) if isinstance(replacements, str) else replacements
            cursor = 0
            while (start := text.find(original, cursor)) != -1:
                end = start + len(original)
                findings.extend(
                    Finding(note_id, start, end, 1, start + 1, original, value, "fake", "en", self.bulk_safe)
                    for value in values
                )
                cursor = end
        return sorted(findings, key=lambda finding: (finding.start, finding.replacement))


class ScriptedReviewer:
    def __init__(self, actions: list[str], remembers: list[spellcheck.RememberChoice | None] | None = None) -> None:
        self.actions = list(actions)
        self.remembers = list(remembers or [])
        self.seen: list[tuple[str, str, str]] = []

    def decide(self, candidate: spellcheck.Candidate) -> str:
        self.seen.append((candidate.note_id, candidate.original, candidate.replacement))
        return self.actions.pop(0) if self.actions else "q"

    def remember(self, _candidate: spellcheck.Candidate) -> spellcheck.RememberChoice | None:
        return self.remembers.pop(0) if self.remembers else None


def write_vault(vault: Path, files: dict[str, str | bytes]) -> None:
    for name, content in files.items():
        path = vault / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="")


def run_vault(
    vault: Path,
    checker: FakeChecker,
    actions: list[str],
    *,
    remembers: list[spellcheck.RememberChoice | None] | None = None,
    state_path: Path | None = None,
) -> ScriptedReviewer:
    reviewer = ScriptedReviewer(actions, remembers)
    args = argparse.Namespace(vault=vault, state=state_path)
    assert spellcheck.run(args, checker=checker, reviewer=reviewer) in {0, 130}
    return reviewer


def test_review_rechecks_markdown_protection_and_identifier_boundaries(tmp_path: Path) -> None:
    checker = FakeChecker({"recieve": "receive"})
    text = "`recieve` [recieve](recieve.md) recieve recieve_count\n\nrecieve"
    assert len(checker.analyze("a.md", text)) == 6
    path = tmp_path / "a.md"
    path.write_text(text, encoding="utf-8", newline="")
    with StateStore(tmp_path / "state.sqlite3") as state:
        result = spellcheck.NoteAnalyzer(tmp_path, checker, state).analyze(path)
        assert result is not None
        assert [len(group.occurrences) for group in result.groups] == [3]


def test_group_repeated_occurrences_within_one_note(tmp_path: Path) -> None:
    write_vault(tmp_path, {"a.md": "recieve one. recieve two."})
    state_path = tmp_path / "state.sqlite3"
    reviewer = run_vault(tmp_path, FakeChecker({"recieve": "receive"}), ["q"], state_path=state_path)
    assert reviewer.seen == [("a.md", "recieve", "receive")]


def test_a_applies_current_note_only(tmp_path: Path) -> None:
    write_vault(tmp_path, {"a.md": "recieve here", "b.md": "recieve there"})
    checker = FakeChecker({"recieve": "receive"})
    reviewer = run_vault(tmp_path, checker, ["a", "q"], state_path=tmp_path / "state.sqlite3")
    assert (tmp_path / "a.md").read_text() == "receive here"
    assert (tmp_path / "b.md").read_text() == "recieve there"
    assert reviewer.seen == [("a.md", "recieve", "receive"), ("b.md", "recieve", "receive")]


def test_uppercase_a_applies_immediately_across_vault(tmp_path: Path) -> None:
    write_vault(
        tmp_path,
        {
            "a.md": "recieve here",
            "b.md": "recieve there and `recieve`",
            "c.md": "recieve_count stays technical",
        },
    )
    run_vault(tmp_path, FakeChecker({"recieve": "receive"}), ["A"], state_path=tmp_path / "state.sqlite3")
    assert (tmp_path / "a.md").read_text() == "receive here"
    assert (tmp_path / "b.md").read_text() == "receive there and `recieve`"
    assert (tmp_path / "c.md").read_text() == "recieve_count stays technical"


def test_n_persists_note_local_rejection_across_unrelated_edits(tmp_path: Path) -> None:
    write_vault(tmp_path, {"a.md": "recieve here", "b.md": "recieve there"})
    state_path = tmp_path / "state.sqlite3"
    checker = FakeChecker({"recieve": "receive"})
    run_vault(tmp_path, checker, ["n"], state_path=state_path)
    (tmp_path / "a.md").write_text("recieve here\nnew unrelated text", encoding="utf-8", newline="")
    reviewer = run_vault(tmp_path, checker, ["q"], state_path=state_path)
    assert reviewer.seen == [("b.md", "recieve", "receive")]


def test_rejecting_one_replacement_does_not_suppress_another(tmp_path: Path) -> None:
    write_vault(tmp_path, {"a.md": "recieve here"})
    reviewer = run_vault(tmp_path, FakeChecker({"recieve": ("receive", "recede")}), ["n", "q"], state_path=tmp_path / "state.sqlite3")
    assert reviewer.seen == [("a.md", "recieve", "recede"), ("a.md", "recieve", "receive")]


def test_d_remembers_exact_phrase_and_scope_without_splitting_words(tmp_path: Path) -> None:
    write_vault(tmp_path, {"a.md": "Human recieve Resource", "sub/b.md": "Human recieve Resource"})
    state_path = tmp_path / "state.sqlite3"
    choice = spellcheck.RememberChoice("Human recieve", "note", "a.md")
    run_vault(tmp_path, FakeChecker({"recieve": "receive"}), ["d", "q"], remembers=[choice], state_path=state_path)
    reviewer = run_vault(tmp_path, FakeChecker({"recieve": "receive"}), ["q"], state_path=state_path)
    assert reviewer.seen == [("sub/b.md", "recieve", "receive")]
    with StateStore(state_path) as state:
        decision = state.active_decisions()[0]
        assert decision.source == "Human recieve"


def test_directory_scope_is_explicit(tmp_path: Path) -> None:
    write_vault(tmp_path, {"sub/a.md": "recieve", "sub/b.md": "recieve", "c.md": "recieve"})
    state_path = tmp_path / "state.sqlite3"
    choice = spellcheck.RememberChoice("recieve", "directory", "sub")
    run_vault(tmp_path, FakeChecker({"recieve": "receive"}), ["d", "q"], remembers=[choice], state_path=state_path)
    reviewer = run_vault(tmp_path, FakeChecker({"recieve": "receive"}), ["q"], state_path=state_path)
    assert reviewer.seen == [("c.md", "recieve", "receive")]


def test_invalid_input_does_not_record_rejection(tmp_path: Path) -> None:
    write_vault(tmp_path, {"a.md": "recieve"})
    state_path = tmp_path / "state.sqlite3"
    run_vault(tmp_path, FakeChecker({"recieve": "receive"}), ["x", "n"], state_path=state_path)
    with StateStore(state_path) as state:
        assert len(state.active_decisions()) == 1


def test_non_tty_input_preserves_uppercase_a(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spellcheck.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda _prompt: "A")
    assert spellcheck._read_key() == "A"


def test_read_single_key_does_not_leak_trailing_enter_into_next_read(monkeypatch: pytest.MonkeyPatch) -> None:
    # A user pressing "A" then habitually hitting Enter queues a trailing "\n"
    # right after the key. If that byte isn't drained, it survives buffered in
    # sys.stdin and silently answers the *next* cooked-mode input() prompt (the
    # batch confirm) with an empty line, cancelling it without the user typing
    # anything.
    pty = pytest.importorskip("pty")
    master_fd, slave_fd = pty.openpty()
    stdin = os.fdopen(slave_fd, "r")
    monkeypatch.setattr(spellcheck.sys, "stdin", stdin)

    # tty.setraw() flushes any input queued before it switches modes (TCSAFLUSH),
    # so the keystroke must arrive after raw mode is already active, exactly like
    # a live terminal where the user types after the prompt is drawn.
    def feed_key() -> None:
        time.sleep(0.2)
        os.write(master_fd, b"A\n")

    feeder = threading.Thread(target=feed_key)
    feeder.start()
    try:
        assert spellcheck._read_key() == "A"
    finally:
        feeder.join()

    try:
        # Simulates the user later typing "yes" at the confirm prompt, well
        # after the key press above.
        os.write(master_fd, b"yes\n")
        assert stdin.readline() == "yes\n"
    finally:
        stdin.close()
        os.close(master_fd)


def test_remember_discards_a_delayed_enter_from_the_prior_keypress(monkeypatch: pytest.MonkeyPatch) -> None:
    # In practice a user's Enter keystroke can land a beat *after* the raw
    # single-key read for "d" already returned (real inter-keystroke gaps run
    # tens of milliseconds), too late for that read's own immediate drain.
    # remember()'s first input() prompt must still catch and discard it,
    # rather than silently reading an empty line as the phrase.
    pty = pytest.importorskip("pty")
    master_fd, slave_fd = pty.openpty()
    stdin = os.fdopen(slave_fd, "r")
    monkeypatch.setattr(spellcheck.sys, "stdin", stdin)

    def feed_key_then_delayed_enter() -> None:
        time.sleep(0.05)
        os.write(master_fd, b"d")
        time.sleep(0.1)
        os.write(master_fd, b"\n")

    feeder = threading.Thread(target=feed_key_then_delayed_enter)
    feeder.start()
    try:
        assert spellcheck._read_key() == "d"
    finally:
        feeder.join()

    def feed_phrase_and_scope() -> None:
        time.sleep(0.05)
        os.write(master_fd, b"custom phrase\nn\n")

    reviewer = spellcheck.InteractiveReviewer()
    candidate = spellcheck.Candidate(Path("a.md"), "a.md", "freinds", "friends")
    feeder2 = threading.Thread(target=feed_phrase_and_scope)
    feeder2.start()
    try:
        choice = reviewer.remember(candidate)
    finally:
        feeder2.join()
        stdin.close()
        os.close(master_fd)
    assert choice == spellcheck.RememberChoice("custom phrase", "note", "a.md")


def test_first_note_is_reviewable_before_later_notes_are_analyzed(tmp_path: Path) -> None:
    write_vault(tmp_path, {"a.md": "recieve", "b.md": "recieve"})
    checker = FakeChecker({"recieve": "receive"})

    class OrderReviewer(ScriptedReviewer):
        def decide(self, candidate: spellcheck.Candidate) -> str:
            assert checker.calls == 1
            return super().decide(candidate)

    reviewer = OrderReviewer(["q"])
    spellcheck.run(
        argparse.Namespace(vault=tmp_path, state=tmp_path / "state.sqlite3"), checker=checker, reviewer=reviewer
    )
    assert checker.calls == 1


def test_unicode_combining_marks_crlf_and_markdown_remain_intact(tmp_path: Path) -> None:
    text = "e\u0301 recieve\r\n`recieve`\r\n"
    path = tmp_path / "a.md"
    path.write_bytes(text.encode("utf-8"))
    os.chmod(path, 0o600)
    run_vault(tmp_path, FakeChecker({"recieve": "receive"}), ["a"], state_path=tmp_path / "state.sqlite3")
    assert path.read_bytes() == "e\u0301 receive\r\n`recieve`\r\n".encode("utf-8")
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_external_modification_is_detected_before_apply(tmp_path: Path) -> None:
    write_vault(tmp_path, {"a.md": "recieve"})
    state_path = tmp_path / "state.sqlite3"

    class ExternalReviewer(ScriptedReviewer):
        def decide(self, candidate: spellcheck.Candidate) -> str:
            if not self.seen:
                candidate.path.write_text("external", encoding="utf-8", newline="")
                self.seen.append((candidate.note_id, candidate.original, candidate.replacement))
                return "a"
            return "q"

    reviewer = ExternalReviewer([])
    spellcheck.run(argparse.Namespace(vault=tmp_path, state=state_path), checker=FakeChecker({"recieve": "receive"}), reviewer=reviewer)
    assert (tmp_path / "a.md").read_text() == "external"


def test_bulk_operation_is_one_undo_and_does_not_create_autocorrection(tmp_path: Path) -> None:
    write_vault(tmp_path, {"a.md": "recieve and seperate"})
    state_path = tmp_path / "state.sqlite3"
    run_vault(
        tmp_path,
        FakeChecker({"recieve": "receive", "seperate": "separate"}),
        ["A", "u", "q"],
        state_path=state_path,
    )
    assert (tmp_path / "a.md").read_text() == "recieve and seperate"
    with StateStore(state_path) as state:
        assert state.active_decisions() == []


def test_interrupted_bulk_has_recoverable_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_vault(tmp_path, {"a.md": "recieve", "b.md": "recieve"})
    state_path = tmp_path / "state.sqlite3"
    with StateStore(state_path) as state:
        analyzer = spellcheck.NoteAnalyzer(tmp_path, FakeChecker({"recieve": "receive"}), state)
        plan, _analyses = spellcheck._make_batch_plan(
            analyzer.analyze(tmp_path / "a.md").groups[0],
            [tmp_path / "a.md", tmp_path / "b.md"],
            analyzer,
        )
        original_write = spellcheck.atomic_write_bytes
        writes = 0

        def interrupt(path: Path, data: bytes) -> None:
            nonlocal writes
            writes += 1
            original_write(path, data)
            if writes == 1:
                raise KeyboardInterrupt

        monkeypatch.setattr(spellcheck, "atomic_write_bytes", interrupt)
        with pytest.raises(KeyboardInterrupt):
            spellcheck.apply_plan(tmp_path, plan, state, "vault-apply")
        assert state.pending_operations()
        monkeypatch.setattr(spellcheck, "atomic_write_bytes", original_write)
        restored, conflicts, _operation = spellcheck.undo_last(tmp_path, state)
        assert (restored, conflicts) == (1, 0)
    assert (tmp_path / "a.md").read_text() == "recieve"
    assert (tmp_path / "b.md").read_text() == "recieve"


def test_undo_detects_external_edit(tmp_path: Path) -> None:
    write_vault(tmp_path, {"a.md": "recieve"})
    state_path = tmp_path / "state.sqlite3"
    with StateStore(state_path) as state:
        analyzer = spellcheck.NoteAnalyzer(tmp_path, FakeChecker({"recieve": "receive"}), state)
        candidate = analyzer.analyze(tmp_path / "a.md").groups[0]
        spellcheck.apply_plan(tmp_path, spellcheck._candidate_plan(candidate), state, "note-apply")
        (tmp_path / "a.md").write_text("changed later", encoding="utf-8", newline="")
        restored, conflicts, _operation = spellcheck.undo_last(tmp_path, state)
        assert (restored, conflicts) == (0, 1)
        assert (tmp_path / "a.md").read_text() == "changed later"


def test_decision_inspection_and_removal_reexposes_finding(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state_path = tmp_path / "state.sqlite3"
    with StateStore(state_path) as state:
        decision_id = state.add_decision(
            kind="rejection",
            scope_kind="note",
            scope_value="a.md",
            source="recieve",
            replacement="receive",
        )
    from tool.notes.cli import build_parser

    args = build_parser().parse_args(["spellcheck", "decisions", "--state", str(state_path)])
    args.handler(args)
    assert str(decision_id) in capsys.readouterr().out
    args = build_parser().parse_args(
        ["spellcheck", "decisions", "--state", str(state_path), "--remove", str(decision_id)]
    )
    args.handler(args)
    with StateStore(state_path) as state:
        assert state.active_decisions() == []
        write_vault(tmp_path, {"a.md": "recieve"})
        analysis = spellcheck.NoteAnalyzer(tmp_path, FakeChecker({"recieve": "receive"}), state).analyze(
            tmp_path / "a.md"
        )
        assert analysis is not None
        assert analysis.groups


def test_uppercase_a_is_refused_for_context_dependent_corrections(tmp_path: Path) -> None:
    write_vault(tmp_path, {"a.md": "slgihtly here", "b.md": "slgihtly there"})
    reviewer = run_vault(
        tmp_path, FakeChecker({"slgihtly": "slightly"}, bulk_safe=False), ["A", "q"], state_path=tmp_path / "s.sqlite3"
    )
    assert (tmp_path / "a.md").read_text() == "slgihtly here"
    assert (tmp_path / "b.md").read_text() == "slgihtly there"
    # The refused A re-prompts the same proposal instead of moving on.
    assert reviewer.seen == [("a.md", "slgihtly", "slightly"), ("a.md", "slgihtly", "slightly")]


class ExplodingReviewer:
    def decide(self, candidate: spellcheck.Candidate) -> str:
        raise AssertionError(f"structured output must not prompt: {candidate}")


def run_structured(
    vault: Path, output_format: str, capsys: pytest.CaptureFixture[str], checker: FakeChecker | None = None
) -> str:
    before = {path: path.read_bytes() for path in vault.rglob("*.md")}
    args = argparse.Namespace(vault=vault, state=vault / "state.sqlite3", format=output_format, notes=[])
    status = spellcheck.run(
        args, checker=checker or FakeChecker({"recieve": "receive"}), reviewer=ExplodingReviewer()
    )
    assert status == 0
    assert {path: path.read_bytes() for path in vault.rglob("*.md")} == before
    out = capsys.readouterr().out
    assert "\033[" not in out
    return out


def test_json_output_is_sorted_complete_and_read_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_vault(tmp_path, {"b.md": "I recieve.", "a.md": "Café 😀 recieve and recieve"})
    findings = json.loads(run_structured(tmp_path, "json", capsys))
    assert [(item["path"], item["start"]) for item in findings] == [("a.md", 7), ("a.md", 19), ("b.md", 2)]
    assert findings[0] == {
        "path": "a.md",
        "line": 1,
        "column": 8,
        "start": 7,
        "end": 14,
        "original": "recieve",
        "replacement": "receive",
        "reason": "fake",
        "language": "en",
        "bulk_safe": True,
    }


def test_jsonl_output_respects_saved_rejections(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_vault(tmp_path, {"a.md": "recieve", "b.md": "recieve"})
    with StateStore(tmp_path / "state.sqlite3") as state:
        state.add_decision(kind="rejection", scope_kind="note", scope_value="a.md", source="recieve", replacement="receive")
    lines = run_structured(tmp_path, "jsonl", capsys).splitlines()
    assert [json.loads(line)["path"] for line in lines] == ["b.md"]


def test_diff_output_shows_every_proposed_edit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_vault(tmp_path, {"a.md": "keep\nI recieve it\nend", "b.md": "clean\n"})
    assert run_structured(tmp_path, "diff", capsys) == (
        "--- a/a.md\n+++ b/a.md\n@@ -1,3 +1,3 @@\n keep\n-I recieve it\n+I receive it\n end\n"
        "\\ No newline at end of file\n"
    )


def test_note_option_limits_output_to_selected_notes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_vault(tmp_path, {"a.md": "recieve", "sub/b.md": "recieve"})
    args = argparse.Namespace(vault=tmp_path, state=tmp_path / "s.sqlite3", format="jsonl", notes=[tmp_path / "sub"])
    assert spellcheck.run(args, checker=FakeChecker({"recieve": "receive"}), reviewer=ExplodingReviewer()) == 0
    assert [json.loads(line)["path"] for line in capsys.readouterr().out.splitlines()] == ["sub/b.md"]
