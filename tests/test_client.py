from __future__ import annotations

import logging

import httpx
import pytest

from pixabay_client import (
    AsyncPixabayClient, AuthError, Image, MemoryCache, NotFound, PixabayClient, PixabayError, RateLimited,
    ServiceError, Video,
)

from .conftest import KEY, Pixabay, image_hit


def client(fake: Pixabay, sleeps: list[float], **kw: object) -> PixabayClient:
    return PixabayClient(KEY, transport=httpx.MockTransport(fake.handler), sleep=sleeps.append, **kw)  # type: ignore[arg-type]


def test_search_images_maps_params_and_hits(fake: Pixabay, sleeps: list[float]) -> None:
    c = client(fake, sleeps)
    res = c.search_images("tabby cat", image_type="photo", per_page=30, colors=["red", "blue"])
    q = fake.api_calls[0].url.params
    assert q["q"] == "tabby cat" and q["image_type"] == "photo" and q["per_page"] == "30"
    assert q["colors"] == "red,blue" and q["safesearch"] == "true"  # safe search on by default
    assert res.total == 1000 and res.total_hits == 500 and res.page == 1
    img = res.hits[0]
    assert isinstance(img, Image) and img.id == 1 and img.tags == ("cat", "animal", "pet")
    assert img.large.url.endswith("l1_1280.jpg") and img.width == 4000 and img.user == "Alice"


def test_query_longer_than_100_characters_is_refused(fake: Pixabay, sleeps: list[float]) -> None:
    with pytest.raises(ValueError):
        client(fake, sleeps).search_images("x" * 101)
    assert fake.requests == []


def test_per_page_bounds(fake: Pixabay, sleeps: list[float]) -> None:
    for bad in (2, 201):
        with pytest.raises(ValueError):
            client(fake, sleeps).search_images("cat", per_page=bad)


def test_results_are_cached_for_24_hours_by_default(fake: Pixabay, sleeps: list[float]) -> None:
    now = [1000.0]
    c = client(fake, sleeps, cache=MemoryCache(clock=lambda: now[0]))
    c.search_images("cat")
    c.search_images("cat")
    assert len(fake.api_calls) == 1
    now[0] += 24 * 3600 + 1
    c.search_images("cat")
    assert len(fake.api_calls) == 2


def test_cache_off(fake: Pixabay, sleeps: list[float]) -> None:
    c = client(fake, sleeps, cache=None)
    c.search_images("cat")
    c.search_images("cat")
    assert len(fake.api_calls) == 2


def test_get_image_and_not_found(fake: Pixabay, sleeps: list[float]) -> None:
    c = client(fake, sleeps)
    assert c.get_image(1).id == 1 and fake.api_calls[0].url.params["id"] == "1"
    fake.reply = lambda r: httpx.Response(400, text='[ERROR 400] "id" is out of valid range.')
    with pytest.raises(NotFound):
        c.get_image(999999999)
    fake.reply = lambda r: httpx.Response(400, text="[ERROR 400] Image #99999999999 does not exist.")  # live wording
    with pytest.raises(NotFound):
        c.get_image(99999999999)


def test_bad_key_is_auth_error_and_never_shows_the_key(fake: Pixabay, sleeps: list[float]) -> None:
    fake.reply = lambda r: httpx.Response(400, text="[ERROR 400] Invalid or missing API key (https://pixabay.com/api/docs/).")
    with pytest.raises(AuthError) as e:
        client(fake, sleeps).search_images("cat")
    assert KEY not in str(e.value) and KEY not in repr(e.value)


def test_other_errors(fake: Pixabay, sleeps: list[float]) -> None:
    fake.reply = lambda r: httpx.Response(400, text="[ERROR 400] something else")
    with pytest.raises(PixabayError):
        client(fake, sleeps).search_images("cat")
    fake.reply = lambda r: httpx.Response(503, text="down")
    with pytest.raises(ServiceError):
        client(fake, sleeps, cache=None).search_images("cat")


def test_rate_limit_is_read_and_a_429_waits_and_retries(fake: Pixabay, sleeps: list[float]) -> None:
    c = client(fake, sleeps, cache=None)
    c.search_images("cat")
    assert c.rate_limit is not None and c.rate_limit.limit == 100 and c.rate_limit.remaining == 99
    answers = [httpx.Response(429, text="API rate limit exceeded", headers={"X-RateLimit-Reset": "7"}),
               Pixabay.ok(httpx.Request("GET", "https://pixabay.com/api/"))]
    fake.reply = lambda r: answers.pop(0)
    assert c.search_images("dog").hits
    assert sleeps == [7.0]


def test_429_gives_up_after_retries(fake: Pixabay, sleeps: list[float]) -> None:
    fake.reply = lambda r: httpx.Response(429, text="API rate limit exceeded", headers={"X-RateLimit-Reset": "3"})
    with pytest.raises(RateLimited):
        client(fake, sleeps, cache=None, retries=2).search_images("cat")
    assert sleeps == [3.0, 3.0]


