from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

KEY = "test-key-1234567890"


def image_hit(i: int = 1, tags: str = "cat, animal, pet", user: str = "Alice") -> dict[str, Any]:
    return {
        "id": i, "pageURL": f"https://pixabay.com/photos/cat-{i}/", "type": "photo", "tags": tags,
        "previewURL": f"https://cdn.pixabay.com/photo/p{i}_150.jpg", "previewWidth": 150, "previewHeight": 100,
        "webformatURL": f"https://pixabay.com/get/w{i}_640.jpg", "webformatWidth": 640, "webformatHeight": 427,
        "largeImageURL": f"https://pixabay.com/get/l{i}_1280.jpg", "imageWidth": 4000, "imageHeight": 2667,
        "imageSize": 123456, "views": 10, "downloads": 5, "likes": 2, "comments": 1,
        "user_id": 42, "user": user, "userImageURL": "https://cdn.pixabay.com/user/42.jpg",
    }


def video_hit(i: int = 7) -> dict[str, Any]:
    def r(w: int, h: int) -> dict[str, Any]:
        return {"url": f"https://cdn.pixabay.com/video/{i}_{w}.mp4", "width": w, "height": h, "size": w * 10,
                "thumbnail": f"https://cdn.pixabay.com/video/{i}_{w}.jpg"}
    return {
        "id": i, "pageURL": f"https://pixabay.com/videos/v-{i}/", "type": "film", "tags": "sea, wave", "duration": 12,
        "videos": {"large": {"url": "", "width": 0, "height": 0, "size": 0, "thumbnail": ""},
                   "medium": r(1920, 1080), "small": r(1280, 720), "tiny": r(960, 540)},
        "views": 1, "downloads": 1, "likes": 1, "comments": 0, "user_id": 9, "user": "Bob", "userImageURL": "",
    }


class Pixabay:
    """A fake Pixabay: records requests; `reply` decides each answer (default: one image hit)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.reply: Callable[[httpx.Request], httpx.Response] = self.ok

    @staticmethod
    def ok(request: httpx.Request, hits: list[dict[str, Any]] | None = None, remaining: int = 99) -> httpx.Response:
        if request.url.host != "pixabay.com" or not request.url.path.startswith("/api"):
            return httpx.Response(200, content=b"\x89PNG-bytes")  # a download
        hits = hits if hits is not None else ([video_hit()] if "videos" in request.url.path else [image_hit()])
        body = {"total": 1000, "totalHits": 500, "hits": hits}
        return httpx.Response(200, text=json.dumps(body), headers={
            "X-RateLimit-Limit": "100", "X-RateLimit-Remaining": str(remaining), "X-RateLimit-Reset": "30"})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.reply(request)

    @property
    def api_calls(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.startswith("/api")]


@pytest.fixture
def fake() -> Pixabay:
    return Pixabay()


@pytest.fixture
def sleeps() -> list[float]:
    return []
