"""TikTok scraper plugin — mounts as a FastAPI router."""

import asyncio
import os
import re
import logging
from pathlib import Path
from fastapi import FastAPI, APIRouter, HTTPException
from pinchana_core.models import ScrapeRequest, ScrapeResponse, MediaItem
from pinchana_core.storage import MediaStorage
from pinchana_core.plugins import ScraperPlugin, registry
from pinchana_core.vpn import GluetunController, VpnRotationError
from .api import TikTokScraper
from yt_dlp import YoutubeDL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()
gluetun = GluetunController()
storage = MediaStorage(
    base_path=os.getenv("CACHE_PATH", "./cache"),
    max_size_gb=float(os.getenv("CACHE_MAX_SIZE_GB", "10.0")),
)


def _media_url_to_path(url: str | None):
    if not url:
        return None
    url = str(url)
    if not url.startswith("/media/"):
        return None
    path_part = url.split("?", 1)[0][len("/media/"):]
    parts = path_part.split("/", 2)
    if len(parts) < 3:
        return None
    platform, post_id, filename = parts[0], parts[1], parts[2]
    if platform != "tiktok" or not post_id or not filename:
        return None
    return storage.base_path / post_id / filename


def _cached_media_ready(metadata: dict) -> bool:
    if not isinstance(metadata, dict):
        return False

    urls: list[str] = []
    for key in ("thumbnail_url", "video_url", "audio_url"):
        url = metadata.get(key)
        if url:
            urls.append(url)

    carousel = metadata.get("carousel") or []
    if isinstance(carousel, list):
        for item in carousel:
            if not isinstance(item, dict):
                continue
            for key in ("thumbnail_url", "video_url"):
                url = item.get(key)
                if url:
                    urls.append(url)

    for url in urls:
        path = _media_url_to_path(url)
        if not path or not path.exists():
            return False

    return True


def _build_ydl(
    outtmpl: dict | str,
    *,
    fmt: str | None = None,
    write_thumbnail: bool = False,
    skip_download: bool = False,
    noplaylist: bool = False,
    cookies_from: YoutubeDL | None = None,
) -> YoutubeDL:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": outtmpl,
        "noplaylist": noplaylist,
        "overwrites": True,
        "retries": 2,
        "fragment_retries": 2,
    }
    if fmt:
        opts["format"] = fmt
    if write_thumbnail:
        opts["writethumbnail"] = True
    if skip_download:
        opts["skip_download"] = True
    ydl = YoutubeDL(opts)
    if cookies_from:
        for cookie in cookies_from.cookiejar:
            ydl.cookiejar.set_cookie(cookie)
    return ydl


def _find_downloaded_file(base_dir: Path, prefix: str) -> Path | None:
    matches = sorted(p for p in base_dir.glob(f"{prefix}.*") if p.is_file())
    return matches[0] if matches else None


