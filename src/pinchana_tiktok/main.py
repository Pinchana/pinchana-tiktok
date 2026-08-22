"""TikTok scraper plugin — mounts as a FastAPI router."""

import asyncio
import copy
import os
import re
import logging
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from fastapi import FastAPI, APIRouter, HTTPException
from pinchana_core.models import ScrapeRequest, ScrapeResponse, MediaItem
from pinchana_core.storage import MediaStorage
from pinchana_core.plugins import ScraperPlugin, registry
from pinchana_core.vpn import GluetunController
from .api import (
    TikTokScraper,
    proxy_url,
    request_interval_seconds,
    tiktok_session_cache,
)
from yt_dlp import YoutubeDL
from yt_dlp.postprocessor.ffmpeg import FFmpegExtractAudioPP
from yt_dlp.version import __version__ as YTDLP_VERSION

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()
gluetun = GluetunController(
    rotation_cooldown=float(
        os.getenv("TIKTOK_VPN_ROTATION_COOLDOWN_SECONDS", "30")
    )
)
storage = MediaStorage(
    base_path=os.getenv("CACHE_PATH", "./cache"),
    max_size_gb=float(os.getenv("CACHE_MAX_SIZE_GB", "10.0")),
)
TIKTOK_VIDEO_CACHE_VERSION = 2


class TikTokUpstreamRunner:
    """Bound blocking yt-dlp work and pace starts across all requests."""

    def __init__(self, concurrency: int, interval: float):
        self._limit = asyncio.Semaphore(max(1, concurrency))
        self._pace_lock = asyncio.Lock()
        self._interval = max(0.0, interval)
        self._last_started = 0.0

    async def run(self, function, *args):
        async with self._limit:
            async with self._pace_lock:
                delay = self._interval - (time.monotonic() - self._last_started)
                if delay > 0:
                    await asyncio.sleep(delay)
                self._last_started = time.monotonic()
            return await asyncio.to_thread(function, *args)


UPSTREAM_RUNNER = TikTokUpstreamRunner(
    int(os.getenv("YTDLP_CONCURRENCY", "1")),
    request_interval_seconds(),
)
TIKTOK_MAX_ATTEMPTS = 4


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

    if (
        metadata.get("media_type") == "video"
        and metadata.get("_tiktok_video_cache_version") != TIKTOK_VIDEO_CACHE_VERSION
    ):
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

    if carousel and metadata.get("audio_url") and not str(metadata["audio_url"]).endswith(".mp3"):
        return False

    for url in urls:
        path = _media_url_to_path(url)
        if not path or not path.exists():
            return False

    return True


def _download_options(
    outtmpl: dict | str,
    *,
    fmt: str | None = None,
    write_thumbnail: bool = False,
    skip_download: bool = False,
    noplaylist: bool = False,
) -> dict:
    options: dict = {
        "outtmpl": outtmpl if isinstance(outtmpl, dict) else {"default": outtmpl},
        "noplaylist": noplaylist,
        "overwrites": True,
        "retries": 2,
        "fragment_retries": 2,
        "writethumbnail": write_thumbnail,
        "skip_download": skip_download,
    }
    if fmt:
        options["format"] = fmt
    return options


@contextmanager
def _temporary_ydl_configuration(
    ydl: YoutubeDL,
    options: dict,
    *,
    extract_audio_mp3: bool = False,
):
    """Apply download options without replacing the extraction session."""
    original_params = ydl.params.copy()
    original_postprocessors = {
        stage: list(postprocessors)
        for stage, postprocessors in ydl._pps.items()
    }
    ydl.params.update(options)
    if extract_audio_mp3:
        ydl.add_post_processor(
            FFmpegExtractAudioPP(
                ydl,
                preferredcodec="mp3",
                preferredquality="192",
            )
        )
    try:
        yield
    finally:
        ydl.params.clear()
        ydl.params.update(original_params)
        ydl._pps = original_postprocessors


def _find_downloaded_file(base_dir: Path, prefix: str) -> Path | None:
    matches = sorted(p for p in base_dir.glob(f"{prefix}.*") if p.is_file())
    return matches[0] if matches else None


