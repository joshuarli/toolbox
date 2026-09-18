import os
import sqlite3
from concurrent.futures import Future
from datetime import datetime
from json import loads
from pathlib import Path

import pytest

import tool.ig.site_builder as site_builder


def test_parse_caption_drops_metadata_only_caption():
    caption = site_builder.parse_caption("13 likes, 0 comments - author on October 23, 2025:")

    assert caption["likes"] == 13
    assert caption["author"] == "author"
    assert caption["date"] == "October 23, 2025"
    assert caption["posted_at"] == datetime(2025, 10, 23)
    assert caption["text"] == ""


def test_parse_caption_extracts_quoted_text_with_trailing_period():
    caption = site_builder.parse_caption(
        '136 likes, 3 comments - silence_objects on April 22, 2026: "2 petites tabatières - art populaire". '
    )

    assert caption["likes"] == 136
    assert caption["author"] == "silence_objects"
    assert caption["date"] == "April 22, 2026"
    assert caption["posted_at"] == datetime(2026, 4, 22)
    assert caption["text"] == "2 petites tabatières - art populaire"


def test_load_posts_skips_posts_without_profile_membership(tmp_path, monkeypatch):
    db_path = tmp_path / "instagram.db"
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE post (
            shortid TEXT PRIMARY KEY,
            username TEXT,
            caption TEXT,
            post_type TEXT,
            scraped_at INTEGER
        )
        """
    )
    con.execute(
        """
        CREATE TABLE media (
            shortid TEXT,
            idx INTEGER,
            url TEXT
        )
        """
    )
    con.execute("CREATE TABLE profile_post (username TEXT, shortid TEXT, seen_at INTEGER, seq INTEGER)")
    con.execute(
        "INSERT INTO post (shortid, username, caption, post_type, scraped_at) VALUES (?, ?, ?, ?, ?)",
        ("GOOD1", "alice", '1 likes, 0 comments - alice on April 29, 2026: "hello"', "image", 1),
    )
    con.execute(
        "INSERT INTO post (shortid, username, caption, post_type, scraped_at) VALUES (?, ?, ?, ?, ?)",
        ("ORPHAN1", "alice", '1 likes, 0 comments - alice on April 29, 2026: "hello"', "image", 2),
    )
    con.execute(
        "INSERT INTO media (shortid, idx, url) VALUES (?, ?, ?)",
        ("GOOD1", 0, "http://cdn/good.jpg"),
    )
    con.execute(
        "INSERT INTO media (shortid, idx, url) VALUES (?, ?, ?)",
        ("ORPHAN1", 0, "http://cdn/orphan.jpg"),
    )
    con.execute("INSERT INTO profile_post VALUES ('alice','GOOD1',1,0)")
    con.commit()
    con.close()

    by_user = site_builder.load_posts(db_path=str(db_path))

    assert list(by_user) == ["alice"]
    assert [post["shortid"] for post in by_user["alice"]] == ["GOOD1"]
    assert by_user["alice"][0]["author"] == "alice"
    assert by_user["alice"][0]["images"] == ["images/by-profile/alice/GOOD1.jpg"]
    assert by_user["alice"][0]["image_sources"] == ["site/images/by-profile/alice/GOOD1.jpg"]


def test_load_posts_sorts_by_original_post_date_when_caption_has_no_date(tmp_path, monkeypatch):
    db_path = tmp_path / "instagram.db"
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE post (
            shortid TEXT PRIMARY KEY,
            username TEXT,
            caption TEXT,
            post_type TEXT,
            scraped_at INTEGER
        )
        """
    )
    con.execute(
        """
        CREATE TABLE media (
            shortid TEXT,
            idx INTEGER,
            url TEXT
        )
        """
    )
    con.execute("CREATE TABLE profile_post (username TEXT, shortid TEXT, seen_at INTEGER, seq INTEGER)")
    for shortid, scraped_at in (("DYi15_VE87s", 20), ("DYj_j5ZE65q", 10)):
        con.execute(
            "INSERT INTO post (shortid, username, caption, post_type, scraped_at) VALUES (?, ?, ?, ?, ?)",
            (shortid, "alice", "plain caption", "image", scraped_at),
        )
        con.execute(
            "INSERT INTO media (shortid, idx, url) VALUES (?, ?, ?)",
            (shortid, 0, f"http://cdn/{shortid}.jpg"),
        )
        con.execute(
            "INSERT INTO profile_post VALUES (?, ?, ?, ?)",
            ("alice", shortid, scraped_at, scraped_at),
        )
    con.commit()
    con.close()

    by_user = site_builder.load_posts(db_path=str(db_path))

    assert [post["shortid"] for post in by_user["alice"]] == ["DYj_j5ZE65q", "DYi15_VE87s"]


