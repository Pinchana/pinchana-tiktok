"""Clean Python API for TikTok scraping."""

from __future__ import annotations

from typing import Iterator

from yt_dlp import YoutubeDL

from .extractor import TikTokIE, TikTokLiveIE, TikTokUserIE, TikTokVMIE


DEFAULT_YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
}


class TikTokScraper:
    """High-level wrapper around yt-dlp's TikTok extractors."""

    def __init__(self, **ydl_opts):
        self._ydl_opts = {**DEFAULT_YDL_OPTS, **ydl_opts}
        self._ydl = YoutubeDL(self._ydl_opts)

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
        return ie.extract(url)

    def extract_user(self, url: str) -> dict:
        """Extract a user's video list as a playlist.

        Args:
            url: TikTok user URL (e.g. ``https://www.tiktok.com/@user``).

        Returns:
            Playlist dict with ``entries`` as a generator of video info dicts.
        """
        ie = TikTokUserIE(self._ydl)
        return ie.extract(url)

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
        return ie.extract(url)

    def resolve_short_url(self, url: str) -> str:
        """Expand a short TikTok URL (vm.tiktok.com, vt.tiktok.com, etc.).

        Args:
            url: Short TikTok URL.

        Returns:
            Canonical TikTok URL.
        """
        ie = TikTokVMIE(self._ydl)
        result = ie.extract(url)
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
