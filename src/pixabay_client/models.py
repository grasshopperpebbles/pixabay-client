"""Typed Pixabay results."""
from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from typing import Any, Generic, TypeVar
from urllib.parse import quote

T = TypeVar("T")


def _tags(raw: str) -> tuple[str, ...]:
    return tuple(t.strip() for t in raw.split(",") if t.strip())


def _user_url(user: str, user_id: int) -> str:
    return f"https://pixabay.com/users/{quote(user)}-{user_id}/"


@dataclass(frozen=True)
class Rendition:
    url: str
    width: int
    height: int


@dataclass(frozen=True)
class VideoRendition(Rendition):
    size: int
    thumbnail: str


@dataclass(frozen=True)
class _Hit:
    id: int
    page_url: str
    type: str
    tags: tuple[str, ...]
    views: int
    downloads: int
    likes: int
    comments: int
    user: str
    user_id: int
    user_image_url: str

    _noun = "Image"

    @property
    def user_url(self) -> str:
        return _user_url(self.user, self.user_id)

    @property
    def credit(self) -> str:
        """The plain "where it's from" line Pixabay asks you to show."""
        return f"{self._noun} by {self.user} from Pixabay"

    @property
    def credit_html(self) -> str:
        """The credit as HTML links (to the author and to the picture's Pixabay page), safely escaped."""
        return (f'{self._noun} by <a href="{escape(self.user_url)}">{escape(self.user)}</a> from '
                f'<a href="{escape(self.page_url)}">Pixabay</a>')


def _common(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(d["id"]), "page_url": d.get("pageURL", ""), "type": d.get("type", ""),
        "tags": _tags(d.get("tags", "")), "views": int(d.get("views", 0)), "downloads": int(d.get("downloads", 0)),
        "likes": int(d.get("likes", 0)), "comments": int(d.get("comments", 0)), "user": d.get("user", ""),
        "user_id": int(d.get("user_id", 0)), "user_image_url": d.get("userImageURL", ""),
    }


@dataclass(frozen=True)
class Image(_Hit):
    preview: Rendition = field(default_factory=lambda: Rendition("", 0, 0))
    webformat: Rendition = field(default_factory=lambda: Rendition("", 0, 0))  # max 640 px; URL valid 24 hours
    large: Rendition = field(default_factory=lambda: Rendition("", 0, 0))      # max 1280 px
    width: int = 0   # the original's size
    height: int = 0
    size: int = 0

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> Image:
        w, h = int(d.get("imageWidth", 0)), int(d.get("imageHeight", 0))
        # Pixabay gives no size for the large rendition: the original scaled to at most 1280 px.
        scale = min(1.0, 1280 / max(w, h, 1))
        return cls(
            **_common(d),
            preview=Rendition(d.get("previewURL", ""), int(d.get("previewWidth", 0)), int(d.get("previewHeight", 0))),
            webformat=Rendition(d.get("webformatURL", ""), int(d.get("webformatWidth", 0)),
                                int(d.get("webformatHeight", 0))),
            large=Rendition(d.get("largeImageURL", ""), round(w * scale), round(h * scale)),
            width=w, height=h, size=int(d.get("imageSize", 0)),
        )


@dataclass(frozen=True)
class Video(_Hit):
    duration: int = 0
    renditions: dict[str, VideoRendition] = field(default_factory=dict)  # large, medium, small, tiny (if present)

    _noun = "Video"

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> Video:
        renditions = {
            name: VideoRendition(r.get("url", ""), int(r.get("width", 0)), int(r.get("height", 0)),
                                 int(r.get("size", 0)), r.get("thumbnail", ""))
            for name, r in (d.get("videos") or {}).items() if r.get("url")
        }
        return cls(**_common(d), duration=int(d.get("duration", 0)), renditions=renditions)


@dataclass(frozen=True)
class SearchResult(Generic[T]):
    total: int        # all matches on Pixabay
    total_hits: int   # matches reachable through the API (Pixabay caps this, usually at 500)
    hits: list[T]
    page: int
    per_page: int


@dataclass(frozen=True)
class RateLimit:
    limit: int
    remaining: int
    reset_at: float  # clock time when the window resets
