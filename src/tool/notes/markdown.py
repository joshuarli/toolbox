"""Conservative Markdown structural scanner for ``notes spellcheck``.

This module understands enough Markdown structure to guarantee document
integrity. It does no linguistic judgment: its only job is to identify
source regions in which spelling edits are forbidden.

All offsets refer to the original source text, so validated edits can be
spliced back byte-for-byte.

Protected regions (edits forbidden inside them):

* YAML front matter at the start of the note
* fenced code blocks (backtick and tilde fences)
* inline code spans, including variable-length backtick delimiters
* raw ``http(s)`` URLs
* Markdown link and image destinations
* link-definition destinations
* autolinks
* Obsidian wikilink targets and embeds
* HTML tags, comments, and ``script``/``style`` element bodies
* inline and block math (``$``/``$$``/``\\(``/``\\[``)
* indented code blocks
* slash-bearing path-like tokens (``a/b``, ``~/x``, ``./run.sh``)

Headings, paragraphs, list text, blockquotes, link labels, and wikilink
aliases stay eligible. When a construct is ambiguous the scanner prefers
over-protection: leaving prose unchecked is acceptable, corrupting
Markdown is not.
"""

from __future__ import annotations

import re

_FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})")
_FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\r?\n.*?\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_AUTOLINK_RE = re.compile(r"<(?:[A-Za-z][A-Za-z0-9+.-]*:[^<>\s]*|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})>")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^<>]*?>|<![A-Z][^<>]*>|<\?.*?\?>", re.DOTALL)
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^<>]*?>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_MATH_DISPLAY_RE = re.compile(r"\$\$.+?\$\$", re.DOTALL)
_MATH_BRACKET_RE = re.compile(r"\\\[.+?\\\]", re.DOTALL)
_MATH_PAREN_RE = re.compile(r"\\\((?:[^\\\n]|\\(?!\)))*?\\\)")
# Inline `$...$`: single-line, non-space adjacent delimiters, not `$$`.
_MATH_INLINE_RE = re.compile(r"(?<!\$)\$(?!\s|\$)([^$\n]+?)(?<!\s)\$(?!\$)")
_RAW_URL_RE = re.compile(r"https?://[^\s<>\"'`\]]+")
_PATH_TOKEN_RE = re.compile(
    r"(?:~|\.{1,2})?/[\w.+\-~]+(?:/[\w.+\-~]+)*|\b[\w.+\-~][\w.+\-~]*/[\w.+\-~]+(?:/[\w.+\-~]+)*"
)
_LINK_DEF_RE = re.compile(r"(?m)^ {0,3}\[[^\]\n]+\]:[ \t]*<?(\S+?)>?(?:[ \t]+.*)?$")
_LIST_ITEM_RE = re.compile(r"^(?:[*+-]|\d{1,9}[.)])\s")


def _is_escaped(text: str, pos: int) -> bool:
    count = 0
    i = pos - 1
    while i >= 0 and text[i] == "\\":
        count += 1
        i -= 1
    return count % 2 == 1


def _merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort ranges and merge overlapping or abutting ones."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _covered(ranges: list[tuple[int, int]], pos: int) -> bool:
    for start, end in ranges:
        if start <= pos < end:
            return True
        if start > pos:
            break
    return False


def _front_matter_range(text: str) -> tuple[int, int] | None:
    match = _FRONT_MATTER_RE.match(text)
    if match is None:
        return None
    return (0, match.end())


def _fenced_ranges(text: str) -> list[tuple[int, int]]:
    """Offsets of fenced code blocks (``` / ~~~), unclosed fence runs to EOF."""
    ranges: list[tuple[int, int]] = []
    open_char = ""
    open_len = 0
    open_start = 0
    offset = 0
    for line in text.splitlines(keepends=True):
        match = _FENCE_RE.match(line)
        if match is not None:
            run = match.group(2)
            char = run[0]
            rest = line[match.end() :]
            if not open_char:
                if char == "`" and "`" in rest:
                    pass  # info string containing a backtick is not a fence
                else:
                    open_char, open_len, open_start = char, len(run), offset
            elif char == open_char and len(run) >= open_len and rest.strip() == "":
                ranges.append((open_start, offset + len(line)))
                open_char, open_len = "", 0
        offset += len(line)
    if open_char:
        ranges.append((open_start, len(text)))
    return ranges


