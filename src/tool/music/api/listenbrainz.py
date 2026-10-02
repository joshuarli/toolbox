"""ListenBrainz Labs client — similar artists, from listening-session data.

No token is needed for similar artists.  Docs: https://listenbrainz.readthedocs.io/en/latest/
"""

import json
from typing import Any, NamedTuple
from urllib.parse import urlencode

from tool.music.http import RateLimiter, Session
from tool.music.metadata import Artist

LABS_BASE = "https://labs.api.listenbrainz.org"
# Names the trained model; ListenBrainz may retire it, in which case lookups return an error.
SIMILAR_ARTISTS_ALGORITHM = "session_based_days_9000_session_300_contribution_5_threshold_15_limit_50_skip_30"

_session = Session(user_agent="music-tag/0.1", rate_limiter=RateLimiter(rate=1.0))


class Similar(NamedTuple):
    artist: Artist
    score: int  # only comparable among results for the same seed
    seed: str  # MBID of the artist this one is similar to


def similar_artists(seed_mbids: list[str]) -> list[Similar]:
    """Artists similar to any of *seed_mbids*, in a single request.

    Scores come back on a different scale per seed (popular seeds score in the thousands), so
    callers should normalise per `Similar.seed`.  Order is not by score.
    """
    query = urlencode([("artist_mbids", m) for m in seed_mbids] + [("algorithm", SIMILAR_ARTISTS_ALGORITHM)])
    # The endpoint wants the parameter repeated, which Session's dict params can't express.
    # The response is a JSON list, which get_json's dict typing doesn't cover.
    rows: list[dict[str, Any]] = json.loads(_session.get_text(f"{LABS_BASE}/similar-artists/json?{query}"))
    return [
        Similar(Artist(r["artist_mbid"], r["name"], r.get("comment") or ""), r["score"], r["reference_mbid"])
        for r in rows
    ]
