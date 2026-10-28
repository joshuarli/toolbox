import hashlib

from tool.movies.fetch_images import download_and_convert


def test_existing_file_skips_download(tmp_path):
    url = "https://example.com/poster.jpg"
    name = f"{hashlib.sha256(url.encode()).hexdigest()[:24]}.webp"
    (tmp_path / name).write_bytes(b"fake-webp")
    assert download_and_convert(url, tmp_path) == name