def _backtick_runs(text: str, skip: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """All unescaped backtick runs outside *skip* as ``(start, end)``."""
    runs: list[tuple[int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "\\":
            i += 2
            continue
        if text[i] != "`" or _covered(skip, i):
            i += 1
            continue
        j = i
        while j < n and text[j] == "`":
            j += 1
        runs.append((i, j))
        i = j
    return runs


def _inline_code_ranges(text: str, skip: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Variable-length backtick code spans; unmatched runs are left alone."""
    runs = _backtick_runs(text, skip)
    ranges: list[tuple[int, int]] = []
    used = [False] * len(runs)
    for k, (start, end) in enumerate(runs):
        if used[k]:
            continue
        want = end - start
        for m in range(k + 1, len(runs)):
            if used[m]:
                continue
            other_start, other_end = runs[m]
            if other_end - other_start != want:
                continue
            ranges.append((start, other_end))
            used[k] = used[m] = True
            break
    return ranges


def _raw_url_ranges(text: str, skip: list[tuple[int, int]]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for match in _RAW_URL_RE.finditer(text):
        if _covered(skip, match.start()):
            continue
        end = match.end()
        while end > match.start() and text[end - 1] in ".,;:!?)]}\"'":
            end -= 1
        if end > match.start():
            ranges.append((match.start(), end))
    return ranges


def _parse_link_destination(text: str, pos: int) -> tuple[int, int, int] | None:
    """Parse a destination at *pos*; return ``(dest_start, dest_end, embed_end)``."""
    n = len(text)
    i = pos
    while i < n and text[i] in " \t\n":
        i += 1
    if i < n and text[i] == "<":
        i += 1
        start = i
        while i < n and text[i] != ">":
            if text[i] == "\\":
                i += 2
                continue
            if text[i] == "\n" or text[i] == "<":
                return None
            i += 1
        if i >= n:
            return None
        dest: tuple[int, int] = (start, i)
        i += 1
    else:
        start = i
        depth = 0
        while i < n:
            char = text[i]
            if char == "\\":
                i += 2
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    break
                depth -= 1
            elif char in " \t\n" or ord(char) < 0x20:
                break
            i += 1
        dest = (start, i)
    j = i
    while j < n and text[j] in " \t\n":
        j += 1
    if j < n and text[j] == ")":
        return dest[0], dest[1], j + 1
    if j == i or not dest[0] < dest[1]:
        return None
    # Optional title before the closing paren; titles stay eligible.
    k = j
    if k < n and text[k] in "\"'":
        quote = text[k]
        k += 1
        while k < n and text[k] != quote:
            if text[k] == "\\":
                k += 2
                continue
            k += 1
        if k >= n:
            return None
        k += 1
    elif k < n and text[k] == "(":
        depth = 1
        k += 1
        while k < n and depth:
            if text[k] == "\\":
                k += 2
                continue
            if text[k] == "(":
                depth += 1
            elif text[k] == ")":
                depth -= 1
            k += 1
        if depth:
            return None
    else:
        return None
    while k < n and text[k] in " \t\n":
        k += 1
    if k >= n or text[k] != ")":
        return None
    return dest[0], dest[1], k + 1


def _link_destination_ranges(text: str, skip: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Spans of ``[...](destination)`` destinations; labels stay eligible."""
    ranges: list[tuple[int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if char in "![" and not _is_escaped(text, i):
            if char == "!":
                if i + 1 >= n or text[i + 1] != "[" or _covered(skip, i):
                    i += 1
                    continue
                j = i + 1
            else:
                if i + 1 < n and text[i + 1] == "[":
                    i += 1  # wikilink `[[...]]` is handled separately
                    continue
                if _covered(skip, i):
                    i += 1
                    continue
                j = i
            depth = 1
            j += 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "[":
                    depth += 1
                elif text[j] == "]":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if depth != 0 or j + 1 >= n or text[j + 1] != "(":
                i += 1
                continue
            parsed = _parse_link_destination(text, j + 2)
            if parsed is None:
                i += 1
                continue
            dest_start, dest_end, embed_end = parsed
            if dest_end > dest_start:
                ranges.append((dest_start, dest_end))
            i = embed_end
            continue
        i += 1
    for match in _LINK_DEF_RE.finditer(text):
        try:
            span = match.span(1)
        except IndexError:
            continue
        if span[1] > span[0] and not _covered(skip, span[0]):
            ranges.append(span)
    return ranges


def _autolink_ranges(text: str, skip: list[tuple[int, int]]) -> list[tuple[int, int]]:
    return [match.span() for match in _AUTOLINK_RE.finditer(text) if not _covered(skip, match.start())]


def _wikilink_ranges(text: str, skip: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Target spans of ``[[target]]``/``[[target|alias]]`` and ``![[embed]]``.

    For plain wikilinks only the target (up to the first ``|``) is
    protected; the alias is visible prose and stays eligible. Embeds
    (``![[...]]``) protect the whole interior, since ``|`` there starts
    a size suffix rather than readable text.
    """
    ranges: list[tuple[int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "\\":
            i += 2
            continue
        embed = text[i] == "!" and i + 2 < n and text[i + 1] == "[" and text[i + 2] == "["
        plain = text[i] == "[" and i + 1 < n and text[i + 1] == "[" and not _is_escaped(text, i)
        if not embed and not plain:
            i += 1
            continue
        open_end = i + 3 if embed else i + 2
        if _covered(skip, i):
            i += 1
            continue
        close = text.find("]]", open_end)
        if close == -1:
            i += 1
            continue
        if embed:
            ranges.append((open_end, close))
        else:
            sep = text.find("|", open_end, close)
            ranges.append((open_end, sep if sep != -1 else close))
        i = close + 2
    return ranges


def _html_ranges(text: str, skip: list[tuple[int, int]]) -> list[tuple[int, int]]:
    ranges = [match.span() for match in _SCRIPT_STYLE_RE.finditer(text) if not _covered(skip, match.start())]
    for pattern in (_HTML_COMMENT_RE, _HTML_TAG_RE):
        ranges.extend(
            match.span()
            for match in pattern.finditer(text)
            if not _covered(skip, match.start()) and not any(s <= match.start() and match.end() <= e for s, e in ranges)
        )
    return ranges


def _math_ranges(text: str, skip: list[tuple[int, int]]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for pattern in (_MATH_DISPLAY_RE, _MATH_BRACKET_RE, _MATH_PAREN_RE, _MATH_INLINE_RE):
        ranges.extend(match.span() for match in pattern.finditer(text) if not _covered(skip, match.start()))
    return ranges


def _path_ranges(text: str, skip: list[tuple[int, int]]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for match in _PATH_TOKEN_RE.finditer(text):
        token = match.group()
        if "/" not in token or token.strip("/") == "":
            continue
        if _covered(skip, match.start()):
            continue
        ranges.append(match.span())
    return ranges


def _indented_code_ranges(text: str, skip: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """4-space/tab-indented code lines following a blank line (or BOF)."""
    ranges: list[tuple[int, int]] = []
    offset = 0
    prev_blank = True
    prev_code = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        indented = line.startswith(("    ", "\t"))
        is_code = bool(stripped) and indented and (prev_blank or prev_code) and _LIST_ITEM_RE.match(stripped) is None
        if is_code and not _covered(skip, offset):
            ranges.append((offset, offset + len(line)))
        prev_code = is_code or (not stripped and prev_code)
        prev_blank = not stripped
        offset += len(line)
    return ranges


def find_protected_ranges(text: str) -> list[tuple[int, int]]:
    """Return sorted, merged ``(start, end)`` spans where edits are forbidden."""
    skip: list[tuple[int, int]] = []
    front = _front_matter_range(text)
    if front is not None:
        skip.append(front)
    skip.extend(_fenced_ranges(text))
    skip = _merge(skip)
    protected = list(skip)
    protected.extend(_inline_code_ranges(text, skip))
    inline_skip = _merge(protected)
    protected.extend(_raw_url_ranges(text, inline_skip))
    protected.extend(_link_destination_ranges(text, inline_skip))
    protected.extend(_autolink_ranges(text, inline_skip))
    protected.extend(_wikilink_ranges(text, inline_skip))
    protected.extend(_html_ranges(text, inline_skip))
    protected.extend(_math_ranges(text, inline_skip))
    protected.extend(_path_ranges(text, inline_skip))
    protected.extend(_indented_code_ranges(text, inline_skip))
    return _merge(protected)
