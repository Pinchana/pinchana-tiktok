"""Clean Python API for TikTok scraping."""

from __future__ import annotations

import copy
import logging
import os
import re
import threading
import time
from http.cookiejar import Cookie
from typing import Iterator
from urllib.parse import urljoin, urlsplit

import httpx
from yt_dlp import YoutubeDL
from yt_dlp.utils import ExtractorError

from .extractor import TikTokIE, TikTokLiveIE, TikTokUserIE


DEFAULT_YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
}


TT_SITE_HOSTS = {"tt.site", "www.tt.site"}
_POST_HOSTS = {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}
_MAX_SHORT_URL_REQUESTS = 3
logger = logging.getLogger(__name__)


def _safe_web_url(url: str, *, https_only: bool = False) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme in ({"https"} if https_only else {"http", "https"})
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443 if parsed.scheme == "https" else 80)
        )
    except ValueError:
        return False


def is_short_tiktok_url(url: str) -> bool:
    """Recognize only known TikTok share hosts and short-link paths."""
    if not _safe_web_url(url):
        return False
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    if hostname in TT_SITE_HOSTS or hostname in _POST_HOSTS:
        return re.fullmatch(r"/t/[\w-]+/?", parsed.path) is not None
    short_host = hostname == "t.tiktok.com" or re.fullmatch(r"v[a-z]\.tiktok\.com", hostname)
    return bool(short_host and re.fullmatch(r"/[\w-]+/?", parsed.path))


def _is_tiktok_post_url(url: str) -> bool:
    if not _safe_web_url(url, https_only=True):
        return False
    parsed = urlsplit(url)
    return (
        (parsed.hostname or "").lower() in _POST_HOSTS
        and re.fullmatch(
            r"/(?:@[\w.-]+/(?:video|photo)|share/video|v|video|photo)/\d+/?",
            parsed.path,
        ) is not None
    )


def _short_request_url(url: str) -> str:
    parsed = urlsplit(url)
    # The apex tt.site may have no DNS record; keep the working www host.
    hostname = (parsed.hostname or "").lower()
    if hostname in TT_SITE_HOSTS:
        hostname = "www.tt.site"
    return parsed._replace(scheme="https", netloc=hostname, fragment="").geturl()


def _tt_site_fallback(url: str) -> str | None:
    parsed = urlsplit(url)
    if (parsed.hostname or "").lower() not in TT_SITE_HOSTS:
        return None
    # TikTok's legacy short host accepts the same share code. Do not fetch a
    # destination supplied by an error page or weaken redirect validation.
    code = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    return parsed._replace(netloc="vm.tiktok.com", path=f"/{code}/").geturl()


def request_interval_seconds() -> float:
    """Minimum spacing between independent TikTok jobs."""
    return max(0.0, float(os.getenv("TIKTOK_REQUEST_INTERVAL_SECONDS", "2.0")))


def internal_request_interval_seconds() -> float:
    """Optional delay between requests inside one extraction job."""
    return max(
        0.0,
        float(os.getenv("TIKTOK_INTERNAL_REQUEST_INTERVAL_SECONDS", "0")),
    )


def direct_web_primary_enabled() -> bool:
    return os.getenv("TIKTOK_DIRECT_WEB_PRIMARY", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def proxy_url() -> str | None:
    return os.getenv("TIKTOK_PROXY_URL", "").strip() or None


def transport_ydl_opts() -> dict:
    """Options shared by extraction and every media download request."""
    options = {"sleep_interval_requests": internal_request_interval_seconds()}
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
        # Pinchana's public scraper is intentionally anonymous.  It uses the
        # official player endpoint with the web extractor as fallback, never a
        # device identity tied to TikTok's private mobile API.
        tiktok_args = extractor_args.setdefault("tiktok", {})
        tiktok_args.pop("device_id", None)
        tiktok_args.pop("app_info", None)
        if direct_web_primary_enabled():
            tiktok_args["direct_web_primary"] = ["true"]
        self._direct_web_primary = "true" in {
            str(value).lower()
            for value in tiktok_args.get("direct_web_primary", [])
        }
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
        ie = TikTokIE(self._ydl)
        try:
            info = ie.extract(url)
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
        """Resolve known share URLs without fetching the destination post.

        Independent of the video extraction feature flag. Each redirect is
        checked before another request; arbitrary hosts and non-post targets
        are never followed.
        """
        if not is_short_tiktok_url(url):
            raise ExtractorError("Unsupported TikTok short URL", expected=True)

        current = _short_request_url(url)
        visited: set[str] = set()
        fallback_used = False
        with httpx.Client(
            follow_redirects=False,
            proxy=self._ydl.params.get("proxy"),
            timeout=20.0,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept-Language": "en-US,en;q=0.9",
            },
        ) as client:
            for request_number in range(_MAX_SHORT_URL_REQUESTS):
                if current in visited:
                    raise ExtractorError("TikTok short URL redirect loop", expected=True)
                visited.add(current)
                try:
                    response = client.get(current)
                    if not response.is_redirect:
                        response.raise_for_status()
                except httpx.InvalidURL as exc:
                    raise ExtractorError("Invalid TikTok short URL redirect", expected=True) from exc
                except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                    alternate = _tt_site_fallback(current)
                    if (
                        alternate
                        and not fallback_used
                        and (status is None or 500 <= status < 600)
                        and request_number + 1 < _MAX_SHORT_URL_REQUESTS
                    ):
                        logger.warning(
                            "TikTok short-link endpoint unavailable host=www.tt.site "
                            "status=%s; falling back to vm.tiktok.com", status,
                        )
                        fallback_used = True
                        current = alternate
                        continue
                    if isinstance(exc, httpx.HTTPStatusError):
                        # Preserve status metadata and the established error text
                        # used by the service's request-time retry classifier.
                        raise ExtractorError(
                            f"HTTP Error {status}: {exc.response.reason_phrase}",
                            cause=exc,
                            expected=True,
                        ) from exc
                    raise
                location = response.headers.get("location")
                if not response.is_redirect or not location:
                    raise ExtractorError(
                        "TikTok short URL did not redirect to a post", expected=True,
                    )
                try:
                    target = urljoin(current, location)
                except ValueError as exc:
                    raise ExtractorError("Invalid TikTok short URL redirect", expected=True) from exc
                if _is_tiktok_post_url(target):
                    return target
                if not _safe_web_url(target, https_only=True) or not is_short_tiktok_url(target):
                    raise ExtractorError("Invalid TikTok short URL redirect", expected=True)
                current = _short_request_url(target)

        raise ExtractorError("Too many TikTok short URL redirects", expected=True)


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
