import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from tool.ig.db import init_db
from tool.ig.following import (
    Followee,
    FollowingFetchError,
    _following_url,
    _parse_following_page,
    fetch_following,
    get_following,
    save_following,
    stored_following_usernames,
)


class _Response:
    def __init__(self, data: dict, *, url: str = "https://www.instagram.com/graphql/query/") -> None:
        self.data = data
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self) -> bytes:
        return json.dumps(self.data).encode("utf-8")

    def geturl(self) -> str:
        return self.url


def _cookies() -> dict[str, str]:
    return {"sessionid": "s", "csrftoken": "c", "ds_user_id": "1"}


def test_init_db_creates_profile_following_table():
    db = init_db(":memory:")

    columns = {row[1] for row in db.execute("PRAGMA table_info(profile_following)")}

    assert "followee_username" in columns
    assert "current" in columns


def test_parse_following_page_reads_users_and_cursor():
    data = {
        "users": [
            {
                "username": "alice",
                "pk": "10",
                "pk_id": "10",
                "full_name": "Alice A",
                "is_private": True,
                "profile_pic_url": "https://cdn/alice.jpg",
            }
        ],
        "has_more": True,
        "next_max_id": "NEXT",
    }

    nodes, has_next, end_cursor = _parse_following_page(data)

    assert nodes == [Followee("alice", "10", "Alice A", True, "https://cdn/alice.jpg")]
    assert has_next is True
    assert end_cursor == "NEXT"


def test_fetch_following_paginates_with_max_id():
    pages = [
        {
            "users": [{"username": "a", "pk": "1"}],
            "has_more": True,
            "next_max_id": "NEXT",
        },
        {
            "users": [{"username": "b", "pk": "2"}],
            "has_more": False,
            "next_max_id": None,
        },
    ]
    requests = []

    def fake_urlopen(req, **kwargs):
        requests.append(req)
        return _Response(pages[len(requests) - 1])

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        followees = fetch_following("source", "123", _cookies())

    assert [f.username for f in followees] == ["a", "b"]
    assert urlparse(requests[0].full_url).path == "/api/v1/friendships/123/following/"
    assert parse_qs(urlparse(requests[0].full_url).query) == {"count": ["200"]}
    second_query = parse_qs(urlparse(requests[1].full_url).query)
    assert second_query == {"count": ["200"], "max_id": ["NEXT"]}


def test_fetch_following_rejects_repeated_cursor():
    page = {
        "users": [{"username": "a", "pk": "1"}],
        "has_more": True,
        "next_max_id": "SAME",
    }

    with (
        patch("urllib.request.urlopen", return_value=_Response(page)),
        pytest.raises(FollowingFetchError, match="repeated next_max_id"),
    ):
        fetch_following("source", "123", _cookies())


def test_request_construction_uses_auth_headers_and_v1_endpoint():
    captured = []

    def fake_urlopen(req, **kwargs):
        captured.append(req)
        return _Response({"users": [], "has_more": False})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        fetch_following("source", "123", _cookies())

    req = captured[0]
    assert urlparse(req.full_url).path == "/api/v1/friendships/123/following/"
    assert parse_qs(urlparse(req.full_url).query) == {"count": ["200"]}
    assert "sessionid=s" in req.get_header("Cookie")
    assert "csrftoken=c" in req.get_header("Cookie")
    assert req.get_header("X-csrftoken") == "c"
    assert req.get_header("X-ig-app-id")
    assert req.get_header("Referer") == "https://www.instagram.com/source/"


def test_missing_cookies_fail_before_request():
    with pytest.raises(FollowingFetchError, match="authenticated browser cookies"):
        fetch_following("source", "123", {"csrftoken": "c"})


def test_save_following_marks_stale_and_preserves_first_seen(monkeypatch):
    db = init_db(":memory:")
    times = iter([100, 200])
    monkeypatch.setattr("tool.ig.following.time.time", lambda: next(times))

    save_following(
        db,
        "source",
        [
            Followee("a", "1", "A", False, "pic-a"),
            Followee("b", "2", "B", True, "pic-b"),
        ],
    )
    result = save_following(
        db,
        "source",
        [
            Followee("b", "22", "Bee", False, "pic-b2"),
            Followee("c", "3", "C", False, "pic-c"),
        ],
    )

    rows = db.execute(
        """
        SELECT followee_username, followee_id, full_name, is_private, first_seen_at, last_seen_at, seq, current
        FROM profile_following
        WHERE username = 'source'
        ORDER BY followee_username
        """
    ).fetchall()
    assert rows == [
        ("a", "1", "A", 0, 100, 100, 0, 0),
        ("b", "22", "Bee", 0, 100, 200, 0, 1),
        ("c", "3", "C", 0, 200, 200, 1, 1),
    ]
    assert result.saved == 2
    assert result.new == 1
    assert result.stale == 1


def test_stored_following_usernames_defaults_to_current_only(monkeypatch):
    db = init_db(":memory:")
    times = iter([100, 200])
    monkeypatch.setattr("tool.ig.following.time.time", lambda: next(times))
    save_following(db, "source", [Followee("a", "1", "", False, ""), Followee("b", "2", "", False, "")])
    save_following(db, "source", [Followee("b", "2", "", False, ""), Followee("c", "3", "", False, "")])

    assert stored_following_usernames(db, "source") == ["b", "c"]
    assert stored_following_usernames(db, "source", current_only=False) == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_get_following_reads_cookies_and_saves(monkeypatch):
    db = init_db(":memory:")
    tab = MagicMock()
    tab.close = AsyncMock()
    tab.get_cookies = AsyncMock(
        return_value={
            "cookies": [
                {"domain": ".instagram.com", "name": "sessionid", "value": {"type": "string", "value": "s"}},
                {"domain": ".instagram.com", "name": "csrftoken", "value": {"type": "string", "value": "c"}},
            ]
        }
    )
    browser = MagicMock()
    browser.close = AsyncMock()

    @asynccontextmanager
    async def browser_session(**_):
        yield browser, tab

    monkeypatch.setattr("tool.ig.following.browser_session", browser_session)
    monkeypatch.setattr("tool.ig.following.profile_id", lambda username, cookies, error_cls=FollowingFetchError: "123")
    monkeypatch.setattr(
        "tool.ig.following.fetch_following", lambda username, user_id, cookies: [Followee("a", "1", "", False, "")]
    )

    result = await get_following("source", db)

    assert result.saved == 1
    assert db.execute("SELECT followee_username FROM profile_following").fetchone()[0] == "a"


def test_following_url_omits_max_id_on_first_page():
    url = _following_url("123")
    query = parse_qs(urlparse(url).query)

    assert urlparse(url).path == "/api/v1/friendships/123/following/"
    assert query == {"count": ["200"]}


def test_following_url_adds_max_id_on_later_pages():
    url = _following_url("123", max_id="CURSOR")
    query = parse_qs(urlparse(url).query)

    assert query == {"count": ["200"], "max_id": ["CURSOR"]}
