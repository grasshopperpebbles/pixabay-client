# pixabay-client

A typed Python client for the [Pixabay API](https://pixabay.com/api/docs/), sync and async, that **follows
Pixabay's API rules for you**:

- **Caching:** Pixabay asks that requests be cached for 24 hours. Results are cached in memory for 24 hours by
  default (bring your own cache, or turn it off).
- **Rate limits:** reads `X-RateLimit-*` headers, waits when the window is used up, and retries a `429` after the
  reset time.
- **No hotlinking:** Pixabay forbids permanent hotlinking. `download()` saves the picture for you, and only ever
  fetches from Pixabay's own hosts.
- **Credit:** `image.credit` / `image.credit_html` give the "where it's from" line Pixabay asks you to show.
- **Your key stays private:** never in `repr`, error messages or logs (httpx's request log line is redacted).
- **Safe by default:** `safesearch` is on unless you turn it off; optional `exclude_people=True` drops hits
  tagged with people.
- Images **and** videos, typed results (`py.typed`), Python 3.10+, one dependency (`httpx`).

```bash
pip install pixabay-client
```

## Use

```python
from pixabay_client import PixabayClient

with PixabayClient("YOUR_KEY") as px:           # or set PIXABAY_API_KEY
    result = px.search_images("tabby cat", image_type="photo", per_page=20)
    for img in result.hits:
        print(img.id, img.tags, img.webformat.url, img.credit)

    img = px.get_image(result.hits[0].id)
    px.download(img, dest="cat.jpg")              # size: "large" (default), "webformat" or "preview"

    for img in px.iter_images("fox", max_results=100):   # pages for you
        ...

    videos = px.search_videos("waves", video_type="film")
    clip = videos.hits[0].renditions["medium"]    # large / medium / small / tiny (missing ones left out)
```

Async:

```python
from pixabay_client import AsyncPixabayClient

async with AsyncPixabayClient() as px:
    result = await px.search_images("owl")
    data = await px.download(result.hits[0])
```

### Errors

| Exception | When |
|---|---|
| `AuthError` | missing or invalid API key |
| `NotFound` | `get_image` / `get_video` with an id Pixabay does not have |
| `RateLimited` | still rate limited after the retries (`retries=2` by default) |
| `ServiceError` | Pixabay answered 5xx or could not be reached |
| `PixabayError` | base class; any other error |

### Options

`PixabayClient(key=None, *, cache=MemoryCache(), cache_ttl=86400, retries=2, max_wait=60, timeout=20,
transport=None)`. `cache=None` turns caching off; any object with `get(key)` / `set(key, value, ttl)` works as a
cache (Redis, disk…). Waits longer than `max_wait` seconds raise `RateLimited` instead of sleeping.

## Pixabay's terms

You need a free API key from [pixabay.com/api/docs](https://pixabay.com/api/docs/). The API is for real human
requests: no systematic mass downloads. Show your users where pictures come from. Read the
[Pixabay content license](https://pixabay.com/service/license-summary/): depicted people, trademarks and
privacy rights can still apply to a picture.

Not affiliated with Pixabay.

## License

MIT
