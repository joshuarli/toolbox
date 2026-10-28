import hashlib

from tool.movies.fetch_images import _pending_work, download_and_convert


def test_existing_file_skips_download(tmp_path):
    url = "https://example.com/poster.jpg"
    name = f"{hashlib.sha256(url.encode()).hexdigest()[:24]}.webp"
    (tmp_path / name).write_bytes(b"fake-webp")
    assert download_and_convert(url, tmp_path) == name


def test_pending_work_skips_present_files(tmp_path):
    (tmp_path / "kept.webp").write_bytes(b"fake-webp")
    rows = [
        ("film-a", "http://example.com/a-backdrop.jpg", "http://example.com/a-poster.jpg", "kept.webp", None),
        ("film-b", "http://example.com/empty-poster.jpg", None, None, None),
        ("film-c", None, None, None, None),
        ("film-d", None, "http://example.com/d-poster.jpg", None, "gone.webp"),
        ("film-e", None, "http://example.com/e-poster.jpg", None, ""),
    ]
    assert _pending_work(rows, tmp_path) == [
        ("film-a", "poster_local", "http://example.com/a-poster.jpg"),
        ("film-d", "poster_local", "http://example.com/d-poster.jpg"),
        ("film-e", "poster_local", "http://example.com/e-poster.jpg"),
    ]
