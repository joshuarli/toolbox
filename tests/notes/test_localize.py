import argparse
import functools
import hashlib
import http.server
import shutil
import threading
from pathlib import Path

import pytest

from tool.notes import localize
from tool.notes.common import NotesError
from tool.notes.localize import ImageError

# 1x1 white PNG.
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "00000009704859730000000100000001004f25c4d60000000c49444154789c63"
    "fcffff3f00060202ff896f02780000000049454e44ae426082"
)
MIN_WEBP = b"RIFF\x0e\x00\x00\x00WEBPpayload"

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def urls(text: str) -> list[str]:
    return [o.url for o in localize.find_remote_images(text)]


def test_basic_and_title() -> None:
    assert urls("![](https://x.test/a.png)") == ["https://x.test/a.png"]
    assert urls('![alt](https://x.test/a.png "title")') == ["https://x.test/a.png"]
    assert urls("![a](<https://x.test/a b.png>)") == ["https://x.test/a b.png"]
    assert urls("![a](https://x.test/a(1).png)") == ["https://x.test/a(1).png"]
    assert urls("![[local.webp]]") == []
    assert urls("![](z-images/a.webp)") == []
    assert urls("![](data:image/png;base64,AAA)") == []
    assert urls("![](ftp://x.test/a.png)") == []
    assert urls("![](https://x.test/a.png)\n![](https://y.test/b.jpg)") == [
        "https://x.test/a.png",
        "https://y.test/b.jpg",
    ]


def test_skips_code() -> None:
    assert urls("```\n![](https://x.test/a.png)\n```\n") == []
    assert urls("~~~\n![](https://x.test/a.png)\n~~~\n") == []
    assert urls("`![](https://x.test/a.png)`") == []
    assert urls("before ![](https://x.test/ok.png)\n```\n![](https://x.test/a.png)\n```\n") == ["https://x.test/ok.png"]
    # Unclosed fence: the rest of the note is code.
    assert urls("text\n```\n![](https://x.test/a.png)\n") == []
    # Escaped bang is not an embed.
    assert urls("\\![](https://x.test/a.png)") == []


def test_parser_edges() -> None:
    # Nested brackets in alt text.
    assert urls("![a [b] c](https://x.test/1.png)") == ["https://x.test/1.png"]
    # Single-quoted and parenthesized titles.
    assert urls("![a](https://x.test/1.png 't')") == ["https://x.test/1.png"]
    assert urls("![a](https://x.test/1.png (t))") == ["https://x.test/1.png"]
    # Angle destination with a title.
    assert urls('![a](<https://x.test/1.png> "t")') == ["https://x.test/1.png"]
    # Unterminated embeds are not images.
    assert urls("![](https://x.test/1.png") == []
    assert urls("![alt]") == []
    assert urls("![alt][ref]") == []
    assert urls("[![alt](https://x.test/1.png)](https://x.test/page)") == ["https://x.test/1.png"]


def test_remote_url() -> None:
    assert localize.remote_url("HTTP://X.TEST/A.PNG") == "HTTP://X.TEST/A.PNG"
    assert localize.remote_url("  https://x.test/a.png  ") == "https://x.test/a.png"
    assert localize.remote_url("#frag") is None
    assert localize.remote_url("") is None
    assert localize.remote_url("/local/a.png") is None


def test_remote_url_unescapes_only_punctuation() -> None:
    assert localize.remote_url("https://x.test/a\\-b.png") == "https://x.test/a-b.png"
    # `\n` is not a punctuation escape: the backslash is preserved.
    assert localize.remote_url("https://x.test/a\\nb.png") == "https://x.test/a\\nb.png"


def test_skippable_content_type() -> None:
    assert localize.is_skippable_content_type("text/html; charset=utf-8")
    assert localize.is_skippable_content_type("application/xhtml+xml")
    assert not localize.is_skippable_content_type("image/png")
    assert not localize.is_skippable_content_type("image/svg+xml")
    assert not localize.is_skippable_content_type("")


def test_apply_localizations() -> None:
    text = "a ![](https://x.test/1.png) b ![](https://x.test/2.png) c"
    updated, replaced = localize.apply_localizations(text, {"https://x.test/1.png": "![[aaa.webp]]"})
    assert updated == "a ![[aaa.webp]] b ![](https://x.test/2.png) c"
    assert replaced == 1
    assert localize.apply_localizations(text, {}) == (text, 0)
    assert localize.apply_localizations(text, {"https://nope.test/x.png": "![[z.webp]]"}) == (text, 0)


def test_occurrences_carry_offsets() -> None:
    text = "xx ![](https://x.test/1.png) yy"
    (occurrence,) = localize.find_remote_images(text)
    assert text[occurrence.start : occurrence.end] == "![](https://x.test/1.png)"
    assert occurrence.url == "https://x.test/1.png"


