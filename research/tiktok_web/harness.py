#!/usr/bin/env python3
"""Research harness for TikTok's logged-out first-party web flow.

This module is deliberately isolated from ``src/pinchana_tiktok``.  It can read a
HAR, fetch a public canonical page, parse SSR hydration, rank media renditions,
and make byte-range probes without persisting response bodies or signed URLs.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

from curl_cffi import requests
from curl_cffi.const import CurlHttpVersion


HYDRATION_SCRIPT_ID = "__UNIVERSAL_DATA_FOR_REHYDRATION__"
SHORT_HOSTS = {"vm.tiktok.com", "vt.tiktok.com"}
SAFE_REQUEST_HEADERS = {
    "accept",
    "accept-encoding",
    "accept-language",
    "priority",
    "referer",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
    "upgrade-insecure-requests",
    "user-agent",
}
SAFE_RESPONSE_HEADERS = {
    "cache-control",
    "content-encoding",
    "content-length",
    "content-type",
    "location",
    "server",
    "x-cache",
    "x-cache-remote",
}


class HydrationError(ValueError):
    """The response did not contain usable TikTok SSR hydration."""


class _HydrationScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._inside_target = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "script" and dict(attrs).get("id") == HYDRATION_SCRIPT_ID:
            self._inside_target = True

    def handle_data(self, data: str) -> None:
        if self._inside_target:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._inside_target:
            self._inside_target = False

    @property
    def payload(self) -> str:
        return "".join(self._parts)


def parse_hydration_html(html: str) -> dict[str, Any]:
    parser = _HydrationScriptParser()
    parser.feed(html)
    if not parser.payload:
        raise HydrationError(f"missing {HYDRATION_SCRIPT_ID} script")
    try:
        data = json.loads(parser.payload)
    except json.JSONDecodeError as error:
        raise HydrationError(f"invalid hydration JSON: {error}") from error
    if not isinstance(data, dict):
        raise HydrationError("hydration root is not an object")
    return data


def extract_item_struct(hydration: dict[str, Any]) -> tuple[dict[str, Any], int]:
    scope = hydration.get("__DEFAULT_SCOPE__")
    if not isinstance(scope, dict):
        raise HydrationError("missing __DEFAULT_SCOPE__")
    detail = scope.get("webapp.video-detail")
    if not isinstance(detail, dict):
        raise HydrationError("missing webapp.video-detail")
    status = detail.get("statusCode", 0)
    item_info = detail.get("itemInfo")
    item = item_info.get("itemStruct") if isinstance(item_info, dict) else None
    if not isinstance(item, dict):
        raise HydrationError(f"missing itemStruct (TikTok status {status!r})")
    return item, int(status) if isinstance(status, int | float) else 0


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def codec_name(value: Any) -> str | None:
    codec = str(value or "").lower()
    if any(marker in codec for marker in ("265", "bytevc", "hevc", "hvc1")):
        return "h265"
    if any(marker in codec for marker in ("264", "avc")):
        return "h264"
    return codec or None


def url_shape(url: str) -> str:
    """Keep routing evidence while removing signed path/query values."""
    parsed = urlsplit(url)
    if parsed.path.startswith("/aweme/v1/play/"):
        path = "/aweme/v1/play/"
    elif parsed.path.startswith("/video/"):
        path = "/video/<redacted>"
    elif re.fullmatch(r"/@[^/]+/(?:video|photo)/[^/]+", parsed.path):
        path = re.sub(r"/[^/]+$", "/<redacted>", parsed.path)
    elif parsed.hostname and any(marker in parsed.hostname for marker in ("tiktokcdn", "tiktokv")):
        path = "/<redacted>"
    else:
        segments = [segment for segment in parsed.path.split("/") if segment]
        path = "/" + "/".join(segments[:2])
        if len(segments) > 2:
            path += "/<redacted>"
    query_keys = sorted({key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)})
    query = "&".join(f"{key}=<redacted>" for key in query_keys)
    return urlunsplit((parsed.scheme, parsed.netloc, path, query, ""))


@dataclass(frozen=True)
class Rendition:
    profile_index: int
    gear_name: str
    width: int | None
    height: int | None
    codec: str | None
    bitrate_bps: int | None
    fps: int | None
    filesize_bytes: int | None
    urls: tuple[str, ...]

    @property
    def area(self) -> int:
        return (self.width or 0) * (self.height or 0)

    def safe_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["bitrate_kbps"] = round(self.bitrate_bps / 1000, 3) if self.bitrate_bps else None
        value.pop("bitrate_bps")
        value["url_shapes"] = [url_shape(url) for url in self.urls]
        value["url_count"] = len(self.urls)
        value.pop("urls")
        return value


@dataclass(frozen=True)
class ImageAsset:
    index: int
    width: int | None
    height: int | None
    urls: tuple[str, ...]

    def safe_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "width": self.width,
            "height": self.height,
            "url_count": len(self.urls),
            "url_shapes": [url_shape(url) for url in self.urls],
        }


def extract_renditions(item: dict[str, Any]) -> list[Rendition]:
    video = item.get("video")
    if not isinstance(video, dict):
        return []
    profiles = _first(video, "bitrateInfo", "bitRate") or []
    renditions: list[Rendition] = []
    for index, profile in enumerate(profiles):
        if not isinstance(profile, dict):
            continue
        address = _first(profile, "PlayAddr", "playAddr") or {}
        if not isinstance(address, dict):
            continue
        urls = _first(address, "UrlList", "urlList") or []
        valid_urls = tuple(url for url in urls if isinstance(url, str) and url.startswith("http"))
        if not valid_urls:
            continue
        renditions.append(Rendition(
            profile_index=index,
            gear_name=str(_first(profile, "GearName", "gearName") or f"quality-{index + 1}"),
            width=_integer(_first(address, "Width", "width")),
            height=_integer(_first(address, "Height", "height")),
            codec=codec_name(_first(profile, "CodecType", "codecType")),
            bitrate_bps=_integer(_first(profile, "Bitrate", "bitrate")),
            fps=_integer(_first(profile, "BitrateFPS", "bitrateFPS", "FPS", "fps")),
            filesize_bytes=_integer(_first(address, "DataSize", "dataSize")),
            urls=valid_urls,
        ))
    return renditions


def extract_image_assets(item: dict[str, Any]) -> list[ImageAsset]:
    image_post = _first(item, "imagePost", "imagePostInfo", "image_post_info") or {}
    if not isinstance(image_post, dict):
        return []
    assets: list[ImageAsset] = []
    for index, image in enumerate(image_post.get("images") or []):
        if not isinstance(image, dict):
            continue
        address = _first(image, "imageURL", "displayImage", "display_image") or {}
        if not isinstance(address, dict):
            continue
        urls = _first(address, "urlList", "UrlList", "url_list") or []
        valid_urls = tuple(url for url in urls if isinstance(url, str) and url.startswith("http"))
        if not valid_urls:
            continue
        assets.append(ImageAsset(
            index=index,
            width=_integer(_first(image, "imageWidth", "width") or _first(address, "Width", "width")),
            height=_integer(_first(image, "imageHeight", "height") or _first(address, "Height", "height")),
            urls=valid_urls,
        ))
    return assets


def rank_renditions(
    renditions: Iterable[Rendition],
    *,
    policy: str = "quality",
    codec: str = "any",
) -> list[Rendition]:
    candidates = [item for item in renditions if codec == "any" or item.codec == codec]
    if policy == "compatibility":
        key = lambda item: (
            1 if item.codec == "h264" else 0,
            item.area,
            item.bitrate_bps or 0,
            item.filesize_bytes or 0,
        )
    elif policy == "quality":
        key = lambda item: (
            item.area,
            item.bitrate_bps or 0,
            item.filesize_bytes or 0,
            1 if item.codec == "h264" else 0,
        )
    else:
        raise ValueError(f"unknown ranking policy: {policy}")
    return sorted(candidates, key=key, reverse=True)


def classify_item(item: dict[str, Any]) -> str:
    image_post = _first(item, "imagePost", "imagePostInfo")
    if isinstance(image_post, dict) and image_post.get("images"):
        return "photo"
    if isinstance(item.get("video"), dict):
        return "video"
    return "unknown"


def _headers(headers: list[dict[str, Any]], allowlist: set[str]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for header in headers:
        name = str(header.get("name", ""))
        lowered = name.lower()
        if lowered not in allowlist:
            continue
        value = str(header.get("value", ""))
        safe[name] = url_shape(value) if lowered in {"location", "referer"} else value
    return safe


def _response_text(entry: dict[str, Any]) -> str:
    content = entry.get("response", {}).get("content", {})
    text = content.get("text")
    if not isinstance(text, str):
        raise HydrationError("HAR document body is unavailable")
    if content.get("encoding") == "base64":
        return base64.b64decode(text).decode("utf-8", errors="replace")
    return text


def _started_ms(entry: dict[str, Any]) -> float:
    value = str(entry.get("startedDateTime"))
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000


def summarize_har(path: Path, *, video_id: str | None = None) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("log", {}).get("entries", [])
    documents = [
        entry for entry in entries
        if entry.get("request", {}).get("method") == "GET"
        and (
            entry.get("_resourceType") == "document"
            or str(entry.get("request", {}).get("headersSize", "")) == "-1"
            or urlsplit(entry.get("request", {}).get("url", "")).hostname in SHORT_HOSTS
        )
        and (
            urlsplit(entry.get("request", {}).get("url", "")).hostname in SHORT_HOSTS
            or (
                video_id
                and re.fullmatch(
                    rf"/@[^/]+/(?:video|photo)/{re.escape(video_id)}",
                    urlsplit(entry.get("request", {}).get("url", "")).path,
                )
            )
        )
    ]
    result: dict[str, Any] = {
        "capture": {"entry_count": len(entries), "page_count": len(data.get("log", {}).get("pages", []))},
        "documents": [],
    }
    for entry in documents:
        request = entry.get("request", {})
        response = entry.get("response", {})
        timings = entry.get("timings", {})
        result["documents"].append({
            "started": entry.get("startedDateTime"),
            "method": request.get("method"),
            "url": url_shape(request.get("url", "")),
            "status": response.get("status"),
            "redirect": url_shape(response.get("redirectURL", "")) if response.get("redirectURL") else None,
            "har_time_ms": entry.get("time"),
            "timings_ms": {key: timings.get(key) for key in ("blocked", "dns", "connect", "ssl", "send", "wait", "receive")},
            "server_ip": entry.get("serverIPAddress"),
            "http_version": response.get("httpVersion"),
            "request_header_names": sorted({str(header.get("name", "")).lower() for header in request.get("headers", [])}),
            "cookie_names": sorted({str(cookie.get("name", "")) for cookie in request.get("cookies", [])}),
            "safe_request_headers": _headers(request.get("headers", []), SAFE_REQUEST_HEADERS),
            "safe_response_headers": _headers(response.get("headers", []), SAFE_RESPONSE_HEADERS),
            "response_body_bytes": response.get("content", {}).get("size"),
        })
    if len(documents) >= 2:
        result["redirect_to_document_start_ms"] = round(_started_ms(documents[1]) - _started_ms(documents[0]), 3)

    canonical_entry = next(
        (entry for entry in documents if entry.get("response", {}).get("status") == 200 and video_id and video_id in entry.get("request", {}).get("url", "")),
        None,
    )
    if canonical_entry:
        hydration = parse_hydration_html(_response_text(canonical_entry))
        scope = hydration.get("__DEFAULT_SCOPE__")
        try:
            item, status = extract_item_struct(hydration)
        except HydrationError as error:
            result["hydration"] = {
                "usable": False,
                "error": str(error),
                "available_scopes": sorted(scope) if isinstance(scope, dict) else [],
            }
        else:
            renditions = rank_renditions(extract_renditions(item))
            result["hydration"] = {
                "usable": True,
                "path": "__DEFAULT_SCOPE__.webapp.video-detail.itemInfo.itemStruct",
                "status": status,
                "item_id": str(item.get("id", "")),
                "item_type": classify_item(item),
                "top_level_keys": sorted(item),
                "video_keys": sorted(item.get("video", {})) if isinstance(item.get("video"), dict) else [],
                "renditions": [item.safe_dict() for item in renditions],
            }

    detail_entry = next(
        (
            entry for entry in entries
            if urlsplit(entry.get("request", {}).get("url", "")).path == "/api/item/detail/"
            and entry.get("response", {}).get("status") == 200
        ),
        None,
    )
    if detail_entry:
        try:
            payload = json.loads(_response_text(detail_entry))
        except (HydrationError, json.JSONDecodeError):
            payload = {}
        detail_item = (
            payload.get("itemInfo", {}).get("itemStruct")
            if isinstance(payload.get("itemInfo"), dict)
            else None
        )
        request_url = detail_entry.get("request", {}).get("url", "")
        detail_summary: dict[str, Any] = {
            "path": "/api/item/detail/",
            "status": detail_entry.get("response", {}).get("status"),
            "http_version": detail_entry.get("response", {}).get("httpVersion"),
            "har_time_ms": detail_entry.get("time"),
            "response_body_bytes": detail_entry.get("response", {}).get("content", {}).get("size"),
            "query_names": sorted({key for key, _ in parse_qsl(urlsplit(request_url).query, keep_blank_values=True)}),
            "request_header_names": sorted({
                str(header.get("name", "")).lower()
                for header in detail_entry.get("request", {}).get("headers", [])
            }),
            "cookie_names": sorted({
                str(cookie.get("name", ""))
                for cookie in detail_entry.get("request", {}).get("cookies", [])
            }),
        }
        if canonical_entry:
            detail_summary["start_after_canonical_document_ms"] = round(
                _started_ms(detail_entry) - _started_ms(canonical_entry),
                3,
            )
        if isinstance(detail_item, dict):
            images = extract_image_assets(detail_item)
            music = detail_item.get("music") if isinstance(detail_item.get("music"), dict) else {}
            detail_summary.update({
                "payload_path": "itemInfo.itemStruct",
                "api_status": payload.get("statusCode", payload.get("status_code")),
                "item_id": str(detail_item.get("id", "")),
                "item_type": classify_item(detail_item),
                "image_count": len(images),
                "images": [image.safe_dict() for image in images],
                "music": {
                    "has_play_url": isinstance(_first(music, "playUrl", "play_url"), str),
                    "duration_seconds": _integer(music.get("duration")),
                },
            })
        result["item_detail_api"] = detail_summary
    return result


def make_session(*, timeout: float, proxy: str | None = None) -> requests.Session:
    return requests.Session(
        impersonate="chrome",
        timeout=timeout,
        proxy=proxy,
        headers={"Accept-Language": "en-US,en;q=0.9"},
    )


def http_version_name(value: Any) -> str | None:
    try:
        version = CurlHttpVersion(value)
    except (TypeError, ValueError):
        return str(value) if value is not None else None
    return {
        CurlHttpVersion.V1_0: "HTTP/1.0",
        CurlHttpVersion.V1_1: "HTTP/1.1",
        CurlHttpVersion.V2_0: "HTTP/2",
        CurlHttpVersion.V3: "HTTP/3",
    }.get(version, version.name)


def resolve_short_url(session: requests.Session, url: str) -> tuple[str, dict[str, Any]]:
    start = time.perf_counter()
    response = session.get(url, allow_redirects=False)
    elapsed_ms = (time.perf_counter() - start) * 1000
    location = response.headers.get("location")
    if response.status_code not in range(300, 400) or not location:
        raise RuntimeError(f"short URL returned HTTP {response.status_code} without Location")
    canonical = urljoin(url, location)
    return canonical, {
        "status": response.status_code,
        "elapsed_ms": round(elapsed_ms, 3),
        "source": url_shape(url),
        "location": url_shape(canonical),
    }


def fetch_hydration(
    session: requests.Session,
    url: str,
    *,
    ranking_policy: str = "quality",
    codec: str = "any",
) -> tuple[dict[str, Any], list[Rendition], dict[str, Any]]:
    start = time.perf_counter()
    response = session.get(url, allow_redirects=True)
    elapsed_ms = (time.perf_counter() - start) * 1000
    response.raise_for_status()
    hydration = parse_hydration_html(response.text)
    try:
        item, status = extract_item_struct(hydration)
    except HydrationError as error:
        scopes = hydration.get("__DEFAULT_SCOPE__")
        available = sorted(scopes) if isinstance(scopes, dict) else []
        raise HydrationError(f"{error}; available scopes: {available}") from error
    renditions = rank_renditions(extract_renditions(item), policy=ranking_policy, codec=codec)
    summary = {
        "status": response.status_code,
        "elapsed_ms": round(elapsed_ms, 3),
        "http_version": http_version_name(getattr(response, "http_version", None)),
        "url": url_shape(response.url),
        "response_bytes": len(response.content),
        "tiktok_status": status,
        "item_id": str(item.get("id", "")),
        "item_type": classify_item(item),
        "renditions": [rendition.safe_dict() for rendition in renditions],
    }
    return item, renditions, summary


def probe_url(session: requests.Session, url: str, *, referer: str) -> dict[str, Any]:
    start = time.perf_counter()
    response = session.get(
        url,
        headers={"Range": "bytes=0-0", "Referer": referer},
        allow_redirects=True,
        stream=True,
    )
    try:
        first_chunk = next(response.iter_content(chunk_size=1), b"")
        elapsed_ms = (time.perf_counter() - start) * 1000
        return {
            "requested": url_shape(url),
            "redirect_chain": [
                {"status": previous.status_code, "url": url_shape(previous.url)}
                for previous in response.history
            ],
            "final_status": response.status_code,
            "final_url": url_shape(response.url),
            "elapsed_to_first_byte_ms": round(elapsed_ms, 3),
            "received_bytes": len(first_chunk),
            "content_type": response.headers.get("content-type"),
            "content_length": response.headers.get("content-length"),
            "content_range": response.headers.get("content-range"),
        }
    finally:
        response.close()


def choose_probe_urls(rendition: Rendition) -> list[str]:
    direct = next((url for url in rendition.urls if "/aweme/v1/play/" not in url), None)
    playback = next((url for url in rendition.urls if "/aweme/v1/play/" in url), None)
    return [url for url in (direct, playback) if url]


def live_flow(
    url: str,
    *,
    timeout: float,
    proxy: str | None,
    ranking_policy: str,
    codec: str,
    probe: bool,
) -> dict[str, Any]:
    session = make_session(timeout=timeout, proxy=proxy)
    result: dict[str, Any] = {"request_count_before_probes": 1}
    canonical = url
    if urlsplit(url).hostname in SHORT_HOSTS:
        canonical, redirect = resolve_short_url(session, url)
        result["redirect"] = redirect
        result["request_count_before_probes"] += 1
    _item, renditions, fetch = fetch_hydration(
        session,
        canonical,
        ranking_policy=ranking_policy,
        codec=codec,
    )
    result["canonical_fetch"] = fetch
    if probe and renditions:
        result["probes"] = [probe_url(session, media_url, referer=canonical) for media_url in choose_probe_urls(renditions[0])]
    return result


def current_extractor_flow(url: str, *, interval: float) -> dict[str, Any]:
    """Run the existing extractor with request counting and no media download."""
    from pinchana_tiktok.api import TikTokScraper, TikTokSessionCache

    previous_interval = os.environ.get("TIKTOK_REQUEST_INTERVAL_SECONDS")
    os.environ["TIKTOK_REQUEST_INTERVAL_SECONDS"] = str(interval)
    try:
        scraper = TikTokScraper(session_cache=TikTokSessionCache())
    finally:
        if previous_interval is None:
            os.environ.pop("TIKTOK_REQUEST_INTERVAL_SECONDS", None)
        else:
            os.environ["TIKTOK_REQUEST_INTERVAL_SECONDS"] = previous_interval

    original_urlopen = scraper._ydl.urlopen
    requests_seen: list[dict[str, Any]] = []

    def counted_urlopen(request):
        request_url = getattr(request, "url", None) or getattr(request, "full_url", "")
        start = time.perf_counter()
        try:
            response = original_urlopen(request)
        except Exception as error:
            requests_seen.append({
                "url": url_shape(str(request_url)),
                "elapsed_ms": round((time.perf_counter() - start) * 1000, 3),
                "error": type(error).__name__,
            })
            raise
        requests_seen.append({
            "url": url_shape(str(request_url)),
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 3),
            "status": getattr(response, "status", None),
        })
        return response

    scraper._ydl.urlopen = counted_urlopen
    canonical = url
    start = time.perf_counter()
    if urlsplit(url).hostname in SHORT_HOSTS:
        canonical = scraper.resolve_short_url(url)
    info = scraper.extract_video(canonical)
    elapsed_ms = (time.perf_counter() - start) * 1000
    formats = info.get("formats") if isinstance(info.get("formats"), list) else []
    entries = info.get("entries") if isinstance(info.get("entries"), list) else []
    return {
        "configured_interval_seconds": interval,
        "elapsed_ms": round(elapsed_ms, 3),
        "request_count": len(requests_seen),
        "requests": requests_seen,
        "item_id": str(info.get("id", "")),
        "item_type": "photo" if info.get("_type") in {"playlist", "multi_video"} else "video",
        "format_count": len(formats),
        "format_heights": sorted({value for item in formats if (value := _integer(item.get("height")))}, reverse=True),
        "entry_count": len(entries),
        "photo_entry_count": sum(1 for item in entries if isinstance(item, dict) and item.get("url") and not item.get("formats")),
        "audio_entry_count": sum(1 for item in entries if isinstance(item, dict) and item.get("vcodec") == "none"),
    }


def direct_photo_flow(url: str, *, timeout: float, proxy: str | None) -> dict[str, Any]:
    """Resolve only the first short redirect, then use the existing player surface."""
    from yt_dlp import YoutubeDL
    from pinchana_tiktok.extractor import TikTokIE

    session = make_session(timeout=timeout, proxy=proxy)
    canonical = url
    result: dict[str, Any] = {"request_count": 1}
    start = time.perf_counter()
    if urlsplit(url).hostname in SHORT_HOSTS:
        canonical, redirect = resolve_short_url(session, url)
        result["redirect"] = redirect
        result["request_count"] += 1
    match = re.search(r"/(?:video|photo)/(\d+)", urlsplit(canonical).path)
    if not match:
        raise RuntimeError("canonical Location did not contain a TikTok post ID")
    post_id = match.group(1)
    ie = TikTokIE(YoutubeDL({
        "quiet": True,
        "no_warnings": True,
        "sleep_interval_requests": 0,
        **({"proxy": proxy} if proxy else {}),
    }))
    player_start = time.perf_counter()
    info = ie._extract_player_api(post_id, canonical)
    player_ms = (time.perf_counter() - player_start) * 1000
    if not isinstance(info, dict) or info.get("_type") not in {"playlist", "multi_video"}:
        raise RuntimeError("player API did not return a photo playlist")
    entries = info.get("entries") if isinstance(info.get("entries"), list) else []
    result.update({
        "elapsed_ms": round((time.perf_counter() - start) * 1000, 3),
        "player_api_ms": round(player_ms, 3),
        "item_id": str(info.get("id", post_id)),
        "photo_entry_count": sum(
            1 for item in entries
            if isinstance(item, dict) and item.get("url") and not item.get("formats")
        ),
        "audio_entry_count": sum(
            1 for item in entries
            if isinstance(item, dict) and item.get("vcodec") == "none"
        ),
        "image_dimensions": [
            {"width": _integer(item.get("width")), "height": _integer(item.get("height"))}
            for item in entries
            if isinstance(item, dict) and item.get("url") and not item.get("formats")
        ],
    })
    return result


def pacing_floor(*, interval: float, short_request_seconds: float, metadata_requests: int) -> dict[str, float]:
    """Model only the two independently configured pacing layers."""
    ytdlp_delay = max(0, metadata_requests - 1) * interval
    runner_delay = max(0.0, interval - short_request_seconds) if metadata_requests > 1 else 0.0
    return {
        "yt_dlp_internal_seconds": round(ytdlp_delay, 3),
        "runner_between_resolve_and_extract_seconds": round(runner_delay, 3),
        "combined_seconds": round(ytdlp_delay + runner_delay, 3),
    }


def _json_dump(value: Any) -> None:
    json.dump(value, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    har = subparsers.add_parser("har", help="summarize a HAR without printing secrets")
    har.add_argument("path", type=Path)
    har.add_argument("--video-id", required=True)

    live = subparsers.add_parser("live", help="run the direct first-party flow")
    live.add_argument("url")
    live.add_argument("--timeout", type=float, default=20.0)
    live.add_argument("--proxy", default=os.getenv("TIKTOK_PROXY_URL") or None)
    live.add_argument("--ranking-policy", choices=("quality", "compatibility"), default="quality")
    live.add_argument("--codec", choices=("any", "h264", "h265"), default="any")
    live.add_argument("--probe", action="store_true")

    current = subparsers.add_parser("current", help="benchmark the existing extractor without downloading media")
    current.add_argument("url")
    current.add_argument("--interval", type=float, default=0.0)

    photo = subparsers.add_parser("photo", help="resolve only Location, then use player API for a photo post")
    photo.add_argument("url")
    photo.add_argument("--timeout", type=float, default=20.0)
    photo.add_argument("--proxy", default=os.getenv("TIKTOK_PROXY_URL") or None)

    pacing = subparsers.add_parser("pacing", help="model the current runner plus yt-dlp delay floor")
    pacing.add_argument("--interval", type=float, default=2.0)
    pacing.add_argument("--short-request-seconds", type=float, required=True)
    pacing.add_argument("--metadata-requests", type=int, default=3)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "har":
        _json_dump(summarize_har(args.path, video_id=args.video_id))
        return 0
    if args.command == "live":
        try:
            result = live_flow(
                args.url,
                timeout=args.timeout,
                proxy=args.proxy,
                ranking_policy=args.ranking_policy,
                codec=args.codec,
                probe=args.probe,
            )
        except HydrationError as error:
            _json_dump({"ok": False, "error": "hydration_unusable", "detail": str(error)})
            return 2
        _json_dump(result)
        return 0
    if args.command == "current":
        _json_dump(current_extractor_flow(args.url, interval=max(0.0, args.interval)))
        return 0
    if args.command == "photo":
        _json_dump(direct_photo_flow(args.url, timeout=args.timeout, proxy=args.proxy))
        return 0
    if args.command == "pacing":
        _json_dump(pacing_floor(
            interval=max(0.0, args.interval),
            short_request_seconds=max(0.0, args.short_request_seconds),
            metadata_requests=max(1, args.metadata_requests),
        ))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
