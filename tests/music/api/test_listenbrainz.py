"""Tests for the ListenBrainz Labs client — recorded similar-artists rows, no network."""

import json
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from tool.music.api.listenbrainz import SIMILAR_ARTISTS_ALGORITHM, similar_artists
from tool.music.metadata import Artist

CORRE = "0850da7c-c769-48c1-a626-cfc2b73d4863"
EMANCIPATOR = "aa1d4315-5246-42b2-b62b-f997d046d8b2"


def test_parses_rows_and_keeps_seed(load_fixture):
    text = json.dumps(load_fixture("lb_similar_artists.json"))
    with patch("tool.music.api.listenbrainz._session.get_text", return_value=text):
        result = similar_artists([CORRE, EMANCIPATOR])

    assert len(result) == 20
    top = max((s for s in result if s.seed == CORRE), key=lambda s: s.score)
    assert top.artist == Artist(EMANCIPATOR, "Emancipator", "")
    assert top.score == 361
    assert {s.seed for s in result} == {CORRE, EMANCIPATOR}


def test_one_request_with_repeated_seed_parameter():
    with patch("tool.music.api.listenbrainz._session.get_text", return_value="[]") as get:
        similar_artists([CORRE, EMANCIPATOR])

    assert get.call_count == 1
    query = parse_qs(urlsplit(get.call_args.args[0]).query)
    assert query["artist_mbids"] == [CORRE, EMANCIPATOR]
    assert query["algorithm"] == [SIMILAR_ARTISTS_ALGORITHM]