def _replace_file(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    src.replace(dest)
    return dest


def _download_with_ydl(
    ydl: YoutubeDL,
    info: dict,
    options: dict,
    extract_audio_mp3: bool = False,
) -> dict:
    with _temporary_ydl_configuration(
        ydl,
        options,
        extract_audio_mp3=extract_audio_mp3,
    ):
        result = ydl.process_ie_result(info, download=True)
        return ydl.sanitize_info(result)


async def _download_with_ydl_bounded(
    scraper: TikTokScraper,
    info: dict,
    options: dict,
    *,
    extract_audio_mp3: bool = False,
) -> dict:
    return await UPSTREAM_RUNNER.run(
        _download_with_ydl,
        scraper._ydl,
        info,
        options,
        extract_audio_mp3,
    )


class RateLimitError(Exception):
    """Raised when TikTok blocks the request (403/429/IP ban)."""
    pass


class AuthenticationRequiredError(Exception):
    """Raised when TikTok requires login or audience confirmation."""
    pass


class MediaNotFoundError(Exception):
    """Raised when a TikTok post was removed or cannot be found."""
    pass


class ExtractionError(Exception):
    """Raised for unexpected extractor failures that must not be retried."""
    pass


class UpstreamUnavailableError(Exception):
    """Raised for temporary network failures that should retry on the same egress."""
    pass


class TikTokRequestError(Exception):
    """Attach request-stage context while retaining the original yt-dlp error."""

    def __init__(
        self,
        stage: str,
        cause: Exception,
        *,
        url: str | None = None,
        format_id: str | None = None,
    ):
        super().__init__(str(cause))
        self.stage = stage
        self.cause = cause
        self.host = urlsplit(url).hostname if url else None
        self.format_id = format_id
        status = re.search(
            r"(?:HTTP Error|status(?: code)?)[ :]*(403|404|429)\b",
            str(cause),
            re.I,
        )
        self.status_code = int(status.group(1)) if status else None


MEDIA_DOWNLOAD_STAGES = {
    "video_download",
    "photo_download",
    "audio_download",
    "thumbnail",
}


def _request_error(
    stage: str,
    error: Exception,
    *,
    url: str | None = None,
    format_id: str | None = None,
) -> TikTokRequestError:
    if isinstance(error, TikTokRequestError):
        return error
    return TikTokRequestError(stage, error, url=url, format_id=format_id)


def _error_stage(error: Exception) -> str:
    return error.stage if isinstance(error, TikTokRequestError) else "unknown"


def _log_request_failure(error: Exception, *, attempt: int | None = None) -> None:
    context = error if isinstance(error, TikTokRequestError) else None
    logger.warning(
        "TikTok request failed stage=%s host=%s status=%s format_id=%s attempt=%s error=%s",
        context.stage if context else "unknown",
        context.host if context else None,
        context.status_code if context else None,
        context.format_id if context else None,
        attempt,
        context.cause if context else error,
    )


def _without_watermarked_formats(info: dict) -> dict:
    formats = info.get("formats")
    if not isinstance(formats, list):
        raise ExtractionError("TikTok did not provide a watermark-free video format")

    clean_formats = [
        format_info
        for format_info in formats
        if "watermark" not in str(format_info.get("format_note") or "").lower()
    ]
    if not clean_formats:
        raise ExtractionError("TikTok did not provide a watermark-free video format")

    if len(clean_formats) != len(formats):
        logger.info(
            "Discarded %d watermarked TikTok format(s)",
            len(formats) - len(clean_formats),
        )
    return {**info, "formats": clean_formats}


async def trigger_rotation():
    """Reconnect the VPN tunnel after TikTok blocks the current session."""
    logger.warning("Reconnecting VPN tunnel...")
    try:
        await gluetun.rotate_ip(wait_for_cooldown=True)
    except Exception as e:
        logger.warning(f"VPN reconnect failed: {e}")
        raise RateLimitError(str(e))


def _classify_extraction_error(error: Exception) -> Exception:
    request_error = error if isinstance(error, TikTokRequestError) else None
    source_error = request_error.cause if request_error else error
    if isinstance(
        source_error,
        (
            AuthenticationRequiredError,
            MediaNotFoundError,
            RateLimitError,
            ExtractionError,
            UpstreamUnavailableError,
        ),
    ):
        return source_error

    message = str(source_error)
    lowered = message.lower()
    if (
        request_error
        and request_error.stage in MEDIA_DOWNLOAD_STAGES
        and (
            request_error.status_code in (403, 404, 429)
            or "forbidden" in lowered
        )
    ):
        return RateLimitError(message)
    if any(
        marker in lowered
        for marker in (
            "log in for access",
            "login for access",
            "requiring login",
            "login required",
            "cookies-from-browser",
            "cookies for the authentication",
            "private video",
            "private post",
            "private account",
            "permission to view this post",
        )
    ):
        return AuthenticationRequiredError(message)
    if any(
        marker in lowered
        for marker in (
            "not found",
            "has been removed",
            "was removed",
            "video unavailable",
            "post unavailable",
            "does not exist",
        )
    ):
        return MediaNotFoundError(message)
    if any(
        marker in lowered
        for marker in (
            "http error 403",
            "http error 429",
            "status code 403",
            "status code 429",
            "too many requests",
            "rate limit",
            "ip address is blocked",
            "verify you are human",
            "unable to solve js challenge",
        )
    ):
        return RateLimitError(message)
    if any(
        marker in lowered
        for marker in (
            "timed out",
            "timeout",
            "connection reset",
            "connection refused",
            "connection aborted",
            "temporary failure",
            "name or service not known",
            "network is unreachable",
        )
    ):
        return UpstreamUnavailableError(message)
    return ExtractionError(message)


def _needs_oembed_probe(error: Exception) -> bool:
    source_error = error.cause if isinstance(error, TikTokRequestError) else error
    lowered = str(source_error).lower()
    return any(
        marker in lowered
        for marker in (
            "unable to extract universal data for rehydration",
            "unexpected response from webpage request",
            "unable to extract challenge data",
            "video not available, status code",
        )
    )


def _probe_oembed_sync(url: str) -> str:
    """Return available, not_found, blocked, or unknown for an official probe."""
    try:
        with httpx.Client(
            proxy=proxy_url(),
            timeout=10.0,
            follow_redirects=True,
        ) as client:
            response = client.get("https://www.tiktok.com/oembed", params={"url": url})
        if response.status_code == 200 and isinstance(response.json(), dict):
            return "available"
        if response.status_code in (404, 410):
            return "not_found"
        if response.status_code in (403, 429):
            return "blocked"
    except (httpx.HTTPError, ValueError):
        pass
    return "unknown"


async def _probe_oembed(url: str) -> str:
    return await UPSTREAM_RUNNER.run(_probe_oembed_sync, url)


def _vpn_enabled() -> bool:
    return (
        not proxy_url()
        and os.getenv("VPN_ENABLED", "true").lower() in ("1", "true", "yes")
    )


def _retry_delay_seconds() -> float:
    return max(0.0, float(os.getenv("TIKTOK_RETRY_DELAY_SECONDS", "2.0")))


def _format_attempts() -> int:
    return max(1, int(os.getenv("TIKTOK_FORMAT_ATTEMPTS", "3")))


def _http_error(
    status_code: int,
    code: str,
    message: str,
    *,
    stage: str | None = None,
) -> HTTPException:
    detail = {"code": code, "message": message}
    if stage and stage != "unknown":
        detail["stage"] = stage
    return HTTPException(status_code=status_code, detail=detail)


def extract_video_id(url: str) -> str:
    match = re.search(r"/(?:video|photo)/(\d+)", str(url))
    if match:
        return match.group(1)
    return url


def canonicalize_tiktok_url(url: str) -> str:
    """Strip tracking data and produce a stable canonical TikTok post URL."""
    parsed = urlsplit(str(url))
    hostname = (parsed.hostname or "").lower()
    if hostname != "tiktok.com" and not hostname.endswith(".tiktok.com"):
        return str(url)

    canonical = re.search(
        r"/@(?P<user>[\w.-]+)/(?P<kind>video|photo)/(?P<id>\d+)",
        parsed.path,
    )
    if canonical:
        return (
            f"https://www.tiktok.com/@{canonical.group('user')}/"
            f"{canonical.group('kind')}/{canonical.group('id')}"
        )

    share = re.fullmatch(r"/share/video/(?P<id>\d+)", parsed.path)
    if share:
        return f"https://www.tiktok.com/share/video/{share.group('id')}"

    legacy = re.search(r"^/(?P<kind>v|video|photo)/(?P<id>\d+)", parsed.path)
    if legacy:
        kind = "photo" if legacy.group("kind") == "photo" else "video"
        return f"https://www.tiktok.com/@_/{kind}/{legacy.group('id')}"
    return str(url)


def _ordered_video_formats(info: dict, ydl: YoutubeDL) -> list[dict]:
    sortable_info = {**info, "formats": [dict(item) for item in info.get("formats") or []]}
    ydl.sort_formats(sortable_info)
    best_first = list(reversed(sortable_info["formats"]))

    groups = (
        lambda item: not item.get("__needs_testing") and item.get("ext") == "mp4",
        lambda item: not item.get("__needs_testing") and item.get("ext") != "mp4",
        lambda item: item.get("__needs_testing") and item.get("ext") == "mp4",
        lambda item: item.get("__needs_testing") and item.get("ext") != "mp4",
    )
    ordered: list[dict] = []
    seen: set[tuple[object, object]] = set()
    for group in groups:
        for format_info in best_first:
            key = (format_info.get("format_id"), format_info.get("url"))
            if not format_info.get("url") or key in seen or not group(format_info):
                continue
            seen.add(key)
            ordered.append(format_info)
    attempt_limit = _format_attempts()
    hd_formats = [item for item in ordered if item.get("__hd_refresh")]
    player_formats = [item for item in ordered if item.get("__player_api")]
    if hd_formats and player_formats and attempt_limit > 1:
        # Always reserve the final attempt for the independent anonymous
        # player endpoint.  HD redirect failures must not remove the reliable
        # 540p fallback that motivated this extraction path.
        selected = [*hd_formats[:attempt_limit - 1], player_formats[0]]
        return selected[:attempt_limit]
    return ordered[:attempt_limit]


def _retryable_format_error(error: Exception) -> bool:
    context = error if isinstance(error, TikTokRequestError) else None
    lowered = str(context.cause if context else error).lower()
    return (
        (context and context.status_code in (403, 404, 429))
        or "http error 403" in lowered
        or "http error 404" in lowered
        or "http error 429" in lowered
        or "forbidden" in lowered
    )


def _transcode_hevc_enabled() -> bool:
    return os.getenv("TIKTOK_TRANSCODE_HEVC", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def _transcode_hevc_for_sharing(source: Path) -> Path:
    destination = source.with_name(f"{source.stem}.h264{source.suffix}")
    destination.unlink(missing_ok=True)
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-tag:v",
            "avc1",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(destination),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        logger.warning("TikTok HEVC compatibility conversion skipped: ffmpeg unavailable")
        return source
    try:
        _, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=max(
                1.0,
                float(os.getenv("TIKTOK_HEVC_TRANSCODE_TIMEOUT_SECONDS", "180")),
            ),
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        destination.unlink(missing_ok=True)
        logger.warning("TikTok HEVC compatibility conversion timed out")
        return source
    if process.returncode != 0 or not destination.is_file() or not destination.stat().st_size:
        destination.unlink(missing_ok=True)
        logger.warning(
            "TikTok HEVC compatibility conversion failed: %s",
            stderr.decode(errors="replace").strip(),
        )
        return source
    destination.replace(source)
    logger.info("Converted TikTok HEVC video to share-compatible H.264 MP4")
    return source


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
        first_download_error = None
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
            try:
                await _download_with_ydl_bounded(
                    scraper,
                    image_info,
                    _download_options(image_outtmpl),
                )
            except Exception as e:
                download_error = True
                image_error = _request_error(
                    "photo_download",
                    e,
                    url=image_entries[0].get("url"),
                )
                first_download_error = first_download_error or image_error
                _log_request_failure(image_error)

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
            for stale_audio in post_dir.glob("audio.*"):
                if stale_audio.is_file():
                    stale_audio.unlink()
            audio_outtmpl = str(post_dir / "audio.%(ext)s")
            try:
                await _download_with_ydl_bounded(
                    scraper,
                    audio_entry,
                    _download_options(
                        audio_outtmpl,
                        fmt="bestaudio/best",
                        noplaylist=True,
                    ),
                    extract_audio_mp3=True,
                )
            except Exception as e:
                download_error = True
                audio_format = (audio_entry.get("formats") or [{}])[0]
                audio_error = _request_error(
                    "audio_download",
                    e,
                    url=audio_format.get("url"),
                    format_id=audio_format.get("format_id"),
                )
                first_download_error = first_download_error or audio_error
                _log_request_failure(audio_error)

        audio_file = post_dir / "audio.mp3"
        if not audio_file.exists():
            audio_file = _find_downloaded_file(post_dir, "audio")
        if audio_file:
            audio_ext = audio_file.suffix.lstrip(".")
            audio_url = f"/media/tiktok/{video_id}/audio.{audio_ext}"

        media_type = "carousel"

        if download_error and not carousel_items and not audio_url:
            raise first_download_error or ExtractionError("TikTok media download failed")

    else:
        video_info = _without_watermarked_formats(info)
        for stale_video in post_dir.glob("video.*"):
            if stale_video.is_file():
                stale_video.unlink()
        video_outtmpl = str(post_dir / "video.%(ext)s")
        format_errors: list[TikTokRequestError] = []
        selected_vcodec = None
        for format_index, format_info in enumerate(
            _ordered_video_formats(video_info, scraper._ydl),
            start=1,
        ):
            candidate_info = copy.deepcopy(video_info)
            candidate_info["formats"] = [copy.deepcopy(format_info)]
            try:
                download_result = await _download_with_ydl_bounded(
                    scraper,
                    candidate_info,
                    _download_options(video_outtmpl, fmt="best", noplaylist=True),
                )
                logger.info(
                    "Selected watermark-free TikTok format id=%s codec=%s resolution=%sx%s attempt=%d",
                    download_result.get("format_id"),
                    download_result.get("vcodec"),
                    download_result.get("width"),
                    download_result.get("height"),
                    format_index,
                )
                selected_vcodec = download_result.get("vcodec") or format_info.get("vcodec")
                break
            except Exception as e:
                format_error = _request_error(
                    "video_download",
                    e,
                    url=format_info.get("url"),
                    format_id=format_info.get("format_id"),
                )
                format_errors.append(format_error)
                _log_request_failure(format_error, attempt=format_index)
                for incomplete_video in post_dir.glob("video.*"):
                    if incomplete_video.is_file():
                        incomplete_video.unlink()
                if not _retryable_format_error(format_error):
                    break

        video_file = _find_downloaded_file(post_dir, "video")
        if not video_file:
            if format_errors:
                raise format_errors[-1]
            raise ExtractionError("TikTok did not provide a downloadable watermark-free format")
        if (
            _transcode_hevc_enabled()
            and str(selected_vcodec or "").lower() in {"h265", "hevc", "hvc1"}
        ):
            video_file = await _transcode_hevc_for_sharing(video_file)

        thumb_outtmpl = {
            "default": str(post_dir / "video.%(ext)s"),
            "thumbnail": str(post_dir / "thumbnail.%(ext)s"),
        }
        try:
            await _download_with_ydl_bounded(
                scraper,
                video_info,
                _download_options(
                    thumb_outtmpl,
                    write_thumbnail=True,
                    skip_download=True,
                    fmt="best",
                    noplaylist=True,
                ),
            )
        except Exception as e:
            thumbnail_error = _request_error(
                "thumbnail",
                e,
                url=info.get("thumbnail"),
            )
            _log_request_failure(thumbnail_error)

        video_ext = video_file.suffix.lstrip(".")
        video_url = f"/media/tiktok/{video_id}/video.{video_ext}"

        thumb_file = _find_downloaded_file(post_dir, "thumbnail")
        if thumb_file:
            thumb_ext = thumb_file.suffix.lstrip(".")
            thumbnail_url = f"/media/tiktok/{video_id}/thumbnail.{thumb_ext}"

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
    if media_type == "video":
        metadata["_tiktok_video_cache_version"] = TIKTOK_VIDEO_CACHE_VERSION
    storage.save_metadata(video_id, metadata)
    return response


async def _process_scrape_request(request: ScrapeRequest):
    url = canonicalize_tiktok_url(str(request.url))
    video_id = None
    same_egress_retry_used = False
    media_refresh_used = False
    vpn_reconnect_used = False
    transport_retry_used = False

    for attempt in range(1, TIKTOK_MAX_ATTEMPTS + 1):
        scraper = TikTokScraper()
        try:
            if (
                "vm.tiktok.com" in url
                or "vt.tiktok.com" in url
                or re.search(r"v[a-z]\.tiktok\.com", url)
                or "/t/" in url
            ):
                short_url = url
                try:
                    url = await UPSTREAM_RUNNER.run(scraper.resolve_short_url, url)
                except Exception as e:
                    raise _request_error("short_url", e, url=short_url) from e
                url = canonicalize_tiktok_url(url)

            if video_id is None:
                video_id = extract_video_id(url)

            if storage.is_cached(video_id):
                cached = storage.load_metadata(video_id)
                if cached and _cached_media_ready(cached):
                    logger.info("Cache hit for %s", video_id)
                    return ScrapeResponse(**cached)
                logger.info("Cache invalid for %s, missing media; re-scraping", video_id)

            logger.info(f"Scraping TikTok: {video_id} (attempt {attempt})")
            try:
                info = await UPSTREAM_RUNNER.run(scraper.extract_video, url)
            except Exception as e:
                raise _request_error("webpage", e, url=url) from e
            return await _download_and_build_response(video_id, info, scraper)
        except HTTPException:
            raise
        except Exception as e:
            stage = _error_stage(e)
            _log_request_failure(e, attempt=attempt)
            classified = _classify_extraction_error(e)
            if isinstance(classified, ExtractionError) and _needs_oembed_probe(e):
                probe_result = await _probe_oembed(url)
                logger.info("TikTok oEmbed diagnostic for %s: %s", video_id, probe_result)
                if probe_result == "not_found":
                    classified = MediaNotFoundError(str(e))
                elif probe_result in ("available", "blocked"):
                    classified = RateLimitError(str(e))
            if isinstance(classified, AuthenticationRequiredError):
                logger.info("TikTok post %s requires anonymous access confirmation", video_id)
                raise _http_error(
                    403,
                    "authentication_required",
                    "This TikTok post requires login or audience confirmation",
                    stage=stage,
                ) from e
            if isinstance(classified, MediaNotFoundError):
                raise _http_error(
                    404,
                    "not_found",
                    "TikTok post not found",
                    stage=stage,
                ) from e
            if isinstance(classified, RateLimitError):
                logger.warning(
                    "TikTok attempt %d was blocked for %s at stage=%s",
                    attempt,
                    video_id,
                    stage,
                )
                if stage in MEDIA_DOWNLOAD_STAGES and not media_refresh_used:
                    media_refresh_used = True
                    delay = _retry_delay_seconds()
                    logger.info(
                        "Refreshing TikTok media URLs once on the current egress after %.1fs.",
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                challenge_failure = _needs_oembed_probe(e)
                if challenge_failure and not same_egress_retry_used:
                    same_egress_retry_used = True
                    tiktok_session_cache.clear()
                    delay = _retry_delay_seconds()
                    logger.info(
                        "Cleared TikTok session cache; retrying once on the current "
                        "egress after %.1fs.",
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                if not vpn_reconnect_used and _vpn_enabled():
                    vpn_reconnect_used = True
                    tiktok_session_cache.clear()
                    logger.info("Cleared TikTok session cache before VPN reconnect.")
                    try:
                        await trigger_rotation()
                    except RateLimitError as rotation_error:
                        logger.warning("TikTok VPN reconnect failed: %s", rotation_error)
                        raise _http_error(
                            503,
                            "rate_limited",
                            "TikTok is temporarily rate limited",
                            stage=stage,
                        ) from rotation_error
                    continue
                raise _http_error(
                    503,
                    "rate_limited",
                    "TikTok is temporarily rate limited",
                    stage=stage,
                ) from e
            if isinstance(classified, UpstreamUnavailableError):
                logger.warning(
                    "TikTok attempt %d had a temporary upstream failure for %s: %s",
                    attempt,
                    video_id,
                    e,
                )
                if not transport_retry_used:
                    transport_retry_used = True
                    await asyncio.sleep(max(0.5, request_interval_seconds()))
                    continue
                raise _http_error(
                    503,
                    "upstream_unavailable",
                    "TikTok is temporarily unavailable",
                    stage=stage,
                ) from e

            logger.exception("TikTok extraction failed for %s: %s", video_id, e)
            raise _http_error(
                502,
                "extraction_failed",
                "TikTok extraction failed",
                stage=stage,
            ) from e

    raise RuntimeError("unreachable")


@router.post("/scrape", response_model=ScrapeResponse)
async def process_scrape_request(request: ScrapeRequest):
    video_id = extract_video_id(str(request.url))
    return await storage.singleflight(video_id, lambda: _process_scrape_request(request))


@router.get("/health")
async def health_check():
    try:
        status = await gluetun.get_vpn_status()
        vpn_status = status.get("status", "").lower()
        if gluetun.enabled and vpn_status != "running":
            raise HTTPException(status_code=503, detail=f"VPN not running: {vpn_status}")
        egress_mode = (
            "proxy"
            if proxy_url()
            else "vpn_namespace"
            if gluetun.enabled
            else "direct"
        )
        return {
            "status": "healthy",
            "service": "tiktok",
            "yt_dlp_version": YTDLP_VERSION,
            "vpn": status,
            "egress_mode": egress_mode,
        }
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

@asynccontextmanager
async def lifespan(_: FastAPI):
    """Release the shared storage client when the standalone app stops."""
    try:
        yield
    finally:
        await storage.close()


# Standalone FastAPI app for container mode
app = FastAPI(title="Pinchana TikTok", version="0.1.0", lifespan=lifespan)
app.include_router(router)
