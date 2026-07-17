# Pinchana TikTok

This FastAPI module extracts supported public TikTok posts through a dedicated yt-dlp workflow. It handles ordinary videos and photo slideshows, including slideshow background audio when available.

## Processing flow

1. Resolve supported canonical and short TikTok URLs.
2. Extract media metadata with the project extractor.
3. Rotate the Gluetun connection and retry within the bounded policy after relevant platform blocks.
4. Download ordered media to `/app/cache/tiktok/{post_id}` in containers.

The gateway's API v1 response represents slideshow images as ordered `content` assets and their audio as a `soundtrack` asset.

## API

- `POST /scrape` accepts `{"url":"https://www.tiktok.com/@account/video/POST_ID"}`.
- `GET /health` reports module and VPN readiness.

External clients should call the gateway's authenticated `POST /v1/scrape` route.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `CACHE_PATH` | `./cache` | Base media cache path |
| `CACHE_MAX_SIZE_GB` | `10.0` | Maximum cache size before eviction |
| `GLUETUN_CONTROL_URL` | `http://localhost:8000` | Private Gluetun control endpoint |

## Development

```sh
uv sync --frozen
uv run uvicorn pinchana_tiktok.main:app --host 0.0.0.0 --port 8081 --reload
```

```sh
# Run from the parent pinchana-api directory.
docker build --file pinchana-tiktok/Dockerfile --tag pinchana-tiktok:local .
```
