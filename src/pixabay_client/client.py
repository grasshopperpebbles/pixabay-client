"""The sync and async clients. Same methods, same rules; the async one awaits."""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

import httpx

from . import _core
from ._core import API, DAY, VIDEO_API, Cache, MemoryCache, Size
from .errors import NotFound, RateLimited, ServiceError
from .models import Image, RateLimit, SearchResult, Video

_UNSET: Any = object()


class _Base:
    def __init__(self, key: str | None, cache: Cache | None, cache_ttl: float, retries: int, max_wait: float,
                 clock: Callable[[], float]) -> None:
        self._key = _core.resolve_key(key)
        self._cache: Cache | None = MemoryCache() if cache is _UNSET else cache
        self._ttl = cache_ttl
        self._retries = retries
        self._max_wait = max_wait
        self._clock = clock
        self.rate_limit: RateLimit | None = None
        _core.install_log_redaction()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(key=***)"

    def _wait_before(self) -> float:
        """Seconds to wait before the next call: the window is used up and has not reset yet."""
        rl = self.rate_limit
        if rl is None or rl.remaining > 0:
            return 0.0
        return max(0.0, rl.reset_at - self._clock())

    def _too_long(self, wait: float) -> None:
        if wait > self._max_wait:
            raise RateLimited(f"Pixabay rate limit: the wait ({wait:.0f} s) is longer than max_wait.", wait)

    def _cached(self, url: str, params: dict[str, str]) -> tuple[str, Any | None]:
        key = _core.cache_key(url, params)
        return key, (self._cache.get(key) if self._cache is not None else None)

    def _store(self, key: str, data: Any) -> None:
        if self._cache is not None:
            self._cache.set(key, data, self._ttl)


