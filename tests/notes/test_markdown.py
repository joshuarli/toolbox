"""Tests for the conservative Markdown structural scanner."""

import itertools

from tool.notes import markdown


def covered(text: str) -> list[str]:
    return [text[start:end] for start, end in markdown.find_protected_ranges(text)]


def test_front_matter_protected() -> None:
    text = "---\ntitle: recieve notes\ntags: [a]\n---\n\nBody with recieve here.\n"
    spans = covered(text)
    assert "---\ntitle: recieve notes\ntags: [a]\n---\n" in spans
    assert not any("Body" in span for span in spans)


def test_front_matter_dot_close_and_unclosed() -> None:
    assert covered("---\na: recieve\n...\nBody\n") == ["---\na: recieve\n...\n"]
    # No closing delimiter: not front matter, body stays eligible.
    assert covered("---\nBody with recieve\n") == []


def test_fenced_backtick_and_tilde() -> None:
    text = "ok\n\n```python\ndef recieve():\n    ...\n```\n\nmid\n\n~~~\ndef recieve():\n~~~\n\ntail\n"
    spans = covered(text)
    assert "```python\ndef recieve():\n    ...\n```\n" in spans
    assert "~~~\ndef recieve():\n~~~\n" in spans
    assert not any("mid" in span or span == "tail\n" for span in spans)


def test_unclosed_fence_runs_to_eof() -> None:
    text = "Body\n```\ndef recieve():\n"
    spans = covered(text)
    assert spans == ["```\ndef recieve():\n"]


def test_longer_closing_fence_and_info_backtick() -> None:
    text = "````\ncode recieve\n`````\nnot code\n`````\n"
    spans = covered(text)
    assert "````\ncode recieve\n`````\n" in spans
    assert not any("not code" in span for span in spans)
    # Info string containing a backtick is not an opener.
    assert covered("``` `weird\nrecieve\n") == []


def test_inline_code_spans() -> None:
    assert covered("use `recieve()` here") == ["`recieve()`"]
    assert covered("a ``code ` recieve`` b") == ["``code ` recieve``"]
    assert covered("a `unclosed recieve") == []
    assert covered("`` `recieve` `` tail") == ["`` `recieve` ``"]


def test_raw_urls() -> None:
    spans = covered("see https://example.com/recieve/docs now")
    assert spans == ["https://example.com/recieve/docs"]
    # Trailing prose punctuation is not part of the URL.
    assert covered("(see https://example.com/a).") == ["https://example.com/a"]


def test_link_and_image_destinations() -> None:
    text = "See [reciever docs](https://example.com/reciever/docs) and ![alt](img/recieve.png)."
    spans = covered(text)
    assert "https://example.com/reciever/docs" in spans
    assert "img/recieve.png" in spans
    assert not any("reciever docs" in span for span in spans)
    assert not any(span == "alt" for span in spans)


def test_link_with_title_and_angle_destination() -> None:
    text = '[a](https://x.test/recieve "title recieve") and [b](<https://x.test/c d>)'
    spans = covered(text)
    assert "https://x.test/recieve" in spans
    assert any("https://x.test/c d" in span for span in spans)


def test_link_definition_destination() -> None:
    text = "See [docs][ref] for recieve.\n\n[ref]: https://example.com/recieve\n"
    assert "https://example.com/recieve" in covered(text)


def test_autolinks() -> None:
    assert covered("mail <user@example.com> now") == ["<user@example.com>"]
    assert covered("see <https://example.com/recieve>") == ["<https://example.com/recieve>"]
    assert covered("a < b and c") == []


def test_wikilink_target_protected_alias_eligible() -> None:
    spans = covered("See [[recieve-target|recieve alias]] now.")
    assert spans == ["recieve-target"]
    assert covered("See [[recieve-target]] now.") == ["recieve-target"]


def test_obsidian_embed_fully_protected() -> None:
    assert covered("![[recieve-diagram.webp]]") == ["recieve-diagram.webp"]
    assert covered("![[recieve-diagram.webp|300]]") == ["recieve-diagram.webp|300"]


def test_html_tags_protected_text_eligible() -> None:
    spans = covered('Use <kbd>recieve</kbd> and <img src="recieve.png" /> now.')
    assert "<kbd>" in spans
    assert "</kbd>" in spans
    assert '<img src="recieve.png" />' in spans
    assert not any(span == "recieve" for span in spans)
    assert covered("<!-- recieve comment -->") == ["<!-- recieve comment -->"]


def test_script_body_protected() -> None:
    text = "<script>\nvar recieve = 1;\n</script>\n\nBody recieve.\n"
    spans = covered(text)
    assert any("var recieve" in span for span in spans)
    assert not any("Body" in span for span in spans)


def test_math_protected() -> None:
    assert covered("Inline $x + recieve$ here.") == ["$x + recieve$"]
    assert covered("Display $$\\frac{recieve}{2}$$ here.") == ["$$\\frac{recieve}{2}$$"]
    assert covered("Also \\(recieve\\) and \\[recieve\\] here.") == ["\\(recieve\\)", "\\[recieve\\]"]
    # Bare currency amounts are not math.
    assert covered("It costs $5 and $10 total.") == []


def test_path_like_tokens_protected() -> None:
    assert covered("edit a/b/c now") == ["a/b/c"]
    assert covered("in ~/notes/x.md now") == ["~/notes/x.md"]
    assert covered("run ./scripts/go.sh now") == ["./scripts/go.sh"]
    assert covered("see pyproject.toml now") == []


def test_indented_code_protected() -> None:
    text = "Para.\n\n    def recieve():\n        ...\n\nNext recieve.\n"
    spans = covered(text)
    assert any("def recieve" in span for span in spans)
    assert not any("Next" in span for span in spans)


def test_ranges_sorted_and_merged() -> None:
    ranges = markdown.find_protected_ranges("`a` <https://x.test/a> [[t]]")
    assert ranges == sorted(ranges)
    for (_start1, end1), (start2, _end2) in itertools.pairwise(ranges):
        assert end1 <= start2
