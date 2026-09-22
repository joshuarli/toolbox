"""Behavior of the spelling core against the real macOS system dictionaries.

These tests exercise ``NSSpellChecker`` itself, not a stand-in, because what
the system dictionary accepts (names, places, French) is exactly the behavior
under test. Each test gets a fresh state database so no cached verdict leaks
between tests.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

pytest.importorskip("AppKit")

from tool.notes.checker import Finding, SpellChecker, content_hash
from tool.notes.speller import SystemSpeller
from tool.notes.state import StateStore


@pytest.fixture(scope="module")
def speller() -> SystemSpeller:
    return SystemSpeller()


class CountingSpeller:
    """Delegates to the system speller and counts every service round trip."""

    def __init__(self, inner: SystemSpeller) -> None:
        self.inner = inner
        self.key = inner.key
        self.unknown_calls = 0
        self.guess_calls = 0

    def unknown(self, words: Iterable[str], language: str) -> set[str]:
        self.unknown_calls += 1
        return self.inner.unknown(words, language)

    def guesses(self, word: str, language: str) -> list[str]:
        self.guess_calls += 1
        return self.inner.guesses(word, language)


def check(tmp_path: Path, speller: SystemSpeller, text: str, others: dict[str, str] | None = None) -> list[Finding]:
    with StateStore(tmp_path / "state.sqlite3") as store:
        checker = SpellChecker(store, speller)
        checker.prepare({"note.md": text, **(others or {})})
        return checker.analyze("note.md", text)


def pairs(findings: list[Finding]) -> list[tuple[str, str]]:
    return [(finding.original, finding.replacement) for finding in findings]


def test_french_inside_english_is_not_flagged(tmp_path: Path, speller: SystemSpeller) -> None:
    text = (
        "I visited Château de Chambord and it was magnifique.\n"
        "We ordered crème brûlée, a pain au chocolat, and a café au lait.\n"
        "Nous avons mangé une tarte aux pommes délicieuse avec des amis.\n"
    )
    assert check(tmp_path, speller, text) == []


def test_obvious_english_typos_are_caught_and_bulk_safe(tmp_path: Path, speller: SystemSpeller) -> None:
    findings = check(tmp_path, speller, "I will recieve the enviroment report tomorrow.")
    assert pairs(findings) == [("recieve", "receive"), ("enviroment", "environment")]
    assert all(finding.bulk_safe and finding.language == "en" for finding in findings)


def test_french_typo_in_a_french_sentence_uses_french_guesses(tmp_path: Path, speller: SystemSpeller) -> None:
    findings = check(tmp_path, speller, "Je lui ai dit bonjor et nous avons mangé ensemble.")
    assert pairs(findings) == [("bonjor", "bonjour")]
    assert findings[0].language == "fr"
    assert not findings[0].bulk_safe


def test_sentence_initial_capitalized_typo_keeps_its_case(tmp_path: Path, speller: SystemSpeller) -> None:
    assert pairs(check(tmp_path, speller, "Recieve it later. Then we recieve more.")) == [
        ("Recieve", "Receive"),
        ("recieve", "receive"),
    ]


def test_names_places_and_titles_are_left_alone(tmp_path: Path, speller: SystemSpeller) -> None:
    text = (
        "Human Resource by Nawapol Thamrongrattanarit is a quiet film.\n"
        "We watched François Truffaut films after visiting Château de Chambord.\n"
        "Paris is in Île-de-France. Flights to Reykjavík and São Tomé were cheap.\n"
        "Nawapol directed it. We drank Lagunitas near Mount Tamalpais.\n"
    )
    assert check(tmp_path, speller, text) == []


def test_technical_identifiers_are_untouched(tmp_path: Path, speller: SystemSpeller) -> None:
    text = "Use io_uring with pyproject.toml on aarch64, then run Qwen3.5 via llama.cpp and recieve_count."
    assert check(tmp_path, speller, text) == []


def test_no_grammar_style_case_or_punctuation_rewrites(tmp_path: Path, speller: SystemSpeller) -> None:
    text = (
        "Human Resource was good. I copied it form the document.\n"
        "dont worry, its fine and youre right. i think so!! Colour and color are both fine.\n"
    )
    assert check(tmp_path, speller, text) == []


def test_single_typing_slip_needs_a_target_word_the_vault_uses(tmp_path: Path, speller: SystemSpeller) -> None:
    text = "The glaze was slgihtly sour."
    assert check(tmp_path / "bare", speller, text) == []
    findings = check(tmp_path / "used", speller, text, {"other.md": "It is slightly better."})
    assert pairs(findings) == [("slgihtly", "slightly")]
    assert not findings[0].bulk_safe


def test_unknown_word_used_often_in_the_vault_is_vocabulary(tmp_path: Path, speller: SystemSpeller) -> None:
    others = {f"n{index}.md": "It is slightly better, slgihtly." for index in range(3)}
    assert check(tmp_path, speller, "The glaze was slgihtly sour.", others) == []


def test_markdown_code_urls_and_links_are_protected(tmp_path: Path, speller: SystemSpeller) -> None:
    text = (
        "---\ntitle: recieve\n---\n"
        "`recieve` then https://example.com/recieve and [[recieve]] and <b title='recieve'>x</b>\n"
        "```\nrecieve\n```\n"
        "Finally I recieve it.\n"
    )
    findings = check(tmp_path, speller, text)
    assert pairs(findings) == [("recieve", "receive")]
    assert findings[0].start == text.rindex("recieve")


def test_offsets_are_unicode_code_points(tmp_path: Path, speller: SystemSpeller) -> None:
    text = "Café naïve 😀 日本語 —\nthen I recieve it."
    (finding,) = check(tmp_path, speller, text)
    assert text[finding.start : finding.end] == "recieve"
    assert (finding.line, finding.column) == (2, len("then I ") + 1)


def test_cached_rerun_makes_no_system_calls_and_rescans_only_changed_notes(
    tmp_path: Path, speller: SystemSpeller
) -> None:
    notes = {"a.md": "I will recieve it.", "b.md": "Nothing to see here."}
    counting = CountingSpeller(speller)
    with StateStore(tmp_path / "state.sqlite3") as store:
        first = SpellChecker(store, counting)
        first.prepare(notes)
        assert pairs(first.analyze("a.md", notes["a.md"])) == [("recieve", "receive")]
        assert counting.unknown_calls
        assert counting.guess_calls

        counting.unknown_calls = counting.guess_calls = 0
        second = SpellChecker(store, counting)
        second.prepare(notes)
        assert pairs(second.analyze("a.md", notes["a.md"])) == [("recieve", "receive")]
        assert (counting.unknown_calls, counting.guess_calls) == (0, 0)

        scan_key = second._scan_key
        unchanged = store.note_scans(scan_key)["a.md"]
        edited = {**notes, "b.md": "A brand new zorblaxian word."}
        third = SpellChecker(store, counting)
        third.prepare(edited)
        assert third.analyze("b.md", edited["b.md"]) == []
        assert counting.unknown_calls > 0
        cached = store.note_scans(scan_key)
        assert cached["a.md"] == unchanged
        assert cached["b.md"][0] == content_hash(edited["b.md"])


def test_explain_word_reports_the_decision_trail(tmp_path: Path, speller: SystemSpeller) -> None:
    with StateStore(tmp_path / "state.sqlite3") as store:
        checker = SpellChecker(store, speller)
        checker.prepare({})
        explained = "\n".join(checker.explain_word("recieve"))
        assert "typos-dict: recieve → receive" in explained
        assert "recieve → receive" in explained
        assert "accepted; never flagged" in "\n".join(checker.explain_word("magnifique"))