def _replace_file(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    src.replace(dest)
    return dest


def _download_with_ydl(ydl: YoutubeDL, info: dict) -> dict:
    result = ydl.process_ie_result(info, download=True)
    return ydl.sanitize_info(result)


class RateLimitError(Exception):
    """Raised when TikTok blocks the request (403/429/IP ban)."""
    pass


async def trigger_rotation():
    """Trigger VPN IP rotation."""
    logger.warning("Rotating VPN IP...")
    try:
        await gluetun.rotate_ip()
    except VpnRotationError as e:
        logger.warning(f"VPN rotation failed: {e}")
        raise RateLimitError(str(e))


def _is_rate_limited(e: Exception) -> bool:
    """Check if an exception indicates rate-limiting or IP blocking."""
    msg = str(e).lower()
    return any(
        x in msg
        for x in (
            "blocked",
            "403",
            "429",
            "rate limit",
            "too many requests",
            "verify",
            "captcha",
            "unavailable",
        )
    )


def extract_video_id(url: str) -> str:
    match = re.search(r"/(?:video|photo)/(\d+)", str(url))
    if match:
        return match.group(1)
    return url


async def _download_and_build_response(video_id: str, info: dict, scraper: TikTokScraper) -> ScrapeResponse:
    storage.prepare_post_dir(video_id)

    post_dir = storage._post_dir(video_id)
    carousel_dir = post_dir / "carousel"
    carousel_dir.mkdir(parents=True, exist_ok=True)

    title = info.get("title") or info.get("description") or video_id
    author = info.get("uploader") or info.get("channel") or ""
    media_type = info.get("_type", "video")

    thumbnail_url = ""
    video_url = None
    carousel_items = []
    audio_url = None

    if media_type == "playlist":
        download_error = False
        image_dir = post_dir / "images"
        image_dir.mkdir(parents=True, exist_ok=True)

        for old in image_dir.glob("*"):
            if old.is_file():
                old.unlink()

        entries = info.get("entries") or []
        image_entries = [
            entry for entry in entries
            if entry and entry.get("url") and entry.get("ext") and not entry.get("formats")
            and entry.get("ext") not in ("m4a", "mp3", "aac")
        ]
        audio_entry = next(
            (
                entry for entry in entries
                if entry and entry.get("formats")
                and (entry.get("vcodec") or entry["formats"][0].get("vcodec")) == "none"
            ),
            None,
        )

        if image_entries:
            image_info = {**info, "entries": image_entries}
            image_outtmpl = str(image_dir / "%(playlist_index)02d.%(ext)s")
            image_ydl = _build_ydl(image_outtmpl, cookies_from=scraper._ydl)
            try:
                await asyncio.to_thread(_download_with_ydl, image_ydl, image_info)
            except Exception as e:
                download_error = True
                logger.error("Image download failed: %s", e)

            image_files = sorted(p for p in image_dir.glob("*.*") if p.is_file())
            for idx, img_path in enumerate(image_files):
                ext = img_path.suffix.lstrip(".") or "jpg"
                dest = carousel_dir / f"{idx}_thumbnail.{ext}"
                _replace_file(img_path, dest)
                carousel_items.append(MediaItem(
                    index=idx,
                    media_type="image",
                    thumbnail_url=f"/media/tiktok/{video_id}/carousel/{idx}_thumbnail.{ext}",
                    video_url=None,
                ))

        if carousel_items:
            thumbnail_url = carousel_items[0].thumbnail_url

        audio_url = None
        if audio_entry:
            audio_outtmpl = str(post_dir / "audio.%(ext)s")
            audio_ydl = _build_ydl(audio_outtmpl, fmt="bestaudio/best", noplaylist=True, cookies_from=scraper._ydl)
            try:
                await asyncio.to_thread(_download_with_ydl, audio_ydl, audio_entry)
            except Exception as e:
                download_error = True
                logger.error("Audio download failed: %s", e)

        audio_file = _find_downloaded_file(post_dir, "audio")
        if audio_file:
            audio_ext = audio_file.suffix.lstrip(".")
            audio_url = f"/media/tiktok/{video_id}/audio.{audio_ext}"

        media_type = "carousel"

        if download_error and not carousel_items and not audio_url:
            raise HTTPException(status_code=503, detail="Media download failed")

    else:
        download_error = False
        video_outtmpl = str(post_dir / "video.%(ext)s")
        video_ydl = _build_ydl(video_outtmpl, fmt="best[ext=mp4]/best", noplaylist=True, cookies_from=scraper._ydl)
        try:
            await asyncio.to_thread(_download_with_ydl, video_ydl, info)
        except Exception as e:
            download_error = True
            logger.error("Video download failed: %s", e)

        thumb_outtmpl = {
            "default": str(post_dir / "video.%(ext)s"),
            "thumbnail": str(post_dir / "thumbnail.%(ext)s"),
        }
        thumb_ydl = _build_ydl(
            thumb_outtmpl,
            write_thumbnail=True,
            skip_download=True,
            fmt="best",
            noplaylist=True,
            cookies_from=scraper._ydl,
        )
        try:
            await asyncio.to_thread(_download_with_ydl, thumb_ydl, info)
        except Exception as e:
            download_error = True
            logger.error("Thumbnail download failed: %s", e)

        video_file = _find_downloaded_file(post_dir, "video")
        if video_file:
            video_ext = video_file.suffix.lstrip(".")
            video_url = f"/media/tiktok/{video_id}/video.{video_ext}"

        thumb_file = _find_downloaded_file(post_dir, "thumbnail")
        if thumb_file:
            thumb_ext = thumb_file.suffix.lstrip(".")
            thumbnail_url = f"/media/tiktok/{video_id}/thumbnail.{thumb_ext}"

        media_type = "video"

        if download_error and not video_url:
            raise HTTPException(status_code=503, detail="Media download failed")

    response = ScrapeResponse(
        shortcode=video_id,
        caption=title,
        author=author,
        media_type=media_type,
        thumbnail_url=thumbnail_url,
        video_url=video_url,
        audio_url=audio_url,
        carousel=carousel_items if carousel_items else None,
    )

    metadata = response.model_dump()
    if audio_url:
        metadata["audio_url"] = audio_url
    storage.save_metadata(video_id, metadata)
    return response


@router.post("/scrape", response_model=ScrapeResponse)
async def process_scrape_request(request: ScrapeRequest):
    url = str(request.url)
    video_id = None
    last_error = None

    for attempt in range(1, 4):
        scraper = TikTokScraper()
        try:
            if "vm.tiktok.com" in url or "vt.tiktok.com" in url or re.search(r"v[a-z]\.tiktok\.com", url) or "/t/" in url:
                url = scraper.resolve_short_url(url)

            if video_id is None:
                video_id = extract_video_id(url)

            if storage.is_cached(video_id):
                cached = storage.load_metadata(video_id)
                if cached and _cached_media_ready(cached):
                    logger.info("Cache hit for %s", video_id)
                    return ScrapeResponse(**cached)
                logger.info("Cache invalid for %s, missing media; re-scraping", video_id)

            logger.info(f"Scraping TikTok: {video_id} (attempt {attempt})")
            info = scraper.extract_video(url)
            return await _download_and_build_response(video_id, info, scraper)
        except Exception as e:
            last_error = e
            if _is_rate_limited(e):
                logger.warning(f"Attempt {attempt} rate-limited/blocked: {e}")
                if attempt < 3:
                    try:
                        await trigger_rotation()
                    except RateLimitError:
                        await asyncio.sleep(30)
                    else:
                        await asyncio.sleep(5)
            else:
                logger.error(f"Attempt {attempt} failed: {e}")
                if attempt < 3:
                    await asyncio.sleep(5)

    if isinstance(last_error, HTTPException):
        raise last_error
    raise HTTPException(
        status_code=503 if _is_rate_limited(last_error) else 500,
        detail=str(last_error)
    )


@router.get("/health")
async def health_check():
    try:
        status = await gluetun.get_vpn_status()
        vpn_status = status.get("status", "").lower()
        if vpn_status != "running":
            raise HTTPException(status_code=503, detail=f"VPN not running: {vpn_status}")
        return {"status": "healthy", "service": "tiktok", "vpn": status}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"VPN check failed: {e}")


# Register with the global plugin registry on import.
registry.register(ScraperPlugin(
    name="tiktok",
    router=router,
    route_patterns=["tiktok.com", "vm.tiktok.com", "vt.tiktok.com", "v*.tiktok.com"],
))

# Standalone FastAPI app for container mode
app = FastAPI(title="Pinchana TikTok", version="0.1.0")
app.include_router(router)
