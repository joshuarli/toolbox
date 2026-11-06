import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

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


class _Handler(BaseHTTPRequestHandler):
    seen: ClassVar[list[dict]] = []
    statuses: ClassVar[list[int]] = []

    def _respond(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self.seen.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body})
        status = self.statuses.pop(0) if self.statuses else 200
        payload = json.dumps({"path": self.path}).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _respond  # noqa: N815

    def log_message(self, *args):
        pass


@pytest.fixture
def server() -> Iterator[str]:
    _Handler.seen = []
    _Handler.statuses = []
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


class TestSessionInit:
    def test_default_user_agent(self):
        assert "User-Agent" in Session().headers

    def test_custom_user_agent(self):
        assert Session(user_agent="test/1.0").headers["User-Agent"] == "test/1.0"

    def test_rate_limiter_optional(self):
        assert Session().rate_limiter is None

    def test_with_rate_limiter(self):
        rl = RateLimiter()
        assert Session(rate_limiter=rl).rate_limiter is rl


class TestSessionRequests:
    def test_get_sends_user_agent_and_params(self, server):
        s = Session(user_agent="test/1.0")
        data = s.get(f"{server}/x", params={"q": "hello", "page": 1}, headers={"X-Custom": "val"})
        [seen] = _Handler.seen
        assert json.loads(data) == {"path": "/x?q=hello&page=1"}
        assert seen["headers"]["User-Agent"] == "test/1.0"
        assert seen["headers"]["X-Custom"] == "val"

    def test_get_rate_limits(self, server):
        rl = MagicMock()
        Session(rate_limiter=rl).get(server)
        rl.acquire.assert_called_once()

    def test_get_json_and_text(self, server):
        s = Session()
        assert s.get_json(f"{server}/a") == {"path": "/a"}
        assert s.get_text(f"{server}/b") == '{"path": "/b"}'

    def test_error_status_returns_body(self, server):
        _Handler.statuses = [404]
        status, data = Session().request("GET", f"{server}/missing")
        assert status == 404
        assert json.loads(data) == {"path": "/missing"}

    def test_post_with_json_body_and_params(self, server):
        Session().post_json(f"{server}/p", params={"key": "val"}, json_body={"a": 1})
        [seen] = _Handler.seen
        assert seen["method"] == "POST"
        assert seen["path"] == "/p?key=val"
        assert seen["headers"]["Content-Type"] == "application/json"
        assert json.loads(seen["body"]) == {"a": 1}

    def test_post_without_body(self, server):
        Session().post(server)
        assert _Handler.seen[0]["body"] == b""

    def test_retries_retryable_statuses_then_returns_the_last(self, server, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda _seconds: None)
        _Handler.statuses = [503, 503, 200]
        status, _data = Session().request("GET", server, retries=2)
        assert status == 200
        assert len(_Handler.seen) == 3
        _Handler.statuses = [503, 503]
        status, _data = Session().request("GET", server, retries=1)
        assert status == 503

    def test_max_bytes_reads_one_byte_past_the_limit(self, server):
        _status, data = Session().request("GET", server, max_bytes=5)
        assert len(data) == 6

    def test_connection_failure_raises_after_retries(self, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda _seconds: None)
        with patch.object(Session, "_wait") as wait, pytest.raises(OSError, match=r"Errno|refused"):
            Session().request("GET", "http://127.0.0.1:1/", retries=1)
        assert wait.call_count == 2