def test_load_posts_keeps_saved_archive_separate(tmp_path, monkeypatch):
    db_path = tmp_path / "instagram.db"
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE post (
            shortid TEXT PRIMARY KEY,
            username TEXT,
            caption TEXT,
            post_type TEXT,
            scraped_at INTEGER
        )
        """
    )
    con.execute("CREATE TABLE media (shortid TEXT, idx INTEGER, url TEXT)")
    con.execute("CREATE TABLE profile_post (username TEXT, saved INTEGER, shortid TEXT, seen_at INTEGER, seq INTEGER)")
    con.execute(
        "INSERT INTO post VALUES (?, ?, ?, ?, ?)",
        ("SAME1", "owner", '1 likes, 0 comments - owner on April 29, 2026: "hello"', "image", 1),
    )
    con.execute("INSERT INTO media VALUES (?, ?, ?)", ("SAME1", 0, "http://cdn/same.jpg"))
    con.execute("INSERT INTO profile_post VALUES (?, ?, ?, ?, ?)", ("alice", 0, "SAME1", 1, 0))
    con.execute("INSERT INTO profile_post VALUES (?, ?, ?, ?, ?)", ("alice", 1, "SAME1", 2, 0))
    con.commit()
    con.close()

    by_user = site_builder.load_posts(db_path=str(db_path))

    assert sorted(by_user) == ["alice", "alice-saved"]
    assert [post["shortid"] for post in by_user["alice"]] == ["SAME1"]
    assert [post["shortid"] for post in by_user["alice-saved"]] == ["SAME1"]
    assert by_user["alice-saved"][0]["archive_label"] == "@alice saved"


def test_load_posts_infers_mixed_carousel_extensions_from_urls(tmp_path, monkeypatch):
    db_path = tmp_path / "instagram.db"
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE post (
            shortid TEXT PRIMARY KEY,
            username TEXT,
            caption TEXT,
            post_type TEXT,
            scraped_at INTEGER
        )
        """
    )
    con.execute("CREATE TABLE media (shortid TEXT, idx INTEGER, url TEXT)")
    con.execute("CREATE TABLE profile_post (username TEXT, shortid TEXT, seen_at INTEGER, seq INTEGER)")
    con.execute(
        "INSERT INTO post VALUES (?, ?, ?, ?, ?)",
        ("MIX1", "owner", '1 likes, 0 comments - owner on April 29, 2026: "hello"', "carousel", 1),
    )
    con.execute("INSERT INTO media VALUES (?, ?, ?)", ("MIX1", 0, "https://cdn/video.mp4"))
    con.execute("INSERT INTO media VALUES (?, ?, ?)", ("MIX1", 1, "https://cdn/image.jpg"))
    con.execute("INSERT INTO profile_post VALUES (?, ?, ?, ?)", ("alice", "MIX1", 1, 0))
    con.commit()
    con.close()

    by_user = site_builder.load_posts(db_path=str(db_path))
    post = by_user["alice"][0]
    data = loads(site_builder.build_profile_data([post]))

    assert post["images"] == ["images/by-profile/alice/MIX1_1.mp4", "images/by-profile/alice/MIX1_2.jpg"]
    assert data["posts"][0]["media"] == [
        {"type": "video", "src": "images/by-profile/alice/MIX1_1.mp4"},
        {"type": "image", "src": "images/by-profile/alice/MIX1_2.jpg"},
    ]


