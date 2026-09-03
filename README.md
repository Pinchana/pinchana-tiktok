# Pinchana TikTok

This FastAPI module extracts supported public TikTok posts through a dedicated yt-dlp workflow. It handles ordinary videos and photo slideshows, including slideshow background audio when available.

## Processing flow

1. When `TIKTOK_DIRECT_WEB_PRIMARY` is enabled, resolve short TikTok URLs from their first redirect without downloading the destination page.
2. In the same mode, extract ordinary videos from the canonical page's hydration payload in one metadata request and use its direct CDN mirrors.
3. Use TikTok's anonymous player endpoint as the primary photo-post path and as the established video fallback; retain TikTok playback-host rewrites as a final media fallback.
4. Reuse anonymous cookies, pace independent scrape jobs globally, and retry temporary failures within a bounded policy.
5. When the deployment-wide VPN is enabled, rotate Gluetun once after a confirmed platform block.
6. Download ordered media to `/app/cache/tiktok/{post_id}` in containers.

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
| `YTDLP_CONCURRENCY` | `1` | Maximum concurrent yt-dlp operations |
| `TIKTOK_DIRECT_WEB_PRIMARY` | `false` | Enable one-page video extraction and first-redirect short URL resolution |
| `TIKTOK_REQUEST_INTERVAL_SECONDS` | `2.0` | Minimum delay between independent TikTok job starts |
| `TIKTOK_INTERNAL_REQUEST_INTERVAL_SECONDS` | `0` | Optional delay between requests inside one job; keep `0` unless block-rate data requires it |
| `TIKTOK_RETRY_DELAY_SECONDS` | `2.0` | Delay before retrying a web challenge with a fresh anonymous session |
| `TIKTOK_FORMAT_ATTEMPTS` | `3` | Maximum watermark-free video formats attempted before refreshing media URLs |
| `TIKTOK_TRANSCODE_HEVC` | `false` | Optionally convert HEVC HD renditions to H.264 for legacy clients; native Telegram apps normally do not need this |
| `TIKTOK_HEVC_TRANSCODE_TIMEOUT_SECONDS` | `180` | Maximum time allowed for an HEVC compatibility conversion |
| `TIKTOK_VPN_ROTATION_COOLDOWN_SECONDS` | `30` | Minimum delay between TikTok-triggered Gluetun reconnects |
| `TIKTOK_PROXY_URL` | empty | Optional HTTP/SOCKS proxy applied to extraction and every media download |
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

Development Compose keeps `VPN_ENABLED=false`. VPN operation is controlled only
by the deployment-wide `VPN_ENABLED` setting; there is no TikTok-specific VPN
toggle. The scraper supports anonymous public posts only and does not accept
account cookies, fabricated mobile identifiers, or CAPTCHA-solving services.

Live tests are opt-in. Set `PINCHANA_TIKTOK_LIVE=1` and provide the four
`TIKTOK_LIVE_*_URL` values documented in `.env.example` before running pytest.