def test_is_page_url() -> None:
    pages = [
        "https://www.youtube.com/watch?v=AM1HJUC7YNg",
        "HTTPS://WWW.YOUTUBE.COM/WATCH?V=AM1HJUC7YNg",
        "https://m.youtube.com/watch?v=AM1HJUC7YNg",
        "https://www.youtube.com/shorts/abc123",
        "https://www.youtube.com/live/abc123",
        "https://www.youtube.com/embed/abc123",
        "https://www.youtube.com/playlist?list=abc123",
        "https://youtu.be/AM1HJUC7YNg",
        "https://youtu.be/AM1HJUC7YNg?t=42",
        "https://vimeo.com/123456789",
        "https://www.vimeo.com/123456789",
        "https://player.vimeo.com/video/123456789",
        "https://x.com/i/status/2056510566018203971",
        "https://x.com/someuser/status/2056510566018203971",
        "https://twitter.com/someuser/status/2056510566018203971",
        "https://mobile.twitter.com/someuser/status/2056510566018203971",
    ]
    for url in pages:
        assert localize.is_page_url(url), url
    images = [
        "https://i.ytimg.com/vi/AM1HJUC7YNg/hqdefault.jpg",
        "https://img.youtube.com/vi/AM1HJUC7YNg/0.jpg",
        "https://i.vimeocdn.com/video/123456789_640.jpg",
        "https://pbs.twimg.com/media/HJ0q6o6aYAEM_ej?format=jpg&name=large",
        "https://substackcdn.com/image/fetch/abc123",
        "https://youtu.be.evil.com/AM1HJUC7YNg",
        "https://www.youtube.com.evil.com/watch?v=1",
        "https://www.youtube.com/watchabc",
        "https://x.com/someuser",
    ]
    for url in images:
        assert not localize.is_page_url(url), url


def test_page_embeds_are_skipped_not_localized() -> None:
    text = (
        "![](https://www.youtube.com/watch?v=AM1HJUC7YNg)\n"
        "![](https://x.com/i/status/1)\n"
        "![](https://x.test/real.png)\n"
    )
    assert [o.url for o in localize.find_remote_images(text)] == ["https://x.test/real.png"]
    assert localize.find_page_urls(text) == [
        "https://www.youtube.com/watch?v=AM1HJUC7YNg",
        "https://x.com/i/status/1",
    ]


def test_collect_markdown_files(tmp_path: Path) -> None:
    vault = tmp_path / "Notes"
    (vault / "sub").mkdir(parents=True)
    (vault / ".trash").mkdir()
    (vault / ".obsidian").mkdir()
    keep = vault / "a.md"
    keep.write_text("x")
    (vault / "sub" / "b.md").write_text("y")
    (vault / ".trash" / "t.md").write_text("z")
    (vault / ".obsidian" / "o.md").write_text("z")
    assert localize.collect_markdown_files(vault) == [keep, vault / "sub" / "b.md"]
    assert localize.collect_markdown_files(vault, [vault / "sub"]) == [vault / "sub" / "b.md"]


def test_webp_helpers() -> None:
    assert localize.is_webp_bytes(MIN_WEBP)
    assert not localize.is_webp_bytes(TINY_PNG)
    assert not localize.is_webp_bytes(b"short")
    assert localize.convert_to_webp(MIN_WEBP) == MIN_WEBP
    name = localize.sha256_name(b"data")
    assert name == hashlib.sha256(b"data").hexdigest() + ".webp"


@needs_ffmpeg
def test_convert_png_to_webp() -> None:
    out = localize.convert_to_webp(TINY_PNG)
    assert localize.is_webp_bytes(out)


@needs_ffmpeg
def test_convert_garbage_raises() -> None:
    with pytest.raises(ImageError, match="webp conversion failed"):
        localize.convert_to_webp(b"not an image at all" * 10)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def http_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "srv"
    directory.mkdir()
    (directory / "img.png").write_bytes(TINY_PNG)
    (directory / "page.html").write_text("<html></html>")
    return directory