class PixabayClient(_Base):
    """Pixabay API client. `key` defaults to the PIXABAY_API_KEY environment variable."""

    def __init__(self, key: str | None = None, *, cache: Cache | None = _UNSET, cache_ttl: float = DAY,
                 retries: int = 2, max_wait: float = 60.0, timeout: float = 20.0,
                 transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(key, cache, cache_ttl, retries, max_wait, clock)
        self._http = httpx.Client(timeout=timeout, transport=transport, follow_redirects=True)
        self._sleep = sleep

    def __enter__(self) -> PixabayClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _get(self, url: str, params: dict[str, str]) -> Any:
        key, hit = self._cached(url, params)
        if hit is not None:
            return hit
        for attempt in range(self._retries + 1):
            wait = self._wait_before()
            if wait:
                self._too_long(wait)
                self._sleep(wait)
            try:
                response = self._http.get(url, params={**params, "key": self._key})
            except httpx.HTTPError as e:
                raise ServiceError(f"Pixabay could not be reached ({type(e).__name__}).") from None
            self.rate_limit = _core.read_rate_limit(response, self._clock()) or self.rate_limit
            try:
                data = _core.check(response)
            except RateLimited as e:
                if attempt == self._retries:
                    raise
                self._too_long(e.retry_after)
                self._sleep(e.retry_after)
                continue
            self._store(key, data)
            return data
        raise AssertionError("unreachable")

    def search_images(self, q: str | None = None, *, lang: str = "en", image_type: str = "all",
                      orientation: str = "all", category: str | None = None, min_width: int = 0, min_height: int = 0,
                      colors: Iterable[str] | None = None, editors_choice: bool = False, safesearch: bool = True,
                      order: str = "popular", page: int = 1, per_page: int = 20,
                      exclude_people: bool = False) -> SearchResult[Image]:
        params = _core.image_params(q, lang=lang, image_type=image_type, orientation=orientation, category=category,
                                    min_width=min_width, min_height=min_height, colors=colors,
                                    editors_choice=editors_choice, safesearch=safesearch, order=order, page=page,
                                    per_page=per_page)
        return _core.images_result(self._get(API, params), page, per_page, exclude_people)

    def iter_images(self, q: str | None = None, *, max_results: int = 100, per_page: int = 50,
                    **kwargs: Any) -> Iterator[Image]:
        """Every hit, page after page, up to max_results (and Pixabay's own cap)."""
        seen, page = 0, 1
        while seen < max_results:
            res = self.search_images(q, page=page, per_page=per_page, **kwargs)
            for hit in res.hits:
                if seen == max_results:
                    return
                seen += 1
                yield hit
            if not res.hits or page * per_page >= res.total_hits:
                return
            page += 1

    def get_image(self, image_id: int) -> Image:
        data = self._get(API, {"id": str(image_id)})
        hits = _core.images_result(data, 1, 1, False).hits
        return hits[0] if hits else _not_found()

    def search_videos(self, q: str | None = None, *, lang: str = "en", video_type: str = "all",
                      category: str | None = None, min_width: int = 0, min_height: int = 0,
                      editors_choice: bool = False, safesearch: bool = True, order: str = "popular", page: int = 1,
                      per_page: int = 20) -> SearchResult[Video]:
        params = _core.video_params(q, lang=lang, video_type=video_type, category=category, min_width=min_width,
                                    min_height=min_height, editors_choice=editors_choice, safesearch=safesearch,
                                    order=order, page=page, per_page=per_page)
        return _core.videos_result(self._get(VIDEO_API, params), page, per_page)

    def get_video(self, video_id: int) -> Video:
        data = self._get(VIDEO_API, {"id": str(video_id)})
        hits = _core.videos_result(data, 1, 1).hits
        return hits[0] if hits else _not_found()

    def download(self, image: Image, *, size: Size = "large", dest: str | Path | None = None) -> Any:
        """The picture's bytes, or with `dest` saved there (returns the Path). Pixabay forbids permanent
        hotlinking: download what you keep."""
        url = httpx.URL(_core.download_url(image, size))
        try:
            for _ in range(_core.MAX_DOWNLOAD_REDIRECTS + 1):
                response = self._http.get(url, follow_redirects=False)
                if not response.is_redirect:
                    break
                url = _core.check_download_url(response.next_request.url if response.next_request else "")
            else:
                raise ServiceError("The download redirected too many times.")
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise ServiceError(f"The download failed ({type(e).__name__}).") from None
        return _save(response.content, dest)


class AsyncPixabayClient(_Base):
    """The async twin of PixabayClient."""

    def __init__(self, key: str | None = None, *, cache: Cache | None = _UNSET, cache_ttl: float = DAY,
                 retries: int = 2, max_wait: float = 60.0, timeout: float = 20.0,
                 transport: httpx.AsyncBaseTransport | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(key, cache, cache_ttl, retries, max_wait, clock)
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=True)
        self._sleep = sleep

    async def __aenter__(self) -> AsyncPixabayClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    async def _get(self, url: str, params: dict[str, str]) -> Any:
        key, hit = self._cached(url, params)
        if hit is not None:
            return hit
        for attempt in range(self._retries + 1):
            wait = self._wait_before()
            if wait:
                self._too_long(wait)
                await self._sleep(wait)
            try:
                response = await self._http.get(url, params={**params, "key": self._key})
            except httpx.HTTPError as e:
                raise ServiceError(f"Pixabay could not be reached ({type(e).__name__}).") from None
            self.rate_limit = _core.read_rate_limit(response, self._clock()) or self.rate_limit
            try:
                data = _core.check(response)
            except RateLimited as e:
                if attempt == self._retries:
                    raise
                self._too_long(e.retry_after)
                await self._sleep(e.retry_after)
                continue
            self._store(key, data)
            return data
        raise AssertionError("unreachable")

    async def search_images(self, q: str | None = None, *, lang: str = "en", image_type: str = "all",
                            orientation: str = "all", category: str | None = None, min_width: int = 0,
                            min_height: int = 0, colors: Iterable[str] | None = None, editors_choice: bool = False,
                            safesearch: bool = True, order: str = "popular", page: int = 1, per_page: int = 20,
                            exclude_people: bool = False) -> SearchResult[Image]:
        params = _core.image_params(q, lang=lang, image_type=image_type, orientation=orientation, category=category,
                                    min_width=min_width, min_height=min_height, colors=colors,
                                    editors_choice=editors_choice, safesearch=safesearch, order=order, page=page,
                                    per_page=per_page)
        return _core.images_result(await self._get(API, params), page, per_page, exclude_people)

    async def iter_images(self, q: str | None = None, *, max_results: int = 100, per_page: int = 50,
                          **kwargs: Any) -> AsyncIterator[Image]:
        seen, page = 0, 1
        while seen < max_results:
            res = await self.search_images(q, page=page, per_page=per_page, **kwargs)
            for hit in res.hits:
                if seen == max_results:
                    return
                seen += 1
                yield hit
            if not res.hits or page * per_page >= res.total_hits:
                return
            page += 1

    async def get_image(self, image_id: int) -> Image:
        data = await self._get(API, {"id": str(image_id)})
        hits = _core.images_result(data, 1, 1, False).hits
        return hits[0] if hits else _not_found()

    async def search_videos(self, q: str | None = None, *, lang: str = "en", video_type: str = "all",
                            category: str | None = None, min_width: int = 0, min_height: int = 0,
                            editors_choice: bool = False, safesearch: bool = True, order: str = "popular",
                            page: int = 1, per_page: int = 20) -> SearchResult[Video]:
        params = _core.video_params(q, lang=lang, video_type=video_type, category=category, min_width=min_width,
                                    min_height=min_height, editors_choice=editors_choice, safesearch=safesearch,
                                    order=order, page=page, per_page=per_page)
        return _core.videos_result(await self._get(VIDEO_API, params), page, per_page)

    async def get_video(self, video_id: int) -> Video:
        data = await self._get(VIDEO_API, {"id": str(video_id)})
        hits = _core.videos_result(data, 1, 1).hits
        return hits[0] if hits else _not_found()

    async def download(self, image: Image, *, size: Size = "large", dest: str | Path | None = None) -> Any:
        url = httpx.URL(_core.download_url(image, size))
        try:
            for _ in range(_core.MAX_DOWNLOAD_REDIRECTS + 1):
                response = await self._http.get(url, follow_redirects=False)
                if not response.is_redirect:
                    break
                url = _core.check_download_url(response.next_request.url if response.next_request else "")
            else:
                raise ServiceError("The download redirected too many times.")
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise ServiceError(f"The download failed ({type(e).__name__}).") from None
        return _save(response.content, dest)


def _not_found() -> Any:
    raise NotFound("Pixabay has no item with that id.")


def _save(content: bytes, dest: str | Path | None) -> bytes | Path:
    if dest is None:
        return content
    path = Path(dest)
    path.write_bytes(content)
    return path
