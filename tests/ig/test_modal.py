"""Tests for _collect_modal_images — the SPA modal scraping path.

The core insight being tested: profile scraping reads post data directly from the
rendered article DOM in the SPA modal, without ever navigating to /p/<shortid>/.
This means the browser never issues a full Document load for a post URL, which is
what was triggering 429 rate limits when using a separate scraping tab.
"""

import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from tool.ig.browser import ProtocolError
from tool.ig.scraper import _collect_modal_images
from tool.ig.types import Pace

_CDN_URL_1 = "https://scontent.cdninstagram.com/v/t51.82787-15/img1.jpg?stp=dst-jpg_e15"
_CDN_URL_2 = "https://scontent.fbcdn.net/v/t51.82787-15/img2.jpg?stp=dst-jpg_e15"
_VIDEO_URL = "https://scontent.cdninstagram.com/v/video.mp4"


def _video_url(tag: str, *, duration: int, bitrate: int) -> str:
    efg = (
        base64.urlsafe_b64encode(json.dumps({"vencode_tag": tag, "duration_s": duration, "bitrate": bitrate}).encode())
        .decode()
        .rstrip("=")
    )
    return f"https://scontent.cdninstagram.com/o1/v/t2/f2/video-{duration}-{bitrate}.mp4?efg={efg}"


def _zero_pace() -> Pace:
    """Pace with all sleeps at zero so tests don't wait."""
    return Pace(
        inter_post=(0, 0),
        skip_post=(0, 0),
        resume_cooldown=(0, 0),
        modal_think=(0, 0),
        slide_render=0,
    )


