"""Deterministic tests for the local, note-oriented spellchecker."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from tool.notes import spellcheck
from tool.notes.detector import DETECTOR_VERSION, Finding, MechanicalTypoDetector, load_rules
from tool.notes.state import StateStore


class FakeDetector:
    version = "fake-v1"
    policy_version = "fake-policy-v1"

    def __init__(self, rules: dict[str, str | tuple[str, ...]]) -> None:
        self.rules = rules
        self.calls = 0

    def detect(self, text: str) -> list[Finding]:
        self.calls += 1
        findings: list[Finding] = []
        for original, replacements in self.rules.items():
            values = (replacements,) if isinstance(replacements, str) else replacements
            cursor = 0
            while True:
                start = text.find(original, cursor)
                if start == -1:
                    break
                findings.extend(
                    Finding(start, start + len(original), original, replacement, self.version) for replacement in values
                )
                cursor = start + len(original)
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
    detector: FakeDetector | MechanicalTypoDetector,
    actions: list[str],
    *,
    remembers: list[spellcheck.RememberChoice | None] | None = None,
    state_path: Path | None = None,
) -> ScriptedReviewer:
    reviewer = ScriptedReviewer(actions, remembers)
    args = argparse.Namespace(vault=vault, state=state_path)
    assert spellcheck.run(args, detector=detector, reviewer=reviewer) in {0, 130}
    return reviewer


def test_real_detector_preserves_intentional_title_and_unknown_terms() -> None:
    detector = MechanicalTypoDetector()
    text = "Human Resource by Nawapol Thamrongrattanarit and μSkia io_uring."
    assert detector.detect(text) == []


def test_real_detector_finds_mechanical_typos_and_not_grammar_or_punctuation() -> None:
    detector = MechanicalTypoDetector()
    findings = detector.detect("I recieved and recieve it. form → from. Hello! cuffs..?")
    assert [(finding.original, finding.replacement) for finding in findings] == [
        ("recieved", "received"),
        ("recieve", "receive"),
    ]


def test_real_dataset_is_pinned_and_ambiguous_rows_abstain() -> None:
    rules = load_rules()
    assert DETECTOR_VERSION == "typos-dict-0.14.1"
    assert rules["recieved"] == "received"
    assert "aache" not in rules
    assert "form" not in rules


def test_markdown_protection_and_identifier_boundaries(tmp_path: Path) -> None:
    detector = FakeDetector({"recieve": "receive"})
    text = "`recieve` [recieve](recieve.md) recieve recieve_count\n\nrecieve"
    findings = detector.detect(text)
    assert len(findings) == 6
    path = tmp_path / "a.md"
    path.write_text(text, encoding="utf-8", newline="")
    state = StateStore(tmp_path / "state.sqlite3")
    analysis = spellcheck.NoteAnalyzer(tmp_path, detector, state)
    try:
        result = analysis.analyze(path)
        assert result is not None
        assert [len(group.occurrences) for group in result.groups] == [3]
    finally:
        analysis.state.close()


def test_group_repeated_occurrences_within_one_note(tmp_path: Path) -> None:
    write_vault(tmp_path, {"a.md": "recieve one. recieve two."})
    state_path = tmp_path / "state.sqlite3"
    reviewer = run_vault(tmp_path, FakeDetector({"recieve": "receive"}), ["q"], state_path=state_path)
    assert reviewer.seen == [("a.md", "recieve", "receive")]


def test_a_applies_current_note_only(tmp_path: Path) -> None:
    write_vault(tmp_path, {"a.md": "recieve here", "b.md": "recieve there"})
    detector = FakeDetector({"recieve": "receive"})
    reviewer = run_vault(tmp_path, detector, ["a", "q"], state_path=tmp_path / "state.sqlite3")
    assert (tmp_path / "a.md").read_text() == "receive here"
    assert (tmp_path / "b.md").read_text() == "recieve there"
    assert reviewer.seen == [("a.md", "recieve", "receive"), ("b.md", "recieve", "receive")]


def test_uppercase_a_requires_second_affirmative_and_applies_exact_matches_across_vault(tmp_path: Path) -> None:
    write_vault(
        tmp_path,
        {
            "a.md": "recieve here",
            "b.md": "recieve there and `recieve`",
            "c.md": "recieve_count stays technical",
        },
    )
    run_vault(tmp_path, FakeDetector({"recieve": "receive"}), ["A", "A"], state_path=tmp_path / "state.sqlite3")
    assert (tmp_path / "a.md").read_text() == "receive here"
    assert (tmp_path / "b.md").read_text() == "receive there and `recieve`"
    assert (tmp_path / "c.md").read_text() == "recieve_count stays technical"


def test_cancelled_uppercase_a_does_not_record_rejection(tmp_path: Path) -> None:
    write_vault(tmp_path, {"a.md": "recieve here"})
    state_path = tmp_path / "state.sqlite3"
    reviewer = run_vault(tmp_path, FakeDetector({"recieve": "receive"}), ["A", "x", "n"], state_path=state_path)
    with StateStore(state_path) as state:
        assert [(decision.kind, decision.source) for decision in state.active_decisions()] == [
            ("rejection", "recieve")
        ]
    assert reviewer.seen[:2] == [("a.md", "recieve", "receive"), ("a.md", "recieve", "receive")]


def test_n_persists_note_local_rejection_across_unrelated_edits(tmp_path: Path) -> None:
    write_vault(tmp_path, {"a.md": "recieve here", "b.md": "recieve there"})
    state_path = tmp_path / "state.sqlite3"
    detector = FakeDetector({"recieve": "receive"})
    run_vault(tmp_path, detector, ["n"], state_path=state_path)
    (tmp_path / "a.md").write_text("recieve here\nnew unrelated text", encoding="utf-8", newline="")
    reviewer = run_vault(tmp_path, detector, ["q"], state_path=state_path)
    assert reviewer.seen == [("b.md", "recieve", "receive")]


def test_rejecting_one_replacement_does_not_suppress_another(tmp_path: Path) -> None:
    write_vault(tmp_path, {"a.md": "recieve here"})
    reviewer = run_vault(tmp_path, FakeDetector({"recieve": ("receive", "recede")}), ["n", "q"], state_path=tmp_path / "state.sqlite3")
    assert reviewer.seen == [("a.md", "recieve", "recede"), ("a.md", "recieve", "receive")]


def test_d_remembers_exact_phrase_and_scope_without_splitting_words(tmp_path: Path) -> None:
    write_vault(tmp_path, {"a.md": "Human recieve Resource", "sub/b.md": "Human recieve Resource"})
    state_path = tmp_path / "state.sqlite3"
    choice = spellcheck.RememberChoice("Human recieve", "note", "a.md")
    run_vault(tmp_path, FakeDetector({"recieve": "receive"}), ["d", "q"], remembers=[choice], state_path=state_path)
    reviewer = run_vault(tmp_path, FakeDetector({"recieve": "receive"}), ["q"], state_path=state_path)
    assert reviewer.seen == [("sub/b.md", "recieve", "receive")]
    with StateStore(state_path) as state:
        decision = state.active_decisions()[0]
        assert decision.source == "Human recieve"


def test_directory_scope_is_explicit(tmp_path: Path) -> None:
    write_vault(tmp_path, {"sub/a.md": "recieve", "sub/b.md": "recieve", "c.md": "recieve"})
    state_path = tmp_path / "state.sqlite3"
    choice = spellcheck.RememberChoice("recieve", "directory", "sub")
    run_vault(tmp_path, FakeDetector({"recieve": "receive"}), ["d", "q"], remembers=[choice], state_path=state_path)
    reviewer = run_vault(tmp_path, FakeDetector({"recieve": "receive"}), ["q"], state_path=state_path)
    assert reviewer.seen == [("c.md", "recieve", "receive")]


def test_invalid_input_does_not_record_rejection(tmp_path: Path) -> None:
    write_vault(tmp_path, {"a.md": "recieve"})
    state_path = tmp_path / "state.sqlite3"
    run_vault(tmp_path, FakeDetector({"recieve": "receive"}), ["x", "n"], state_path=state_path)
    with StateStore(state_path) as state:
        assert len(state.active_decisions()) == 1


def test_non_tty_input_preserves_uppercase_a(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spellcheck.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda _prompt: "A")
    assert spellcheck._read_key() == "A"


def test_cache_reuses_empty_and_nonempty_results_and_invalidates_changed_note(tmp_path: Path) -> None:
    write_vault(tmp_path, {"a.md": "clean", "b.md": "recieve"})
    state_path = tmp_path / "state.sqlite3"
    detector = FakeDetector({"recieve": "receive"})
    with StateStore(state_path) as state:
        analyzer = spellcheck.NoteAnalyzer(tmp_path, detector, state)
        assert analyzer.analyze(tmp_path / "a.md").groups == []
        assert analyzer.analyze(tmp_path / "b.md").groups[0].occurrences
        assert detector.calls == 2
        assert analyzer.analyze(tmp_path / "a.md").groups == []
        assert analyzer.analyze(tmp_path / "b.md").groups[0].occurrences
        assert detector.calls == 2
        (tmp_path / "a.md").write_text("recieve", encoding="utf-8", newline="")
        analyzer.analyze(tmp_path / "a.md")
        assert detector.calls == 3


def test_first_note_is_reviewable_before_later_detection(tmp_path: Path) -> None:
    write_vault(tmp_path, {"a.md": "recieve", "b.md": "recieve"})
    detector = FakeDetector({"recieve": "receive"})

    class OrderReviewer(ScriptedReviewer):
        def decide(self, candidate: spellcheck.Candidate) -> str:
            assert detector.calls == 1
            return super().decide(candidate)

    reviewer = OrderReviewer(["q"])
    spellcheck.run(
        argparse.Namespace(vault=tmp_path, state=tmp_path / "state.sqlite3"), detector=detector, reviewer=reviewer
    )
    assert detector.calls == 1


def test_unicode_combining_marks_crlf_and_markdown_remain_intact(tmp_path: Path) -> None:
    text = "e\u0301 recieve\r\n`recieve`\r\n"
    path = tmp_path / "a.md"
    path.write_bytes(text.encode("utf-8"))
    os.chmod(path, 0o600)
    run_vault(tmp_path, FakeDetector({"recieve": "receive"}), ["a"], state_path=tmp_path / "state.sqlite3")
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
    spellcheck.run(argparse.Namespace(vault=tmp_path, state=state_path), detector=FakeDetector({"recieve": "receive"}), reviewer=reviewer)
    assert (tmp_path / "a.md").read_text() == "external"


def test_bulk_operation_is_one_undo_and_does_not_create_autocorrection(tmp_path: Path) -> None:
    write_vault(tmp_path, {"a.md": "recieve and seperate"})
    state_path = tmp_path / "state.sqlite3"
    run_vault(
        tmp_path,
        FakeDetector({"recieve": "receive", "seperate": "separate"}),
        ["A", "A", "u", "q"],
        state_path=state_path,
    )
    assert (tmp_path / "a.md").read_text() == "recieve and seperate"
    with StateStore(state_path) as state:
        assert state.active_decisions() == []


def test_interrupted_bulk_has_recoverable_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_vault(tmp_path, {"a.md": "recieve", "b.md": "recieve"})
    state_path = tmp_path / "state.sqlite3"
    with StateStore(state_path) as state:
        analyzer = spellcheck.NoteAnalyzer(tmp_path, FakeDetector({"recieve": "receive"}), state)
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
        analyzer = spellcheck.NoteAnalyzer(tmp_path, FakeDetector({"recieve": "receive"}), state)
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
        analysis = spellcheck.NoteAnalyzer(tmp_path, FakeDetector({"recieve": "receive"}), state).analyze(
            tmp_path / "a.md"
        )
        assert analysis is not None
        assert analysis.groups


def test_default_run_never_imports_foundation_models_or_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_vault(tmp_path, {"a.md": "clean"})
    import sys

    monkeypatch.delitem(sys.modules, "tool.notes.foundation_models", raising=False)
    assert spellcheck.run(argparse.Namespace(vault=tmp_path, state=tmp_path / "state.sqlite3")) == 0
    assert "tool.notes.foundation_models" not in sys.modules