def test_load_posts_uses_custom_media_dir(tmp_path):
    db_path = tmp_path / "instagram.db"
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE post (
            shortid TEXT PRIMARY KEY,
            username TEXT,
            caption TEXT,
            post_type TEXT,
            scraped_at INTEGER
        )
        """
    )
    con.execute("CREATE TABLE media (shortid TEXT, idx INTEGER, url TEXT)")
    con.execute("CREATE TABLE profile_post (username TEXT, shortid TEXT, seen_at INTEGER, seq INTEGER)")
    con.execute(
        "INSERT INTO post VALUES (?, ?, ?, ?, ?)",
        ("POST1", "alice", '1 likes, 0 comments - alice on April 29, 2026: "hello"', "image", 1),
    )
    con.execute("INSERT INTO media VALUES (?, ?, ?)", ("POST1", 0, "http://cdn/post.jpg"))
    con.execute("INSERT INTO profile_post VALUES (?, ?, ?, ?)", ("alice", "POST1", 1, 0))
    con.commit()
    con.close()

    custom_dir = tmp_path / "my-media"
    by_user = site_builder.load_posts(db_path=str(db_path), media_dir=custom_dir)

    assert by_user["alice"][0]["image_sources"] == [str(custom_dir / "by-profile" / "alice" / "POST1.jpg")]


def test_build_profile_data_prefers_caption_author_over_archive_username():
    data = loads(
        site_builder.build_profile_data(
            [
                {
                    "shortid": "DYkqJQyga98",
                    "author": "________emanon________",
                    "post_type": "carousel",
                    "caption": site_builder.parse_caption("149 likes, 0 comments - sungmin_eun on May 20, 2026"),
                    "images": ["images/98/DYkqJQyga98_1.jpg"],
                }
            ]
        )
    )

    assert data["posts"][0]["author"] == "sungmin_eun"


def test_build_profile_uses_lazy_modal_and_video_thumbnail():
    html = site_builder.build_profile(
        "alice",
        [
            {
                "shortid": "VID1",
                "post_type": "video",
                "caption": {"likes": 0, "date": "", "posted_at": None, "text": "hello"},
                "posted_at": datetime(2025, 9, 6),
                "scraped_at": 1,
                "images": ["images/d1/VID1.mp4"],
                "thumb": "thumbs/d1/VID1.webp",
            }
        ],
    )

    assert 'data-posts-src="data/alice.json"' in html
    assert '<img src="thumbs/d1/VID1.webp" alt="" loading="lazy">' in html
    assert 'src="assets/play.svg"' in html
    assert 'id="post-modal"' in html
    assert 'class="modal-author"' in html
    assert 'class="modal-post-link"' in html
    assert "images/d1/VID1.mp4" not in html
    assert "modal-VID1" not in html

    data = loads(
        site_builder.build_profile_data(
            [
                {
                    "shortid": "VID1",
                    "author": "alice",
                    "post_type": "video",
                    "caption": {"likes": 0, "date": "", "posted_at": None, "text": "hello"},
                    "posted_at": datetime(2025, 9, 6),
                    "scraped_at": 1,
                    "images": ["images/d1/VID1.mp4"],
                }
            ]
        )
    )
    assert data["posts"][0]["meta"] == "Sep 6, 2025"
    assert data["posts"][0]["author"] == "alice"
    assert data["posts"][0]["url"] == "https://www.instagram.com/p/VID1/"
    assert data["posts"][0]["media"] == [{"type": "video", "src": "images/d1/VID1.mp4"}]


def test_build_profile_uses_external_assets_without_inline_handlers():
    html = site_builder.build_profile(
        "alice",
        [
            {
                "shortid": "CAR1",
                "post_type": "carousel",
                "caption": {"likes": 0, "date": "", "posted_at": None, "text": "hello"},
                "scraped_at": 1,
                "images": ["images/r1/CAR1_1.jpg", "images/r1/CAR1_2.jpg"],
            }
        ],
    )

    assert '<link rel="stylesheet" href="assets/site.css">' in html
    assert '<script src="assets/site.js" defer></script>' in html
    assert 'src="assets/carousel.svg"' in html
    assert "<style" not in html
    assert "<script>" not in html
    assert "onclick=" not in html
    assert "<svg" not in html


def test_build_profile_links_grid_items_to_post_pages():
    html = site_builder.build_profile(
        "alice",
        [
            {
                "shortid": "POST1",
                "post_type": "image",
                "caption": {"likes": 0, "date": "", "posted_at": None, "text": "hello"},
                "scraped_at": 1,
                "images": ["images/t1/POST1.jpg"],
            }
        ],
    )

    assert 'href="p/POST1"' in html
    assert 'data-open-modal="POST1"' in html


def test_build_post_page_uses_parent_relative_assets_and_media():
    html = site_builder.build_post_page(
        {
            "shortid": "POST1",
            "archive_key": "alice",
            "archive_label": "@alice",
            "author": "alice",
            "post_type": "video",
            "caption": {"likes": 0, "date": "", "posted_at": None, "text": "hello"},
            "posted_at": datetime(2025, 9, 6),
            "scraped_at": 1,
            "images": ["images/t1/POST1.mp4"],
        }
    )

    assert '<link rel="stylesheet" href="../assets/site.css">' in html
    assert 'href="../alice.html"' in html
    assert '<video src="../images/t1/POST1.mp4"' in html


def test_profile_keyboard_can_advance_between_posts():
    js = (site_builder.WEB / "assets" / "site.js").read_text(encoding="utf-8")

    assert "function ensurePosts" in js
    assert "function playActiveVideo" in js
    assert "function lastSlideIndex" in js
    assert "function syncBackgroundToPost" in js
    assert 'author.textContent = post.author ? "@" + post.author : "";' in js
    assert 'link.href = post.url || "#";' in js
    assert 'gridItem.scrollIntoView({ block: "center", inline: "nearest" });' in js
    assert "openModal(post.shortid, dir < 0 ? lastSlideIndex(post) : 0);" in js
    assert "event.preventDefault();" in js
    assert 'if (event.key === "ArrowLeft" && !moveSlide(-1)) movePost(-1);' in js
    assert 'if (event.key === "ArrowRight" && !moveSlide(1)) movePost(1);' in js


def test_sync_media_uses_site_images_without_copying(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "site"

    media = out / "images" / "by-profile" / "alice" / "POST1.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"image")

    stale = out / "images" / "T1" / "POST1.jpg"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"stale")

    by_user = {
        "alice": [
            {
                "shortid": "POST1",
                "images": ["images/by-profile/alice/POST1.jpg"],
            }
        ]
    }

    # First call migrates and removes legacy stale
    assert site_builder.sync_media(by_user, out=out) == (0, 1, 0)
    assert media.read_bytes() == b"image"
    assert not stale.exists()
    assert site_builder.sync_media(by_user, out=out) == (0, 0, 0)


def test_sync_media_copies_from_custom_media_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "site"

    src = tmp_path / "archive" / "by-profile" / "alice" / "POST1.jpg"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"image")

    by_user = {
        "alice": [
            {
                "shortid": "POST1",
                "images": ["images/by-profile/alice/POST1.jpg"],
                "image_sources": [str(src)],
            }
        ]
    }

    assert site_builder.sync_media(by_user, out=out) == (1, 0, 0)
    assert (out / "images" / "by-profile" / "alice" / "POST1.jpg").read_bytes() == b"image"


def test_sync_media_does_not_purge_legacy_tree_when_a_source_is_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "site"

    legacy = out / "images" / "t1" / "POST1.jpg"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy")
    stale = out / "images" / "or" / "ORPHAN.jpg"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"orphan")

    by_user = {
        "alice": [
            {"shortid": "POST1", "images": ["images/by-profile/alice/POST1.jpg"]},
            {"shortid": "MISSING", "images": ["images/by-profile/alice/MISSING.jpg"]},
        ]
    }

    copied, removed, missing = site_builder.sync_media(by_user, out=out)

    assert copied == 1
    assert removed == 0
    assert missing == 1
    assert (out / "images" / "by-profile" / "alice" / "POST1.jpg").read_bytes() == b"legacy"
    assert legacy.exists()
    assert stale.exists()


def test_sync_media_with_empty_database_leaves_existing_archive_untouched(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "site"
    existing = out / "images" / "ab" / "POST1.jpg"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"image")

    assert site_builder.sync_media({}, out=out) == (0, 0, 0)
    assert existing.read_bytes() == b"image"


def test_sync_media_cleanup_preserves_unrelated_image_entries(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "site"
    legacy = out / "images" / "t1" / "POST1.jpg"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy")
    keep = out / "images" / "keep.txt"
    keep.write_text("keep", encoding="utf-8")

    by_user = {"alice": [{"shortid": "POST1", "images": ["images/by-profile/alice/POST1.jpg"]}]}

    assert site_builder.sync_media(by_user, out=out) == (1, 1, 0)
    assert keep.read_text(encoding="utf-8") == "keep"


def test_sync_thumbnails_generates_referenced_thumbs_and_removes_stale(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(site_builder, "ffmpeg_supports_encoder", lambda _encoder: True)
    out = tmp_path / "site"

    src = out / "images" / "T1" / "POST1.jpg"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"image")

    stale = out / "thumbs" / "LE" / "STALE.webp"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"stale")

    def fake_generate(_src, dst, _backend=None):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"thumb")
        return True

    monkeypatch.setattr(site_builder, "generate_thumbnail", fake_generate)

    by_user = {
        "alice": [
            {
                "shortid": "POST1",
                "images": ["images/t1/POST1.jpg"],
            }
        ]
    }

    assert site_builder.sync_thumbnails(by_user, out=out) == (1, 1, 0, 0)
    assert by_user["alice"][0]["thumb"] == "thumbs/t1/POST1.webp"
    assert (out / "thumbs" / "T1" / "POST1.webp").read_bytes() == b"thumb"
    assert not stale.exists()


def test_sync_thumbnails_skips_existing_regardless_of_mtime(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(site_builder, "ffmpeg_supports_encoder", lambda _encoder: True)
    out = tmp_path / "site"

    src = out / "images" / "T1" / "POST1.jpg"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"image")

    dst = out / "thumbs" / "T1" / "POST1.webp"
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"thumb")

    src.touch()
    dst.touch()
    src_time = 2_000_000_000
    dst_time = 1_000_000_000
    os.utime(src, ns=(src_time, src_time))
    os.utime(dst, ns=(dst_time, dst_time))

    called = False

    def fake_generate(_src, _dst, _backend=None):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(site_builder, "generate_thumbnail", fake_generate)

    by_user = {"alice": [{"shortid": "POST1", "images": ["images/t1/POST1.jpg"]}]}

    assert site_builder.sync_thumbnails(by_user, out=out) == (0, 0, 0, 0)
    assert not called, "generate_thumbnail should not be called for an existing thumb"
    assert dst.stat().st_mtime_ns == dst_time


def test_sync_thumbnails_uses_executor_for_multiple_workers(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(site_builder, "ffmpeg_supports_encoder", lambda _encoder: True)
    out = tmp_path / "site"
    created_workers = []

    for name in ("POST1.jpg", "POST2.jpg"):
        stem = Path(name).stem
        src = out / "images" / stem[-2:] / name
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"image")

    def fake_generate(_src, dst, _backend=None):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"thumb")
        return True

    class FakeExecutor:
        def __init__(self, max_workers):
            created_workers.append(max_workers)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def submit(self, fn, task):
            future = Future()
            future.set_result(fn(task))
            return future

    monkeypatch.setattr(site_builder, "generate_thumbnail", fake_generate)
    monkeypatch.setattr(site_builder, "ThreadPoolExecutor", FakeExecutor)

    by_user = {
        "alice": [
            {"shortid": "POST1", "images": ["images/t1/POST1.jpg"]},
            {"shortid": "POST2", "images": ["images/t2/POST2.jpg"]},
        ]
    }

    assert site_builder.sync_thumbnails(by_user, out=out, progress=True, workers=2) == (2, 0, 0, 0)
    assert created_workers == [2]
    output = capsys.readouterr().out
    assert "  using 2 thumbnail workers\n" in output
    assert "  thumbnails: 0/2" in output
    assert "  thumbnails: 1/2" in output
    assert "  thumbnails: 2/2\n" in output


def test_sync_thumbnails_fails_before_generating_without_webp_converter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(site_builder, "_FFMPEG", "ffmpeg")
    monkeypatch.setattr(site_builder, "_CWEBP", None)
    monkeypatch.setattr(site_builder, "ffmpeg_supports_encoder", lambda encoder: encoder == "mjpeg")
    monkeypatch.setattr(site_builder, "sips_supports_webp_write", lambda: False)
    out = tmp_path / "site"

    src = out / "images" / "T1" / "POST1.jpg"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"image")

    generated = False

    def fake_generate(_src, _dst, _backend=None):
        nonlocal generated
        generated = True
        return True

    monkeypatch.setattr(site_builder, "generate_thumbnail", fake_generate)

    by_user = {"alice": [{"shortid": "POST1", "images": ["images/t1/POST1.jpg"]}]}

    with pytest.raises(site_builder.ThumbnailGenerationError, match="no WebP converter"):
        site_builder.sync_thumbnails(by_user, out=out)

    assert not generated


def test_sync_thumbnails_uses_cwebp_fallback_backend(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(site_builder, "_FFMPEG", "ffmpeg")
    monkeypatch.setattr(site_builder, "_CWEBP", "cwebp")
    monkeypatch.setattr(site_builder, "ffmpeg_supports_encoder", lambda encoder: encoder == "mjpeg")
    monkeypatch.setattr(site_builder, "sips_supports_webp_write", lambda: False)
    out = tmp_path / "site"

    src = out / "images" / "T1" / "POST1.jpg"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"image")

    backends = []

    def fake_generate(_src, dst, backend=None):
        backends.append(backend.method)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"thumb")
        return True

    monkeypatch.setattr(site_builder, "generate_thumbnail", fake_generate)

    by_user = {"alice": [{"shortid": "POST1", "images": ["images/t1/POST1.jpg"]}]}

    assert site_builder.sync_thumbnails(by_user, out=out) == (1, 0, 0, 0)
    assert backends == ["ffmpeg-jpg-cwebp"]


def test_sync_post_pages_writes_extensionless_paths_and_removes_stale(tmp_path):
    out = tmp_path / "site"
    stale = out / "p" / "STALE"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale", encoding="utf-8")

    by_user = {
        "alice": [
            {
                "shortid": "POST1",
                "archive_key": "alice",
                "archive_label": "@alice",
                "author": "alice",
                "post_type": "image",
                "caption": {"likes": 0, "date": "", "posted_at": None, "text": "hello"},
                "posted_at": datetime(2025, 9, 6),
                "scraped_at": 1,
                "images": ["images/POST1.jpg"],
            }
        ]
    }

    assert site_builder.sync_post_pages(by_user, out=out) == (1, 1)
    assert (out / "p" / "POST1").exists()
    assert not stale.exists()
