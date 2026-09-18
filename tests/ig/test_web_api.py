"""Unit tests for tool.ig.web_api — authenticated web v1 API primitives.

All tests are fully offline: no browser, no network.
"""

import json
import urllib.error
from unittest.mock import patch

import pytest

from tool.ig.errors import InstagramHttpError
from tool.ig.web_api import (
    WebApiError,
    auth_headers,
    profile_id,
    request_json,
)


class _Response:
    def __init__(self, data: dict) -> None:
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self) -> bytes:
        return json.dumps(self.data).encode("utf-8")


def _cookies() -> dict[str, str]:
    return {"sessionid": "s", "csrftoken": "c", "ds_user_id": "1"}


def test_auth_headers_include_web_app_fingerprint():
    headers = auth_headers(_cookies(), "https://www.instagram.com/source/")

    assert "sessionid=s" in headers["Cookie"]
    assert headers["X-IG-App-ID"]
    assert headers["X-ASBD-ID"] == "359341"
    assert headers["X-CSRFToken"] == "c"
    assert headers["Referer"] == "https://www.instagram.com/source/"
    assert headers["X-Requested-With"] == "XMLHttpRequest"


def test_auth_headers_use_chrome_user_agent():
    headers = auth_headers(_cookies(), "https://www.instagram.com/source/")
    assert "Chrome/" in headers["User-Agent"]


def test_auth_headers_raise_missing_cookies():
    with pytest.raises(WebApiError, match="authenticated browser cookies"):
        auth_headers({"csrftoken": "c"}, "https://www.instagram.com/source/")


def test_request_json_retries_transient_http_errors(monkeypatch):
    response = _Response({"data": {"ok": True}})
    err = urllib.error.HTTPError("https://x", 503, "unavailable", {}, None)
    calls = []

    def fake_urlopen(req, **kwargs):
        calls.append(req)
        if len(calls) == 1:
            raise err
        return response

    monkeypatch.setattr("tool.ig.http_retry.time.sleep", lambda _delay: None)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        data = request_json(
            "https://www.instagram.com/api/v1/example/",
            _cookies(),
            "https://www.instagram.com/source/",
            "example label",
        )

    assert data == {"data": {"ok": True}}
    assert len(calls) == 2


def test_request_json_does_not_retry_rate_limits(monkeypatch):
    err = urllib.error.HTTPError("https://x", 429, "too many", {}, None)
    calls = []

    def fake_urlopen(req, **kwargs):
        calls.append(req)
        raise err

    monkeypatch.setattr("tool.ig.http_retry.time.sleep", lambda _delay: None)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen), pytest.raises(WebApiError) as exc_info:
        request_json(
            "https://www.instagram.com/api/v1/example/",
            _cookies(),
            "https://www.instagram.com/source/",
            "example label",
        )

    assert exc_info.value.status == 429
    assert len(calls) == 1


def test_request_json_surfaces_errors_with_error_cls(monkeypatch):
    err = urllib.error.HTTPError("https://x", 429, "too many", {}, None)

    def boom(req, **kwargs):
        raise err

    class DomainError(InstagramHttpError):
        pass

    monkeypatch.setattr("tool.ig.http_retry.time.sleep", lambda _delay: None)

    with patch("urllib.request.urlopen", side_effect=boom), pytest.raises(DomainError) as exc_info:
        request_json(
            "https://www.instagram.com/api/v1/example/",
            _cookies(),
            "https://www.instagram.com/source/",
            "example label",
            error_cls=DomainError,
        )

    assert exc_info.value.status == 429


def test_request_json_require_auth_fails_with_error_cls():
    class DomainError(InstagramHttpError):
        pass

    with pytest.raises(DomainError, match="authenticated browser cookies"):
        request_json(
            "https://www.instagram.com/api/v1/example/",
            {"csrftoken": "c"},
            "https://www.instagram.com/source/",
            "example label",
            error_cls=DomainError,
        )


def test_profile_id_uses_web_profile_info_endpoint():
    captured = []

    def fake_urlopen(req, **kwargs):
        captured.append(req)
        return _Response({"data": {"user": {"id": "123"}}})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        user_id = profile_id("source", _cookies())

    assert user_id == "123"
    assert captured[0].full_url == "https://www.instagram.com/api/v1/users/web_profile_info/?username=source"


def test_profile_id_falls_back_to_username_feed_after_rate_limit(monkeypatch):
    calls: list[str] = []

    def fake_request_json(url, *_args, **_kwargs):
        calls.append(url)
        if "web_profile_info" in url:
            raise WebApiError("profile info HTTP 429", status=429, retry_after=60, url=url)
        return {"user": {"pk": "123"}, "items": []}

    monkeypatch.setattr("tool.ig.web_api.request_json", fake_request_json)

    assert profile_id("source", _cookies()) == "123"
    assert calls == [
        "https://www.instagram.com/api/v1/users/web_profile_info/?username=source",
        "https://www.instagram.com/api/v1/feed/user/source/username/?count=1",
    ]


def test_profile_id_raises_when_user_missing(monkeypatch):
    def fake_urlopen(req, **kwargs):
        return _Response({"status": "ok"})

    with (
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
        pytest.raises(WebApiError, match="did not include @source"),
    ):
        profile_id("source", _cookies())


def test_missing_cookies_fail_before_request():
    with pytest.raises(WebApiError, match="authenticated browser cookies"):
        profile_id("source", {"csrftoken": "c"})
