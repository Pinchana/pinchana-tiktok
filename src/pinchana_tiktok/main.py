"""TikTok scraper plugin — mounts as a FastAPI router."""

import asyncio
import os
import re
import logging
from fastapi import FastAPI, APIRouter, HTTPException
from pinchana_core.models import ScrapeRequest, ScrapeResponse, MediaItem
from pinchana_core.storage import MediaStorage
from pinchana_core.plugins import ScraperPlugin, registry
from .api import TikTokScraper

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()
scraper = TikTokScraper()
storage = MediaStorage(
    base_path=os.getenv("CACHE_PATH", "./cache"),
    max_size_gb=float(os.getenv("CACHE_MAX_SIZE_GB", "10.0")),
)


def extract_video_id(url: str) -> str:
    match = re.search(r"/(?:video|photo)/(\d+)", str(url))
    if match:
        return match.group(1)
    return url


async def _download_and_build_response(video_id: str, info: dict) -> ScrapeResponse:
    storage.prepare_post_dir(video_id)

    title = info.get("title") or info.get("description") or video_id
    author = info.get("uploader") or info.get("channel") or ""
    media_type = info.get("_type", "video")

    thumbnail_url = ""
    video_url = None
    carousel_items = []
    audio_url = None

    if media_type == "playlist":
        entries = info.get("entries", [])
        image_entries = [e for e in entries if e.get("ext") == "jpg"]
        audio_entries = [e for e in entries if e.get("formats") and e["formats"][0].get("vcodec") == "none"]

        tasks = []
        for idx, img in enumerate(image_entries):
            dest = storage.carousel_thumbnail_path(video_id, idx)
            tasks.append(storage.download(img["url"], dest))

        for aud in audio_entries:
            fmt = aud["formats"][0]
            ext = fmt.get("ext", "mp3")
            dest = storage._post_dir(video_id) / f"audio.{ext}"
            tasks.append(storage.download(fmt["url"], dest))
            audio_url = f"/media/tiktok/{video_id}/audio.{ext}"

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.error(f"Download error: {r}")

        for idx, img in enumerate(image_entries):
            carousel_items.append(MediaItem(
                index=idx,
                media_type="image",
                thumbnail_url=f"/media/tiktok/{video_id}/carousel/{idx}_thumbnail.jpg",
                video_url=None,
            ))

        if image_entries:
            thumbnail_url = f"/media/tiktok/{video_id}/carousel/0_thumbnail.jpg"
        media_type = "carousel"

    else:
        formats = info.get("formats", [])
        best_video = None
        for fmt in formats:
            if fmt.get("vcodec") != "none" and fmt.get("url"):
                best_video = fmt
                break

        thumbnails = info.get("thumbnails", [])
        best_thumb = thumbnails[0]["url"] if thumbnails else None

        tasks = []
        if best_thumb:
            tasks.append(storage.download(best_thumb, storage.thumbnail_path(video_id)))
        if best_video:
            ext = best_video.get("ext", "mp4")
            dest = storage._post_dir(video_id) / f"video.{ext}"
            tasks.append(storage.download(best_video["url"], dest))
            video_url = f"/media/tiktok/{video_id}/video.{ext}"

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.error(f"Download error: {r}")

        thumbnail_url = f"/media/tiktok/{video_id}/thumbnail.jpg" if best_thumb else ""
        media_type = "video"

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
    if "vm.tiktok.com" in url or "vt.tiktok.com" in url or "/t/" in url:
        url = scraper.resolve_short_url(url)

    video_id = extract_video_id(url)

    if storage.is_cached(video_id):
        logger.info(f"Cache hit for {video_id}")
        return ScrapeResponse(**storage.load_metadata(video_id))

    logger.info(f"Scraping TikTok: {video_id}")
    try:
        info = scraper.extract_video(url)
        return await _download_and_build_response(video_id, info)
    except Exception as e:
        logger.error(f"Scrape failed for {video_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Scrape failed: {e}")


@router.get("/health")
async def health_check():
    return {"status": "healthy", "service": "tiktok"}


# Register with the global plugin registry on import.
registry.register(ScraperPlugin(
    name="tiktok",
    router=router,
    route_patterns=["tiktok.com", "vm.tiktok.com", "vt.tiktok.com"],
))

# Standalone FastAPI app for container mode
app = FastAPI(title="Pinchana TikTok", version="0.1.0")
app.include_router(router)