def test_no_requests_left_waits_before_the_next_call(fake: Pixabay, sleeps: list[float]) -> None:
    fake.reply = lambda r: Pixabay.ok(r, remaining=0)
    c = client(fake, sleeps, cache=None, clock=lambda: 0.0)
    c.search_images("cat")
    c.search_images("dog")
    assert sleeps == [30.0]  # X-RateLimit-Reset of the first answer


def test_key_never_in_repr_or_logs(fake: Pixabay, sleeps: list[float], caplog: pytest.LogCaptureFixture) -> None:
    c = client(fake, sleeps)
    assert KEY not in repr(c)
    with caplog.at_level(logging.DEBUG):
        c.search_images("cat")
        logging.getLogger("httpx").info("HTTP Request: GET https://pixabay.com/api/?key=%s&q=cat", KEY)
    assert KEY not in caplog.text


def test_key_from_environment(monkeypatch: pytest.MonkeyPatch, fake: Pixabay, sleeps: list[float]) -> None:
    monkeypatch.setenv("PIXABAY_API_KEY", KEY)
    c = PixabayClient(transport=httpx.MockTransport(fake.handler))
    c.search_images("cat")
    assert fake.api_calls[0].url.params["key"] == KEY
    monkeypatch.delenv("PIXABAY_API_KEY")
    with pytest.raises(AuthError):
        PixabayClient()


def test_a_malformed_hit_is_skipped(fake: Pixabay, sleeps: list[float]) -> None:
    fake.reply = lambda r: Pixabay.ok(r, hits=[image_hit(1), {"id": "bad"}, "junk", image_hit(2)])
    assert [h.id for h in client(fake, sleeps).search_images("cat").hits] == [1, 2]


def test_exclude_people_filter(fake: Pixabay, sleeps: list[float]) -> None:
    fake.reply = lambda r: Pixabay.ok(r, hits=[image_hit(1, "cat, pet"), image_hit(2, "woman, cat"),
                                                 image_hit(3, "children, park")])
    res = client(fake, sleeps).search_images("cat", exclude_people=True)
    assert [h.id for h in res.hits] == [1]


def test_download_saves_from_pixabay_only(fake: Pixabay, sleeps: list[float], tmp_path: object) -> None:
    c = client(fake, sleeps)
    img = c.search_images("cat").hits[0]
    assert c.download(img) == b"\x89PNG-bytes"
    assert fake.requests[-1].url.path.endswith("l1_1280.jpg")
    out = c.download(img, size="webformat", dest=tmp_path / "cat.jpg")  # type: ignore[operator]
    assert out.read_bytes() == b"\x89PNG-bytes"  # type: ignore[union-attr]
    evil = Image.from_api({**image_hit(), "largeImageURL": "http://169.254.169.254/latest"})
    with pytest.raises(ValueError):
        c.download(evil)


def test_credit_lines() -> None:
    img = Image.from_api(image_hit(user="Alice"))
    assert img.credit == "Image by Alice from Pixabay"
    assert 'href="https://pixabay.com/users/Alice-42/"' in img.credit_html
    assert 'href="https://pixabay.com/photos/cat-1/"' in img.credit_html
    assert "<script>" not in Image.from_api(image_hit(user="<script>")).credit_html


def test_videos(fake: Pixabay, sleeps: list[float]) -> None:
    c = client(fake, sleeps)
    res = c.search_videos("sea", video_type="film")
    assert fake.api_calls[0].url.path == "/api/videos/" and fake.api_calls[0].url.params["video_type"] == "film"
    v = res.hits[0]
    assert isinstance(v, Video) and v.duration == 12 and v.renditions["medium"].width == 1920
    assert "large" not in v.renditions  # Pixabay sends an empty large rendition when there is none
    assert v.credit == "Video by Bob from Pixabay"


def test_iter_images_pages_until_enough(fake: Pixabay, sleeps: list[float]) -> None:
    fake.reply = lambda r: Pixabay.ok(r, hits=[image_hit(int(r.url.params["page"]) * 10 + i) for i in range(3)])
    ids = [h.id for h in client(fake, sleeps).iter_images("cat", per_page=3, max_results=7)]
    assert ids == [10, 11, 12, 20, 21, 22, 30]


async def test_async_client(fake: Pixabay, sleeps: list[float]) -> None:
    async def asleep(s: float) -> None:
        sleeps.append(s)
    async with AsyncPixabayClient(KEY, transport=httpx.MockTransport(fake.handler), sleep=asleep) as c:
        res = await c.search_images("cat")
        assert res.hits[0].id == 1
        await c.search_images("cat")
        assert len(fake.api_calls) == 1  # cached
        assert await c.download(res.hits[0]) == b"\x89PNG-bytes"
        assert (await c.get_video(7)).id == 7
