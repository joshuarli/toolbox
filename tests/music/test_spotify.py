import json
from unittest.mock import MagicMock, patch

from tool.music.api.spotify import (
    _FALLBACK_SECRET,
    SpotifyPublicPlaylist,
    _generate_totp,
    _read_cached_secret,
    _write_cached_secret,
)


class TestGenerateTotp:
    def test_generates_six_digit_code(self):
        version, secret = _FALLBACK_SECRET
        code = _generate_totp(version, secret)
        assert len(code) == 6
        assert code.isdigit()

    def test_deterministic_with_fixed_time(self):
        import time

        with patch.object(time, "time", return_value=1234567890.0):
            version, secret = _FALLBACK_SECRET
            code1 = _generate_totp(version, secret)
            code2 = _generate_totp(version, secret)
            assert code1 == code2


class TestCachedSecret:
    def test_read_nonexistent(self, tmp_path):
        nonexistent = str(tmp_path / "nonexistent" / "secret.json")
        with patch("tool.music.api.spotify._CACHE_PATH", nonexistent):
            assert _read_cached_secret() is None

    def test_read_corrupt(self, tmp_path):
        corrupt = tmp_path / "secret.json"
        corrupt.write_text("not valid json")
        with patch("tool.music.api.spotify._CACHE_PATH", str(corrupt)):
            assert _read_cached_secret() is None

    def test_read_valid(self, tmp_path):
        cache_file = tmp_path / "secret.json"
        cache_file.write_text(json.dumps({"61": [1, 2, 3, 4]}))
        with patch("tool.music.api.spotify._CACHE_PATH", str(cache_file)):
            result = _read_cached_secret()
            assert result is not None
            assert result[0] == 61
            assert list(result[1]) == [1, 2, 3, 4]

    def test_read_version_zero(self, tmp_path):
        cache_file = tmp_path / "secret.json"
        cache_file.write_text(json.dumps({"0": []}))
        with patch("tool.music.api.spotify._CACHE_PATH", str(cache_file)):
            assert _read_cached_secret() is None

    def test_write_creates_dir(self, tmp_path):
        cache_dir = str(tmp_path / "subdir")
        cache_file = tmp_path / "subdir" / "secret.json"
        with patch("tool.music.api.spotify._CACHE_DIR", cache_dir), patch("tool.music.api.spotify._CACHE_PATH", cache_file):
            _write_cached_secret({"61": [10, 20]})
            assert cache_file.exists()
            data = json.loads(cache_file.read_text())
            assert data == {"61": [10, 20]}


class TestSpotifyPublicPlaylist:
    def test_init_default_session(self):
        sp = SpotifyPublicPlaylist()
        assert sp._access_token is None
        assert sp._client_token is None

    def test_init_custom_session(self):
        from tool.music.http import Session

        s = Session()
        sp = SpotifyPublicPlaylist(session=s)
        assert sp._session is s

    def test_api_headers_when_authenticated(self):
        sp = SpotifyPublicPlaylist()
        sp._access_token = "tok"
        sp._client_token = "ctok"
        sp._client_version = "1.0"
        sp._sha_hash = "abc"

        headers = sp._api_headers
        # When all tokens are set, _authenticate() returns early — no network calls
        assert headers["Authorization"] == "Bearer tok"
        assert headers["Client-Token"] == "ctok"
        assert headers["Spotify-App-Version"] == "1.0"

    def test_api_headers_calls_authenticate(self):
        sp = SpotifyPublicPlaylist()
        sp._authenticate = MagicMock()
        sp._client_token = "ctok"
        sp._client_version = "1.0"

        _ = sp._api_headers
        sp._authenticate.assert_called_once()

    def test_strips_playlist_url(self):
        sp = SpotifyPublicPlaylist()
        sp._authenticate = MagicMock()
        sp._graphql = MagicMock(
            return_value={
                "data": {
                    "playlistV2": {
                        "content": {"items": [], "totalCount": 0},
                        "ownerV2": {"data": {"name": "", "uri": ""}},
                        "images": {"items": []},
                        "name": "",
                        "description": "",
                    }
                }
            }
        )

        metadata, _tracks = sp.get_playlist("https://open.spotify.com/playlist/abc123?si=xxx")
        assert metadata["playlist_id"] == "abc123"
