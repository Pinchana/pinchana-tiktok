"""TikTok media classification and normalization helpers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse, parse_qs


IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "heic"}
VIDEO_EXTS = {"mp4", "mov", "m4v"}
AUDIO_EXTS = {"m4a", "mp3", "aac"}

LIVE_MARKER_KEYS = (
    "live",
    "live_photo",
    "livephoto",
    "motion",
    "paired",
    "paired_video",
    "clip",
    "animated",
)
VIDEO_CONTAINER_KEYS = (
    "video",
    "play_addr",
    "playaddr",
    "download_addr",
    "downloadaddr",
    "play_url",
    "playurl",
    "url_list",
    "urllist",
)


def _raw_sources(info: dict[str, Any]) -> list[dict[str, Any]]:
    sources = []
    for key in ("__raw_aweme", "__raw_web"):
        value = info.get(key)
        if isinstance(value, dict):
            sources.append(value)
    sources.append(info)
    return sources


def _first_url(value: Any) -> str | None:
    if isinstance(value, str) and _is_http_url(value):
        return value
    if isinstance(value, list):
        for item in value:
            url = _first_url(item)
            if url:
                return url
    if isinstance(value, dict):
        for key in ("url", "src", "uri"):
            url = _first_url(value.get(key))
            if url:
                return url
        for key in ("urlList", "url_list", "UrlList"):
            url = _first_url(value.get(key))
            if url:
                return url
    return None


def _is_http_url(value: str) -> bool:
    return value.startswith(("http://", "https://", "//"))


def _normalize_url(value: str) -> str:
    return f"https:{value}" if value.startswith("//") else value


def _url_ext(value: str) -> str:
    path = urlparse(value).path
    if "." not in path:
        return ""
    return path.rsplit(".", 1)[-1].lower()


def _url_mime(value: str) -> str:
    query = parse_qs(urlparse(value).query)
    return (query.get("mime_type") or query.get("mime") or [""])[-1].lower().replace("_", "/")


def _looks_like_image_url(value: str) -> bool:
    ext = _url_ext(value)
    mime = _url_mime(value)
    return ext in IMAGE_EXTS or mime.startswith("image/")


def _looks_like_video_url(value: str) -> bool:
    ext = _url_ext(value)
    mime = _url_mime(value)
    return ext in VIDEO_EXTS or mime.startswith("video/")


def _looks_like_audio_url(value: str) -> bool:
    ext = _url_ext(value)
    mime = _url_mime(value)
    return ext in AUDIO_EXTS or mime.startswith("audio/")


def _dedupe(values: Iterable[str | None]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        if not value:
            continue
        value = _normalize_url(value)
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _walk(value: Any, path: tuple[str, ...] = ()):
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, (*path, str(key)))
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            yield from _walk(child, (*path, str(idx)))


def _path_has_marker(path: tuple[str, ...]) -> bool:
    lowered = ".".join(path).replace("-", "_").lower()
    return any(marker in lowered for marker in LIVE_MARKER_KEYS)


def _path_has_video_container(path: tuple[str, ...]) -> bool:
    lowered = ".".join(path).replace("-", "_").lower()
    return any(key in lowered for key in VIDEO_CONTAINER_KEYS)


def has_live_photo_marker(raw_or_info: dict[str, Any]) -> bool:
    for path, value in _walk(raw_or_info):
        if _path_has_marker(path):
            if value not in (None, "", [], {}, False):
                return True
    return False


def find_still_image_urls(raw_or_info: dict[str, Any]) -> list[str]:
    urls: list[str | None] = []

    image_post = raw_or_info.get("imagePost") or raw_or_info.get("image_post")
    if isinstance(image_post, dict):
        images = image_post.get("images")
        if isinstance(images, list):
            for image in images:
                if not isinstance(image, dict):
                    continue
                urls.append(_first_url(image.get("imageURL") or image.get("image_url")))

    origin_image = raw_or_info.get("origin_image")
    if isinstance(origin_image, dict):
        urls.append(_first_url(origin_image))

    for key in ("cover", "originCover", "origin_cover", "thumbnail"):
        urls.append(_first_url(raw_or_info.get(key)))

    video = raw_or_info.get("video")
    if isinstance(video, dict):
        for key in ("cover", "originCover", "origin_cover", "thumbnail"):
            urls.append(_first_url(video.get(key)))

    if raw_or_info.get("_type") == "playlist":
        for entry in raw_or_info.get("entries") or []:
            if not isinstance(entry, dict) or entry.get("formats"):
                continue
            url = entry.get("url")
            ext = (entry.get("ext") or _url_ext(str(url))).lower()
            if isinstance(url, str) and ext not in AUDIO_EXTS:
                urls.append(url)

    for thumb in raw_or_info.get("thumbnails") or []:
        if isinstance(thumb, dict):
            urls.append(thumb.get("url"))

    return _dedupe(url for url in urls if isinstance(url, str) and (_looks_like_image_url(url) or _is_http_url(url)))


def find_motion_video_url(raw_or_info: dict[str, Any]) -> str | None:
    for path, value in _walk(raw_or_info):
        if not (_path_has_marker(path) or _path_has_video_container(path)):
            continue
        url = _first_url(value)
        if url and _looks_like_video_url(url):
            return _normalize_url(url)
        if isinstance(value, str) and _is_http_url(value) and _looks_like_video_url(value):
            return _normalize_url(value)
    return None


def find_audio_url(raw_or_info: dict[str, Any]) -> str | None:
    for path, value in _walk(raw_or_info):
        lowered = ".".join(path).replace("-", "_").lower()
        if "music" not in lowered and "audio" not in lowered:
            continue
        url = _first_url(value)
        if url and (_looks_like_audio_url(url) or "mime_type=audio" in url):
            return _normalize_url(url)

    if raw_or_info.get("_type") == "playlist":
        for entry in raw_or_info.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            for fmt in entry.get("formats") or []:
                if isinstance(fmt, dict) and fmt.get("vcodec") == "none" and fmt.get("url"):
                    return _normalize_url(fmt["url"])
    return None


def _best_duration(info: dict[str, Any]) -> int | None:
    for source in _raw_sources(info):
        value = source.get("duration")
        if isinstance(value, int) and value > 0:
            return value
        video = source.get("video")
        if isinstance(video, dict):
            value = video.get("duration")
            if isinstance(value, int) and value > 0:
                return value
        music = source.get("music")
        if isinstance(music, dict):
            value = music.get("duration")
            if isinstance(value, int) and value > 0:
                return value
    return None


def _has_formats(info: dict[str, Any]) -> bool:
    return any(isinstance(fmt, dict) and fmt.get("url") for fmt in info.get("formats") or [])


def _has_image_post(source: dict[str, Any]) -> bool:
    image_post = source.get("imagePost") or source.get("image_post")
    return isinstance(image_post, dict) and bool(image_post.get("images"))


def classify_tiktok_media(info: dict[str, Any]) -> str:
    stills: list[str] = []
    motion_url = None
    marker = False

    for source in _raw_sources(info):
        stills.extend(find_still_image_urls(source))
        motion_url = motion_url or find_motion_video_url(source)
        marker = marker or has_live_photo_marker(source)

    stills = _dedupe(stills)
    if _has_formats(info) and not any(_has_image_post(source) for source in _raw_sources(info)):
        return "video"
    if stills and motion_url:
        return "live_photo"
    if stills and marker:
        return "live_photo"
    if len(stills) > 1:
        return "photo_carousel"
    if len(stills) == 1:
        return "photo"
    if _has_formats(info):
        return "video"
    return info.get("_type") or "unknown"


def normalize_tiktok_info(info: dict[str, Any]) -> dict[str, Any]:
    stills: list[str] = []
    motion_url = None
    audio_url = None
    marker = False

    for source in _raw_sources(info):
        stills.extend(find_still_image_urls(source))
        motion_url = motion_url or find_motion_video_url(source)
        audio_url = audio_url or find_audio_url(source)
        marker = marker or has_live_photo_marker(source)

    stills = _dedupe(stills)
    kind = classify_tiktok_media(info)
    normalized = {
        "kind": kind,
        "image_urls": stills,
        "image_url": stills[0] if stills else None,
        "video_url": motion_url,
        "audio_url": audio_url,
        "duration": _best_duration(info),
        "available": None,
        "reason": None,
    }

    if kind == "live_photo":
        normalized["available"] = bool(stills and motion_url)
        if stills and not motion_url:
            normalized["reason"] = "Motion asset not exposed in current TikTok responses"
        elif marker and not stills:
            normalized["available"] = False
            normalized["reason"] = "Still image not exposed in current TikTok responses"

    return normalized
