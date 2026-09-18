import json
import time
from unittest.mock import MagicMock, patch

from tool.music.http import RateLimiter, Session


class TestRateLimiter:
    def test_acquire_no_wait(self):
        rl = RateLimiter(rate=100.0)
        rl.acquire()

    def test_acquire_blocks(self, monkeypatch):
        rl = RateLimiter(rate=1.0)
        rl.acquire()
        sleeps = []
        monkeypatch.setattr(time, "sleep", sleeps.append)
        rl.acquire()
        assert len(sleeps) > 0

    def test_rate_limiter_refills(self):
        # High rate refills tokens almost instantly — two rapid acquires
        # should not need to sleep.
        rl = RateLimiter(rate=1e6)
        rl.acquire()
        # Second acquire with a very high rate should refill immediately
        sleeps = []
        with patch("tool.music.http.time.sleep", side_effect=sleeps.append):
            rl.acquire()
        assert len(sleeps) == 0


class TestSessionInit:
    def test_default_user_agent(self):
        s = Session()
        assert "User-Agent" in s.headers

    def test_custom_user_agent(self):
        s = Session(user_agent="test/1.0")
        assert s.headers["User-Agent"] == "test/1.0"

    def test_rate_limiter_optional(self):
        s = Session()
        assert s.rate_limiter is None

    def test_with_rate_limiter(self):
        rl = RateLimiter()
        s = Session(rate_limiter=rl)
        assert s.rate_limiter is rl


class TestSessionGet:
    def test_get_basic(self):
        mock_resp = MagicMock()
        mock_resp.data = b"response body"
        s = Session(user_agent="test/1.0")
        s.pool = MagicMock()
        s.pool.request.return_value = mock_resp

        result = s.get("http://example.com")
        assert result == b"response body"
        args, kwargs = s.pool.request.call_args
        assert args[0] == "GET"
        assert args[1] == "http://example.com"
        assert kwargs["headers"] == s.headers

    def test_get_with_params(self):
        mock_resp = MagicMock()
        mock_resp.data = b"ok"
        s = Session(user_agent="test/1.0")
        s.pool = MagicMock()
        s.pool.request.return_value = mock_resp

        result = s.get("http://example.com", params={"q": "hello", "page": "1"})
        assert result == b"ok"
        args, _kwargs = s.pool.request.call_args
        url = args[1]
        assert "q=hello" in url
        assert "page=1" in url

    def test_get_with_extra_headers(self):
        mock_resp = MagicMock()
        mock_resp.data = b"ok"
        s = Session(user_agent="test/1.0")
        s.pool = MagicMock()
        s.pool.request.return_value = mock_resp

        s.get("http://example.com", headers={"X-Custom": "val"})
        _args, kwargs = s.pool.request.call_args
        merged = kwargs["headers"]
        assert merged["X-Custom"] == "val"
        assert merged["User-Agent"] == "test/1.0"

    def test_get_rate_limits(self):
        mock_resp = MagicMock()
        mock_resp.data = b"ok"
        rl = MagicMock()
        s = Session(rate_limiter=rl)
        s.pool = MagicMock()
        s.pool.request.return_value = mock_resp

        s.get("http://example.com")
        rl.acquire.assert_called_once()


class TestSessionGetJson:
    def test_get_json(self):
        mock_resp = MagicMock()
        mock_resp.data = json.dumps({"key": "value"}).encode()
        s = Session()
        s.pool = MagicMock()
        s.pool.request.return_value = mock_resp

        result = s.get_json("http://example.com")
        assert result == {"key": "value"}

    def test_get_json_list(self):
        mock_resp = MagicMock()
        mock_resp.data = json.dumps([1, 2, 3]).encode()
        s = Session()
        s.pool = MagicMock()
        s.pool.request.return_value = mock_resp

        result = s.get_json("http://example.com")
        assert result == [1, 2, 3]


class TestSessionGetText:
    def test_get_text(self):
        mock_resp = MagicMock()
        mock_resp.data = b"plain text"
        s = Session()
        s.pool = MagicMock()
        s.pool.request.return_value = mock_resp

        result = s.get_text("http://example.com")
        assert result == "plain text"


class TestSessionPost:
    def test_post_no_body(self):
        mock_resp = MagicMock()
        mock_resp.data = b"ok"
        s = Session()
        s.pool = MagicMock()
        s.pool.request.return_value = mock_resp

        result = s.post("http://example.com")
        assert result == b"ok"
        _args, kwargs = s.pool.request.call_args
        assert kwargs["body"] is None

    def test_post_with_json_body(self):
        mock_resp = MagicMock()
        mock_resp.data = b"ok"
        s = Session()
        s.pool = MagicMock()
        s.pool.request.return_value = mock_resp

        s.post("http://example.com", json_body={"a": 1})
        _args, kwargs = s.pool.request.call_args
        assert kwargs["headers"]["Content-Type"] == "application/json"
        assert b'"a": 1' in kwargs["body"]

    def test_post_with_params(self):
        mock_resp = MagicMock()
        mock_resp.data = b"ok"
        s = Session()
        s.pool = MagicMock()
        s.pool.request.return_value = mock_resp

        s.post("http://example.com", params={"key": "val"})
        args, _kwargs = s.pool.request.call_args
        url = args[1]
        assert "key=val" in url


class TestSessionPostJson:
    def test_post_json_dict(self):
        mock_resp = MagicMock()
        mock_resp.data = json.dumps({"result": "ok"}).encode()
        s = Session()
        s.pool = MagicMock()
        s.pool.request.return_value = mock_resp

        result = s.post_json("http://example.com", json_body={"x": 1})
        assert result == {"result": "ok"}

    def test_post_json_list(self):
        mock_resp = MagicMock()
        mock_resp.data = json.dumps([1, 2]).encode()
        s = Session()
        s.pool = MagicMock()
        s.pool.request.return_value = mock_resp

        result = s.post_json("http://example.com")
        assert result == [1, 2]