class _FastClock:
    """Returns 0.0 for the first *budget* calls, then monotonically huge values
    so every busy-wait deadline check fails after the budget is exhausted."""

    def __init__(self, budget: int = 4):
        self._n = 0
        self._budget = budget

    def __call__(self):
        self._n += 1
        if self._n <= self._budget:
            return 0.0
        return self._n * 1e9


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr("tool.ig.scraper.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("tool.ig.scraper.think", AsyncMock())
    mock_loop = MagicMock()
    mock_loop.time = _FastClock()
    monkeypatch.setattr("tool.ig.scraper.asyncio.get_running_loop", lambda: mock_loop)


# ---------------------------------------------------------------------------
# Pace
# ---------------------------------------------------------------------------


class TestPace:
    def test_fast_has_valid_ranges(self):
        p = Pace.fast()
        assert p.inter_post[0] <= p.inter_post[1]
        assert p.skip_post[0] <= p.skip_post[1]
        assert p.resume_cooldown[0] <= p.resume_cooldown[1]
        assert p.modal_think[0] <= p.modal_think[1]
        assert p.slide_render >= 0


@pytest.fixture
def _no_scroll_click(monkeypatch):
    monkeypatch.setattr("tool.ig.scraper.human_scroll", AsyncMock())
    monkeypatch.setattr("tool.ig.scraper.human_click", AsyncMock())


def _carousel_tab(slides: list[str]):
    """Tab mock that simulates a carousel.

    State machine: img_index starts at 1.  Each call to img_index_js advances
    it by 1 (simulating a click taking effect).  has_next is True until the
    last slide is current.  MODAL_IMGS_JS returns urls[0:current_index].
    """
    idx = [1]
    total = len(slides)

    async def fake_eval(js):
        if "srcset" in js:
            return slides[: idx[0]]
        if "article button" in js:
            return idx[0] < total
        if "URLSearchParams" in js:
            cur = idx[0]
            idx[0] = min(idx[0] + 1, total)
            return cur
        if "getBoundingClientRect" in js:
            return {"docX": 500.0, "docY": 400.0, "viewH": 800.0}
        return None

    tab = MagicMock()
    tab.evaluate = AsyncMock(side_effect=fake_eval)
    return tab


@pytest.mark.asyncio
class TestCollectModalImages:
    @pytest.fixture(autouse=True)
    def _patches(self, _no_scroll_click):
        pass

    async def test_single_image_returns_immediately(self):
        tab = MagicMock()
        tab.evaluate = AsyncMock(return_value=[_CDN_URL_1])
        result = await _collect_modal_images(tab, "X", is_carousel=False, pace=_zero_pace())
        assert result == [_CDN_URL_1]
        tab.evaluate.assert_awaited_once()

    async def test_single_image_deduplicates(self):
        tab = MagicMock()
        tab.evaluate = AsyncMock(return_value=[_CDN_URL_1, _CDN_URL_1])
        result = await _collect_modal_images(tab, "X", is_carousel=False, pace=_zero_pace())
        assert result == [_CDN_URL_1]

    async def test_carousel_no_next_button_returns_initial(self):
        """Carousel with a single slide (has_next=False): returns the initial URL."""

        async def fake_eval(js):
            if "srcset" in js:
                return [_CDN_URL_1]
            if "article button" in js:
                return False
            return None

        tab = MagicMock()
        tab.evaluate = AsyncMock(side_effect=fake_eval)
        result = await _collect_modal_images(tab, "X", is_carousel=True, pace=_zero_pace())
        assert result == [_CDN_URL_1]

    async def test_carousel_two_slides_collects_both(self):
        tab = _carousel_tab([_CDN_URL_1, _CDN_URL_2])
        result = await _collect_modal_images(tab, "X", is_carousel=True, pace=_zero_pace())
        assert result == [_CDN_URL_1, _CDN_URL_2]

    async def test_carousel_three_slides(self):
        third = "https://scontent.cdninstagram.com/img3.jpg"
        tab = _carousel_tab([_CDN_URL_1, _CDN_URL_2, third])
        result = await _collect_modal_images(tab, "X", is_carousel=True, pace=_zero_pace())
        assert result == [_CDN_URL_1, _CDN_URL_2, third]

    async def test_carousel_clicks_article_next_button(self, monkeypatch):
        """Profile modals also have a document-level next-post button; ignore it."""
        clicked = []

        async def fake_click(tab, x, y):
            clicked.append((x, y))

        monkeypatch.setattr("tool.ig.scraper.human_click", fake_click)

        idx = [1]

        async def fake_eval(js):
            if "srcset" in js:
                return [_CDN_URL_1] if idx[0] == 1 else [_CDN_URL_1, _CDN_URL_2]
            if "article button" in js:
                return idx[0] < 2
            if "URLSearchParams" in js:
                cur = idx[0]
                idx[0] = 2
                return cur
            if "getBoundingClientRect" in js:
                assert "document.querySelector('article')" in js or 'document.querySelector("article")' in js
                return {"docX": 106.0, "docY": 400.0, "viewH": 800.0}
            return None

        tab = MagicMock()
        tab.evaluate = AsyncMock(side_effect=fake_eval)
        result = await _collect_modal_images(tab, "X", is_carousel=True, pace=_zero_pace())
        assert result == [_CDN_URL_1, _CDN_URL_2]
        assert clicked == [(106.0, 400.0)]

    async def test_carousel_tolerates_stale_img_index_from_previous_post(self, monkeypatch):
        """ArrowRight can carry ?img_index=2 into the next carousel URL."""
        third = "https://scontent.cdninstagram.com/img3.jpg"
        current_slide = [1]

        async def fake_click(tab, x, y):
            current_slide[0] = min(current_slide[0] + 1, 3)

        monkeypatch.setattr("tool.ig.scraper.human_click", fake_click)
        fake_loop = MagicMock()
        t = [0.0]

        def fake_time():
            t[0] += 4.0
            return t[0]

        fake_loop.time.side_effect = fake_time
        monkeypatch.setattr("tool.ig.scraper.asyncio.get_running_loop", lambda: fake_loop)

        async def fake_eval(js):
            if "srcset" in js:
                if current_slide[0] < 3:
                    return [_CDN_URL_1, _CDN_URL_2]
                return [_CDN_URL_1, _CDN_URL_2, third]
            if "article button" in js:
                return current_slide[0] < 3
            if "URLSearchParams" in js:
                return 2
            if "getBoundingClientRect" in js:
                return {"docX": 500.0, "docY": 400.0, "viewH": 800.0}
            return None

        tab = MagicMock()
        tab.evaluate = AsyncMock(side_effect=fake_eval)
        result = await _collect_modal_images(tab, "X", is_carousel=True, pace=_zero_pace())
        assert result == [_CDN_URL_1, _CDN_URL_2, third]

    async def test_carousel_no_new_urls_stops_loop(self):
        """If clicking Next reveals no new URLs, the loop exits early."""

        async def fake_eval(js):
            if "srcset" in js:
                return [_CDN_URL_1]  # never changes
            if "article button" in js:
                return True  # always claims Next exists
            if "URLSearchParams" in js:
                return 2  # always different from prev (simulates advance)
            if "getBoundingClientRect" in js:
                return {"docX": 500.0, "docY": 400.0, "viewH": 800.0}
            return None

        tab = MagicMock()
        tab.evaluate = AsyncMock(side_effect=fake_eval)
        result = await _collect_modal_images(tab, "X", is_carousel=True, pace=_zero_pace())
        assert result == [_CDN_URL_1]

    async def test_carousel_no_btn_pos_stops_loop(self):
        """BTN_DOC_POS_JS returning None (button not in viewport) stops collection."""

        async def fake_eval(js):
            if "srcset" in js:
                return [_CDN_URL_1]
            if "article button" in js:
                return True
            if "getBoundingClientRect" in js:
                return None
            return None

        tab = MagicMock()
        tab.evaluate = AsyncMock(side_effect=fake_eval)
        result = await _collect_modal_images(tab, "X", is_carousel=True, pace=_zero_pace())
        assert result == [_CDN_URL_1]

    async def test_carousel_click_no_effect_stops_loop(self, monkeypatch):
        """img_index never advances (deadline expires) → loop exits, returns what we have."""
        fake_loop = MagicMock()
        t = [0.0]

        def fake_time():
            t[0] += 10.0
            return t[0]

        fake_loop.time.side_effect = fake_time
        monkeypatch.setattr("tool.ig.scraper.asyncio.get_running_loop", lambda: fake_loop)

        async def fake_eval(js):
            if "srcset" in js:
                return [_CDN_URL_1]
            if "article button" in js:
                return True
            if "URLSearchParams" in js:
                return 1  # never changes → click had no effect
            if "getBoundingClientRect" in js:
                return {"docX": 500.0, "docY": 400.0, "viewH": 800.0}
            return None

        tab = MagicMock()
        tab.evaluate = AsyncMock(side_effect=fake_eval)
        result = await _collect_modal_images(tab, "X", is_carousel=True, pace=_zero_pace())
        assert result == [_CDN_URL_1]

    async def test_carousel_out_of_bounds_pointer_uses_js_click_fallback(self, monkeypatch):
        clicked = []

        async def bad_click(tab, x, y):
            raise ProtocolError("input.performActions: move target out of bounds")

        monkeypatch.setattr("tool.ig.scraper.human_click", bad_click)

        async def fake_eval(js):
            if "srcset" in js:
                return [_CDN_URL_1] if not clicked else [_CDN_URL_1, _CDN_URL_2]
            if ".click()" in js:
                clicked.append(True)
                return None
            if "article button" in js:
                return not clicked
            if "URLSearchParams" in js:
                return 1 if not clicked else 2
            if "getBoundingClientRect" in js:
                return {"docX": -1.0, "docY": -1.0, "viewH": 800.0}
            return None

        tab = MagicMock()
        tab.evaluate = AsyncMock(side_effect=fake_eval)
        result = await _collect_modal_images(tab, "X", is_carousel=True, pace=_zero_pace())
        assert result == [_CDN_URL_1, _CDN_URL_2]

    async def test_carousel_deduplicates_across_slides(self):
        """A URL appearing in multiple DOM reads is included only once."""
        # slide 2 DOM read returns both slide 1 and slide 2 URLs.
        reads = iter([[_CDN_URL_1], [_CDN_URL_1, _CDN_URL_2]])
        idx = [1]

        async def fake_eval(js):
            if "srcset" in js:
                return next(reads, [_CDN_URL_1, _CDN_URL_2])
            if "article button" in js:
                return idx[0] < 2
            if "URLSearchParams" in js:
                cur = idx[0]
                idx[0] += 1
                return cur
            if "getBoundingClientRect" in js:
                return {"docX": 500.0, "docY": 400.0, "viewH": 800.0}
            return None

        tab = MagicMock()
        tab.evaluate = AsyncMock(side_effect=fake_eval)
        result = await _collect_modal_images(tab, "X", is_carousel=True, pace=_zero_pace())
        assert result.count(_CDN_URL_1) == 1
        assert _CDN_URL_2 in result

    async def test_carousel_replaces_low_res_placeholders_after_slide_render(self, monkeypatch):
        """Offscreen placeholder URLs should not win over rendered slide URLs."""
        lows = [
            "https://scontent.cdninstagram.com/v/t51.82787-15/slide1.jpg?stp=dst-jpg_s150x150",
            "https://scontent.cdninstagram.com/v/t51.82787-15/slide2.jpg?stp=dst-jpg_s150x150",
            "https://scontent.cdninstagram.com/v/t51.82787-15/slide3.jpg?stp=dst-jpg_s150x150",
        ]
        highs = [
            "https://scontent.cdninstagram.com/v/t51.82787-15/slide1.jpg?stp=dst-jpg_p1080x1080",
            "https://scontent.cdninstagram.com/v/t51.82787-15/slide2.jpg?stp=dst-jpg_p1080x1080",
            "https://scontent.cdninstagram.com/v/t51.82787-15/slide3.jpg?stp=dst-jpg_p1080x1080",
        ]
        current_slide = [1]

        async def fake_click(tab, x, y):
            current_slide[0] = min(current_slide[0] + 1, 3)

        monkeypatch.setattr("tool.ig.scraper.human_click", fake_click)

        async def fake_eval(js):
            if "srcset" in js:
                if current_slide[0] == 1:
                    return [highs[0], lows[1], lows[2]]
                return [highs[current_slide[0] - 1]]
            if "article button" in js:
                return current_slide[0] < 3
            if "URLSearchParams" in js:
                return current_slide[0]
            if "getBoundingClientRect" in js:
                return {"docX": 500.0, "docY": 400.0, "viewH": 800.0}
            return None

        tab = MagicMock()
        tab.evaluate = AsyncMock(side_effect=fake_eval)
        result = await _collect_modal_images(tab, "X", is_carousel=True, pace=_zero_pace())
        assert result == highs

    async def test_carousel_prefers_network_hq_variant_for_same_filename(self):
        low = "https://scontent.cdninstagram.com/v/t51.82787-15/slide1.jpg?stp=dst-jpg_e35_p640x640_tt6"
        high = "https://scontent.cdninstagram.com/v/t51.82787-15/slide1.jpg?stp=dst-jpg_e35_p1080x1080_tt6"
        tab = MagicMock()
        tab.evaluate = AsyncMock(return_value=[low])

        result = await _collect_modal_images(
            tab,
            "X",
            is_carousel=False,
            pace=_zero_pace(),
            captured_image_urls=[high],
        )

        assert result == [high]

    async def test_carousel_prefers_unconstrained_network_variant_for_same_filename(self):
        low = "https://scontent.cdninstagram.com/v/t51.82787-15/slide1.jpg?stp=dst-jpg_e35_p640x640_tt6"
        high = "https://scontent.cdninstagram.com/v/t51.82787-15/slide1.jpg?stp=dst-jpg_e35_tt6"
        tab = MagicMock()
        tab.evaluate = AsyncMock(return_value=[low])

        result = await _collect_modal_images(
            tab,
            "X",
            is_carousel=False,
            pace=_zero_pace(),
            captured_image_urls=[high],
        )

        assert result == [high]
