from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict

SavedScope = Literal["profile", "saved"]
"""Distinguishes normal profile membership from saved-collection membership.

Used instead of a bare bool so callers must be explicit and a missing
argument can't silently collapse saved posts into the normal scope."""

PostType = Literal["image", "carousel", "video"]


@dataclass
class ScrapedPost:
    """A scraped Instagram post, before files are downloaded."""

    shortid: str
    post_type: PostType
    media_urls: list[str]
    username: str | None = None
    caption: str | None = None

    def __post_init__(self) -> None:
        if not self.shortid:
            raise ValueError("shortid cannot be empty")
        if self.post_type not in ("image", "carousel", "video"):
            raise ValueError(f"invalid post_type: {self.post_type!r}")
        if not isinstance(self.media_urls, list):
            raise TypeError("media_urls must be a list")


@dataclass
class Pace:
    """Timing parameters for the profile scraper.

    Use the class methods for the three named presets. FAST is the default.
    """

    inter_post: tuple[float, float]  # sleep between posts
    skip_post: tuple[float, float]  # sleep when skipping a done post
    resume_cooldown: tuple[float, float]  # sleep after resume click before loop
    modal_think: tuple[float, float]  # pause after modal DOM is ready
    slide_render: float  # wait after clicking carousel Next

    @classmethod
    def fast(cls) -> Pace:
        return cls(
            inter_post=(1.5, 3.0),
            skip_post=(0.05, 0.2),
            resume_cooldown=(3.0, 6.0),
            modal_think=(0.1, 0.4),
            slide_render=0.2,
        )


class ScrapeInfo(TypedDict):
    username: str | None
    caption: str | None
    type: str
    mediaUrls: list[str] | None


class ModalBtnPos(TypedDict):
    docX: float
    docY: float
    viewH: float
