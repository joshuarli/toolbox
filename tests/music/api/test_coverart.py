"""Cover Art Archive fallback, validation and HTTP handling with mocked transport."""

from unittest.mock import MagicMock, patch

import pytest

from tool.music.api import coverart

_RELEASE = "490a58ac-6f2d-482e-8915-b703e229794b"
_GROUP = "4cda8183-371b-4189-bcb1-265bd0ff4b5f"
_JPEG = b"\xff\xd8\xffdata"
_PNG = b"\x89PNG\r\n\x1a\ndata"


def test_exact_release_preferred():
    with patch.object(coverart, "_get", return_value=_JPEG) as fetch:
        got = coverart.fetch_cover(_RELEASE, _GROUP)
    assert got.source == "release"
    assert got.mime == "image/jpeg"
    assert fetch.call_args.args[0].endswith(f"/release/{_RELEASE}/front-1200")
    fetch.assert_called_once()


def test_group_fallback_only_after_release_sizes():
    with patch.object(coverart, "_get", side_effect=[None, None, None, _PNG]) as fetch:
        got = coverart.fetch_cover(_RELEASE, _GROUP)
    assert got.source == "release-group"
    assert got.extension == "png"
    assert [c.args[0].split("/")[-1] for c in fetch.call_args_list] == ["front-1200", "front-500", "front-250", "front-1200"]


def test_release_size_degrades_before_group():
    with patch.object(coverart, "_get", side_effect=[None, _JPEG]) as fetch:
        got = coverart.fetch_cover(_RELEASE, _GROUP)
    assert got.source == "release"
    assert fetch.call_count == 2


def test_no_group_fallback():
    with patch.object(coverart, "_get", return_value=None) as fetch:
        assert coverart.fetch_cover(_RELEASE, _GROUP, group_fallback=False, size=500) is None
    assert fetch.call_count == 2
    assert all("release-group" not in c.args[0] for c in fetch.call_args_list)


def test_group_without_release():
    with patch.object(coverart, "_get", return_value=_PNG) as fetch:
        got = coverart.fetch_cover(None, _GROUP)
    assert got.source == "release-group"
    fetch.assert_called_once()


def test_bad_id_does_not_make_request():
    with patch.object(coverart, "_get") as fetch:
        with pytest.raises(coverart.CoverArtError, match="invalid MusicBrainz ID"):
            coverart.fetch_cover("../../etc/passwd", None)
    fetch.assert_not_called()


def test_nonimage_is_error_not_missing():
    with patch.object(coverart, "_get", return_value=b"<html>error</html>"):
        with pytest.raises(coverart.CoverArtError, match="non-image"):
            coverart.fetch_cover(_RELEASE, _GROUP)


def test_get_handles_404_and_503_without_confusing_them():
    response = MagicMock(status=404)
    with patch.object(coverart._session.pool, "request", return_value=response):
        assert coverart._get("https://coverartarchive.org/x") is None
    response.close.assert_called_once()

    response = MagicMock(status=503)
    with patch.object(coverart._session.pool, "request", return_value=response):
        with pytest.raises(coverart.CoverArtError, match="503"):
            coverart._get("https://coverartarchive.org/x")
    response.close.assert_called_once()


def test_get_checks_size_and_closes_response():
    response = MagicMock(status=200)
    response.read.return_value = b"x" * (coverart._MAX_BYTES + 1)
    with patch.object(coverart._session.pool, "request", return_value=response):
        with pytest.raises(coverart.CoverArtError, match="exceeds"):
            coverart._get("https://coverartarchive.org/x")
    response.close.assert_called_once()
