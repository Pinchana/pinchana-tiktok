# 🎵 Pinchana TikTok Scraper

**Pinchana TikTok Scraper** is a specialized module for extracting high-quality media from TikTok. It uses a custom [yt-dlp](https://github.com/yt-dlp/yt-dlp) extractor to bypass platform restrictions and handles both videos and photo carousels.

---

## ✨ Key Features

- **📽 Full Media Support:** Extracts videos (with/without watermarks), image galleries (carousels), and background audio.
- **🛡 Anti-Ban Integration:** Automatically detects 403/429 errors and signals the VPN (Gluetun) to rotate IPs.
- **💾 Local Caching:** Saves downloaded media to a persistent LRU cache for fast re-serving.
- **🚀 Standalone Service:** Runs as a lightweight FastAPI service that can be proxied by the Pinchana Gateway.

---

## 🏗 Architecture

The scraper follows a "Scrape -> Download -> Cache" workflow:
1. **Metadata Extraction:** Uses `yt-dlp` with a custom extractor to get direct media URLs.
2. **Download:** Downloads files directly through the VPN tunnel.
3. **Storage:** Organizes files into a structured directory under `/app/cache/tiktok/{video_id}`.

---

## 📡 API Reference

### `POST /scrape`
Extracts and downloads media for a given TikTok URL.
```json
{
  "url": "https://www.tiktok.com/@user/video/1234567890"
}
```

### `GET /health`
Checks the service health and VPN connectivity.

---

## ⚙️ Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `CACHE_PATH` | `./cache` | Where to store downloaded media. |
| `CACHE_MAX_SIZE_GB` | `10.0` | Max size for the LRU cache. |
| `GLUETUN_CONTROL_URL` | `http://localhost:8000` | URL for the Gluetun control API. |

---

## 🛠 Development

Managed by `uv`.

```bash
uv sync
uv run uvicorn src.pinchana_tiktok.main:app --host 0.0.0.0 --port 8081
```

---

## 📜 License

MIT
