"""Clean Python API for TikTok scraping."""

from __future__ import annotations

import copy
import os
import re
import threading
import time
from http.cookiejar import Cookie
from typing import Iterator

from yt_dlp import YoutubeDL

from .extractor import TikTokIE, TikTokLiveIE, TikTokUserIE, TikTokVMIE


DEFAULT_YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
}


def request_interval_seconds() -> float:
    return max(0.0, float(os.getenv("TIKTOK_REQUEST_INTERVAL_SECONDS", "2.0")))


def proxy_url() -> str | None:
    return os.getenv("TIKTOK_PROXY_URL", "").strip() or None


def transport_ydl_opts() -> dict:
    """Options shared by extraction and every media download request."""
    options = {"sleep_interval_requests": request_interval_seconds()}
    if configured_proxy := proxy_url():
        options["proxy"] = configured_proxy
    return options


class TikTokSessionCache:
    """Reuse TikTok's short-lived anonymous cookies between scrapes."""

    def __init__(self):
        self._lock = threading.RLock()
        self._cookies: dict[tuple[str, str, str], Cookie] = {}

    def restore(self, ydl: YoutubeDL) -> None:
        now = time.time()
        with self._lock:
            expired = [
                key for key, cookie in self._cookies.items()
                if cookie.is_expired(now)
            ]
            for key in expired:
                self._cookies.pop(key, None)
            cookies = [copy.copy(cookie) for cookie in self._cookies.values()]
        for cookie in cookies:
            ydl.cookiejar.set_cookie(cookie)

    def capture(self, ydl: YoutubeDL) -> None:
        now = time.time()
        with self._lock:
            for cookie in ydl.cookiejar:
                key = (cookie.domain, cookie.path, cookie.name)
                if cookie.is_expired(now):
                    self._cookies.pop(key, None)
                else:
                    self._cookies[key] = copy.copy(cookie)

    def clear(self) -> None:
        """Drop anonymous cookies tied to an old egress IP."""
        with self._lock:
            self._cookies.clear()


tiktok_session_cache = TikTokSessionCache()


class TikTokScraper:
    """High-level wrapper around yt-dlp's TikTok extractors."""

    def __init__(
        self,
        *,
        session_cache: TikTokSessionCache = tiktok_session_cache,
        **ydl_opts,
    ):
        extractor_args = {
            key: dict(value)
            for key, value in ydl_opts.pop("extractor_args", {}).items()
        }
        # Pinchana's public scraper is intentionally anonymous and web-only.
        # Do not let internal callers accidentally opt into the mobile API.
        tiktok_args = extractor_args.setdefault("tiktok", {})
        tiktok_args.pop("device_id", None)
        tiktok_args.pop("app_info", None)
        self._session_cache = session_cache
        self._ydl_opts = {
            **DEFAULT_YDL_OPTS,
            **transport_ydl_opts(),
            "extractor_args": extractor_args,
            **ydl_opts,
        }
        self._ydl = YoutubeDL(self._ydl_opts)
        self._session_cache.restore(self._ydl)

    def extract_video(self, url: str) -> dict:
        """Extract metadata for a single TikTok video or photo post.

        Args:
            url: TikTok video URL (e.g. ``https://www.tiktok.com/@user/video/123``).

        Returns:
            Info dict containing formats, thumbnails, metadata, etc.
            For photo slideshows the dict will have ``_type: "playlist"``
            with image entries.
        """
        # Upstream TikTokIE owns URL routing, including /share/video URLs.  A
        # photo post is served by the same canonical video endpoint, so only
        # normalize that Pinchana-specific spelling before handing it over.
        extractor_url = re.sub(r"(/@[\w.-]*/|/share/)photo/", r"\1video/", url)
        ie = TikTokIE(self._ydl)
        try:
            info = ie.extract(extractor_url)
        finally:
            self._session_cache.capture(self._ydl)
        # yt-dlp expects these fields when the info dict is later passed
        # to a fresh YoutubeDL instance for downloading.
        info.setdefault('extractor', ie.IE_NAME)
        info.setdefault('extractor_key', ie.ie_key())
        info.setdefault('webpage_url', url)
        if info.get('_type') in ('playlist', 'multi_video'):
            for entry in info.get('entries') or []:
                if isinstance(entry, dict):
                    entry.setdefault('extractor', ie.IE_NAME)
                    entry.setdefault('extractor_key', ie.ie_key())
                    entry.setdefault('webpage_url', entry.get('url') or url)
        return info

    def extract_user(self, url: str) -> dict:
        """Extract a user's video list as a playlist.

        Args:
            url: TikTok user URL (e.g. ``https://www.tiktok.com/@user``).

        Returns:
            Playlist dict with ``entries`` as a generator of video info dicts.
        """
        ie = TikTokUserIE(self._ydl)
        try:
            return ie.extract(url)
        finally:
            self._session_cache.capture(self._ydl)

    def extract_user_videos(self, url: str) -> Iterator[dict]:
        """Convenience helper that yields each video dict for a user.

        Args:
            url: TikTok user URL.

        Yields:
            Individual video/photo info dicts.
        """
        playlist = self.extract_user(url)
        yield from playlist.get("entries", [])

    def extract_live(self, url: str) -> dict:
        """Extract metadata for a TikTok livestream.

        Args:
            url: Live URL (e.g. ``https://www.tiktok.com/@user/live``).

        Returns:
            Info dict with HLS/FLV formats and live status.
        """
        ie = TikTokLiveIE(self._ydl)
        try:
            return ie.extract(url)
        finally:
            self._session_cache.capture(self._ydl)

    def resolve_short_url(self, url: str) -> str:
        """Expand a short TikTok URL (vm.tiktok.com, vt.tiktok.com, etc.).

        Args:
            url: Short TikTok URL.

        Returns:
            Canonical TikTok URL.
        """
        ie = TikTokVMIE(self._ydl)
        try:
            result = ie.extract(url)
        finally:
            self._session_cache.capture(self._ydl)
        return result.get("url", url)


# Module-level convenience functions -----------------------------------------


def extract_video(url: str, **ydl_opts) -> dict:
    """One-shot video extraction."""
    return TikTokScraper(**ydl_opts).extract_video(url)


def extract_user(url: str, **ydl_opts) -> dict:
    """One-shot user playlist extraction."""
    return TikTokScraper(**ydl_opts).extract_user(url)


def extract_user_videos(url: str, **ydl_opts) -> Iterator[dict]:
    """One-shot user video generator."""
    yield from TikTokScraper(**ydl_opts).extract_user_videos(url)


def extract_live(url: str, **ydl_opts) -> dict:
    """One-shot livestream extraction."""
    return TikTokScraper(**ydl_opts).extract_live(url)


def resolve_short_url(url: str, **ydl_opts) -> str:
    """One-shot short URL resolution."""
    return TikTokScraper(**ydl_opts).resolve_short_url(url)
