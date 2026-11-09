"""What the sync and async clients share: parameters, the cache, rate limits, errors and key redaction."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Literal, Protocol

import httpx

from .errors import AuthError, NotFound, PixabayError, RateLimited, ServiceError
from .models import Image, RateLimit, SearchResult, Video

API = "https://pixabay.com/api/"
VIDEO_API = "https://pixabay.com/api/videos/"
DAY = 24 * 3600
DOWNLOAD_HOSTS = ("pixabay.com", "cdn.pixabay.com")
Size = Literal["large", "webformat", "preview"]

# Tags that mark a hit as showing people (exclude_people=True). A tag match, not a guarantee.
PEOPLE_TAGS = frozenset({
    "people", "person", "man", "woman", "men", "women", "girl", "girls", "boy", "boys", "child", "children", "kid",
    "kids", "baby", "human", "humans", "lady", "guy", "couple", "family", "model", "teen", "teenager", "adult",
})


class Cache(Protocol):
    def get(self, key: str) -> Any | None: ...
    def set(self, key: str, value: Any, ttl: float) -> None: ...


class MemoryCache:
    """A small thread-safe in-memory TTL cache (the default)."""

    def __init__(self, max_items: int = 1000, clock: Callable[[], float] = time.monotonic) -> None:
        self._items: dict[str, tuple[float, Any]] = {}
        self._max = max_items
        self._clock = clock
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            if item[0] <= self._clock():
                del self._items[key]
                return None
            return item[1]

    def set(self, key: str, value: Any, ttl: float) -> None:
        with self._lock:
            if len(self._items) >= self._max:
                self._items.pop(next(iter(self._items)))  # the oldest
            self._items[key] = (self._clock() + ttl, value)


class _RedactKey(logging.Filter):
    _key = re.compile(r"([?&]key=)[^&\s'\"]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.msg = record.getMessage()
            record.args = None
        if isinstance(record.msg, str):
            record.msg = self._key.sub(r"\1***", record.msg)
        return True


def install_log_redaction() -> None:
    """httpx logs every request URL, and Pixabay wants the key in the URL: redact it (once per logger)."""
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, _RedactKey) for f in logger.filters):
            logger.addFilter(_RedactKey())


def resolve_key(key: str | None) -> str:
    key = key or os.environ.get("PIXABAY_API_KEY", "")
    if not key:
        raise AuthError("No Pixabay API key: pass one or set PIXABAY_API_KEY.")
    return key


def _flag(v: bool) -> str:
    return "true" if v else "false"


def image_params(q: str | None, *, lang: str, image_type: str, orientation: str, category: str | None,
                 min_width: int, min_height: int, colors: Iterable[str] | None, editors_choice: bool,
                 safesearch: bool, order: str, page: int, per_page: int) -> dict[str, str]:
    p = _shared(q, lang=lang, category=category, min_width=min_width, min_height=min_height,
                editors_choice=editors_choice, safesearch=safesearch, order=order, page=page, per_page=per_page)
    p.update(image_type=image_type, orientation=orientation)
    if colors:
        p["colors"] = ",".join(colors)
    return p


def video_params(q: str | None, *, lang: str, video_type: str, category: str | None, min_width: int,
                 min_height: int, editors_choice: bool, safesearch: bool, order: str, page: int,
                 per_page: int) -> dict[str, str]:
    p = _shared(q, lang=lang, category=category, min_width=min_width, min_height=min_height,
                editors_choice=editors_choice, safesearch=safesearch, order=order, page=page, per_page=per_page)
    p["video_type"] = video_type
    return p


def _shared(q: str | None, *, lang: str, category: str | None, min_width: int, min_height: int,
            editors_choice: bool, safesearch: bool, order: str, page: int, per_page: int) -> dict[str, str]:
    if q is not None and len(q) > 100:
        raise ValueError("Pixabay search terms are at most 100 characters.")
    if not 3 <= per_page <= 200:
        raise ValueError("per_page must be between 3 and 200.")
    if page < 1:
        raise ValueError("page starts at 1.")
    p = {"lang": lang, "min_width": str(min_width), "min_height": str(min_height),
         "editors_choice": _flag(editors_choice), "safesearch": _flag(safesearch), "order": order,
         "page": str(page), "per_page": str(per_page)}
    if q:
        p["q"] = q
    if category:
        p["category"] = category
    return p


def cache_key(url: str, params: Mapping[str, str]) -> str:
    return url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))  # the API key is never part of it


def check(response: httpx.Response) -> Any:
    """The JSON body of a good answer; else the matching error (Pixabay answers errors in plain text)."""
    if response.status_code == 200:
        try:
            return response.json()
        except json.JSONDecodeError as e:
            raise ServiceError("Pixabay sent an answer that is not JSON.") from e
    text = response.text.strip()[:200]
    if response.status_code == 429:
        raise RateLimited("Pixabay rate limit exceeded.", retry_after=reset_seconds(response))
    if response.status_code >= 500:
        raise ServiceError(f"Pixabay error {response.status_code}.")
    low = text.lower()
    if "api key" in low:
        raise AuthError("Invalid or missing Pixabay API key.")
    # Live 2026-11-09: "[ERROR 400] Image #N does not exist." (older answers said "out of valid range").
    if "does not exist" in low or "out of valid range" in low:
        raise NotFound("Pixabay has no item with that id.")
    raise PixabayError(f"Pixabay error {response.status_code}: {_scrub(text)}")


def _scrub(text: str) -> str:
    return re.sub(r"key=[^&\s]+", "key=***", text)


def reset_seconds(response: httpx.Response) -> float:
    try:
        return max(0.0, float(response.headers.get("X-RateLimit-Reset", "60")))
    except ValueError:
        return 60.0


def read_rate_limit(response: httpx.Response, now: float) -> RateLimit | None:
    h = response.headers
    if "X-RateLimit-Remaining" not in h:
        return None
    try:
        return RateLimit(int(h.get("X-RateLimit-Limit", "0")), int(h["X-RateLimit-Remaining"]),
                         now + reset_seconds(response))
    except ValueError:
        return None


def images_result(data: dict[str, Any], page: int, per_page: int, exclude_people: bool) -> SearchResult[Image]:
    hits = [Image.from_api(h) for h in data.get("hits", [])]
    if exclude_people:
        hits = [h for h in hits if not PEOPLE_TAGS.intersection(t.lower() for t in h.tags)]
    return SearchResult(int(data.get("total", 0)), int(data.get("totalHits", 0)), hits, page, per_page)


def videos_result(data: dict[str, Any], page: int, per_page: int) -> SearchResult[Video]:
    return SearchResult(int(data.get("total", 0)), int(data.get("totalHits", 0)),
                        [Video.from_api(h) for h in data.get("hits", [])], page, per_page)


def download_url(item: Image, size: Size) -> str:
    url = {"large": item.large, "webformat": item.webformat, "preview": item.preview}[size].url
    host = httpx.URL(url).host if url else ""
    if httpx.URL(url).scheme != "https" or host not in DOWNLOAD_HOSTS:
        raise ValueError("Downloads come only from Pixabay's own hosts over https.")
    return url