def _serve(directory: Path) -> tuple[http.server.ThreadingHTTPServer, int]:
    handler = functools.partial(_QuietHandler, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def test_download_image(http_dir: Path) -> None:
    server, port = _serve(http_dir)
    try:
        body, content_type = localize.download_image(f"http://127.0.0.1:{port}/img.png")
        assert body == TINY_PNG
        assert "image/png" in content_type
        with pytest.raises(ImageError, match="HTTP 404"):
            localize.download_image(f"http://127.0.0.1:{port}/missing.png")
    finally:
        server.shutdown()
        server.server_close()


def test_download_retries_then_succeeds(http_dir: Path) -> None:
    attempts = 0

    class Flaky(_QuietHandler):
        def do_GET(self) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                self.send_error(503)
            else:
                super().do_GET()

    handler = functools.partial(Flaky, directory=str(http_dir))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        body, _ = localize.download_image(f"http://127.0.0.1:{server.server_address[1]}/img.png")
        assert body == TINY_PNG
        assert attempts == 2
    finally:
        server.shutdown()
        server.server_close()


def test_download_refuses_oversize(http_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(localize, "_MAX_DOWNLOAD_BYTES", 10)
    server, port = _serve(http_dir)
    try:
        with pytest.raises(ImageError, match="exceeds"):
            localize.download_image(f"http://127.0.0.1:{port}/img.png")
    finally:
        server.shutdown()
        server.server_close()


@needs_ffmpeg
def test_fetch_and_store_dedups(http_dir: Path, tmp_path: Path) -> None:
    server, port = _serve(http_dir)
    try:
        folder = tmp_path / "z-images"
        folder.mkdir()
        name = localize.fetch_and_store(f"http://127.0.0.1:{port}/img.png", folder)
        assert name.endswith(".webp")
        assert (folder / name).is_file()
        assert localize.fetch_and_store(f"http://127.0.0.1:{port}/img.png", folder) == name
        with pytest.raises(ImageError, match="non-image"):
            localize.fetch_and_store(f"http://127.0.0.1:{port}/page.html", folder)
    finally:
        server.shutdown()
        server.server_close()


def _localize_args(vault: Path, *, apply: bool = False, paths: list[Path] | None = None) -> argparse.Namespace:
    return argparse.Namespace(vault=vault, apply=apply, jobs=2, quality=85, paths=paths or [])


def test_run_dry_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = tmp_path / "Notes"
    vault.mkdir()
    (vault / "n.md").write_text("look ![](https://x.test/1.png) and ![](https://x.test/2.png)\n")
    assert localize.run(_localize_args(vault)) == 0
    out = capsys.readouterr().out
    assert "https://x.test/1.png" in out
    assert "https://x.test/2.png" in out
    assert "would be downloaded" in out
    assert not (vault / "z-images").exists()


def test_run_no_remote(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = tmp_path / "Notes"
    vault.mkdir()
    (vault / "n.md").write_text("nothing here ![[a.webp]]\n")
    assert localize.run(_localize_args(vault)) == 0
    assert "No remote images" in capsys.readouterr().out


def test_run_dry_run_lists_skipped_pages(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = tmp_path / "Notes"
    vault.mkdir()
    (vault / "n.md").write_text("![](https://www.youtube.com/watch?v=AM1HJUC7YNg)\n![](https://x.test/1.png)\n")
    assert localize.run(_localize_args(vault)) == 0
    out = capsys.readouterr().out
    assert "https://x.test/1.png" in out
    assert "1 video/page embeds skipped (not images)" in out
    assert "https://www.youtube.com/watch?v=AM1HJUC7YNg" in out


def test_run_only_pages(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = tmp_path / "Notes"
    vault.mkdir()
    (vault / "n.md").write_text("![](https://www.youtube.com/watch?v=AM1HJUC7YNg)\n")
    assert localize.run(_localize_args(vault)) == 0
    assert "No localizable images found (1 video/page embeds skipped)" in capsys.readouterr().out
    assert not (vault / "z-images").exists()


@needs_ffmpeg
def test_run_apply_rewrites(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "Notes"
    vault.mkdir()
    note = vault / "n.md"
    note.write_text("a ![](https://x.test/1.png) b ![](https://x.test/1.png) c ![](https://bad.test/x.png)\n")

    def _png_for(url: str, **kwargs: object) -> tuple[bytes, str]:
        if "bad.test" in url:
            raise ImageError("HTTP 404")
        return TINY_PNG, "image/png"

    monkeypatch.setattr(localize, "download_image", _png_for)

    assert localize.run(_localize_args(vault, apply=True)) == 0
    out = capsys.readouterr().out
    assert "rewrote n.md (2 images)" in out
    assert "skipped https://bad.test/x.png (HTTP 404)" in out
    text = note.read_text()
    assert "https://x.test/1.png" not in text
    assert "https://bad.test/x.png" in text
    assert text.count("![[") == 2
    stored = list((vault / "z-images").glob("*.webp"))
    assert len(stored) == 1
    # Same content hashes to the same name: a rerun finds no remote embeds left.
    content = note.read_text()
    assert localize.run(_localize_args(vault, apply=True)) == 0
    assert note.read_text() == content


def test_run_bad_quality_is_validated_first(tmp_path: Path) -> None:
    vault = tmp_path / "Notes"
    vault.mkdir()
    args = _localize_args(vault, apply=True)
    args.quality = 0
    with pytest.raises(NotesError, match="invalid --quality"):
        localize.run(args)


def test_resolve_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = (tmp_path / "Notes").resolve()
    (vault / "sub").mkdir(parents=True)
    note = vault / "sub" / "n.md"
    note.write_text("x")
    # Absolute paths pass through.
    assert localize.resolve_roots(vault, [note]) == [note]
    # Relative paths fall back to the vault when absent from cwd.
    monkeypatch.chdir(tmp_path)
    assert localize.resolve_roots(vault, [Path("sub")]) == [vault / "sub"]
    assert localize.resolve_roots(vault, [Path("sub/n.md")]) == [note]
    # Missing and outside-vault paths are explicit errors, never silent.
    with pytest.raises(NotesError, match="does not exist"):
        localize.resolve_roots(vault, [Path("missing.md")])
    outside = tmp_path / "elsewhere.md"
    outside.write_text("![](https://x.test/1.png)\n")
    with pytest.raises(NotesError, match="outside the notes vault"):
        localize.resolve_roots(vault, [outside])
