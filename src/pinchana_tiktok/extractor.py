"""Small Pinchana extensions for yt-dlp's maintained TikTok extractors.

The upstream extractor owns TikTok's web request, challenge-cookie, format, and
metadata behavior.  This module deliberately contains only the Pinchana-specific
URL and photo-post support that yt-dlp does not expose as ordered image assets.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from yt_dlp.extractor.tiktok import (
    TikTokIE as UpstreamTikTokIE,
    TikTokLiveIE,
    TikTokUserIE,
    TikTokVMIE,
)
from yt_dlp.utils import (
    ExtractorError,
    int_or_none,
    mimetype2ext,
    truncate_string,
    url_or_none,
)
from yt_dlp.utils.traversal import traverse_obj


class TikTokIE(UpstreamTikTokIE):
    """Anonymous web-only TikTok extractor with Pinchana photo-post support."""

    def _parse_frontity_video_data(self, webpage: str, display_id: str) -> dict | None:
        """Map Embed V2's Frontity payload to yt-dlp's web item shape.

        Embed V2 is used only for photo posts.  Its regular-video URL is a
        watermarked download and is intentionally never exposed as a format.
        """
        frontity = self._search_json(
            r'<script[^>]+\bid="__FRONTITY_CONNECT_STATE__"[^>]*>',
            webpage,
            "frontity state",
            display_id,
            end_pattern=r"</script>",
            default={},
            fatal=False,
        )
        source_data = traverse_obj(frontity, ("source", "data", {dict})) or {}
        for value in source_data.values():
            video_data = value.get("videoData") if isinstance(value, dict) else None
            if not isinstance(video_data, dict):
                continue

            item = video_data.get("itemInfos") or {}
            author = video_data.get("authorInfos") or {}
            music = video_data.get("musicInfos") or {}
            image_post = video_data.get("imagePostInfo") or {}
            raw_images = image_post.get("displayImages") or image_post.get("images") or []
            images = []
            for image in raw_images:
                if not isinstance(image, dict):
                    continue
                urls = (
                    image.get("urlList")
                    or image.get("url_list")
                    or traverse_obj(image, ("displayImage", "imageURL", "urlList"))
                    or traverse_obj(image, ("imageURL", "urlList"))
                    or []
                )
                if urls:
                    images.append({
                        "imageURL": {"urlList": urls},
                        "imageWidth": image.get("width"),
                        "imageHeight": image.get("height"),
                    })

            if not images:
                continue

            covers = item.get("covers") or []
            play_url = music.get("playUrl")
            if isinstance(play_url, list) and play_url:
                first_url = play_url[0]
                play_url = first_url.get("url") if isinstance(first_url, dict) else str(first_url)

            author_info = {
                "id": author.get("userId") or author.get("id") or "",
                "authorId": author.get("userId") or author.get("id") or "",
                "uniqueId": author.get("uniqueId") or author.get("secUid") or "",
                "nickname": author.get("nickName") or author.get("nickname") or "",
                "secUid": author.get("secUid") or "",
                "authorSecId": author.get("secUid") or "",
            }
            item_struct = {
                "id": item.get("id") or display_id,
                "desc": item.get("text") or "",
                "createTime": item.get("createTime"),
                "author": author_info,
                "authorInfo": author_info,
                "video": {
                    "id": item.get("id") or display_id,
                    "duration": (item.get("video") or {}).get("duration"),
                    "cover": covers[0] if covers else "",
                    "dynamicCover": covers[1] if len(covers) > 1 else "",
                    "originCover": covers[-1] if covers else "",
                },
                "imagePost": {"images": images},
                "music": {
                    "id": music.get("musicId"),
                    "title": music.get("musicName"),
                    "authorName": music.get("authorName"),
                    "playUrl": play_url,
                    "coverThumb": music.get("coverThumb"),
                    "duration": music.get("duration"),
                },
                "stats": {
                    "diggCount": item.get("diggCount", 0),
                    "shareCount": item.get("shareCount", 0),
                    "commentCount": item.get("commentCount", 0),
                    "playCount": item.get("playCount", 0),
                },
            }
            return {
                "webapp.video-detail": {
                    "statusCode": 0,
                    "itemInfo": {"itemStruct": item_struct},
                }
            }
        return None

    def _get_universal_data(self, webpage: str, display_id: str) -> dict:
        upstream_data = super()._get_universal_data(webpage, display_id)
        return upstream_data or self._parse_frontity_video_data(webpage, display_id) or {}

    def _extract_embed_photo(self, video_id: str) -> tuple[dict, int] | None:
        embed_page = self._download_webpage(
            f"https://www.tiktok.com/embed/v2/{video_id}",
            video_id,
            note="Downloading Embed V2 photo fallback",
            fatal=False,
            impersonate=True,
        )
        universal_data = (
            self._parse_frontity_video_data(embed_page, video_id)
            if embed_page
            else None
        )
        video_data = traverse_obj(
            universal_data,
            ("webapp.video-detail", "itemInfo", "itemStruct", {dict}),
        )
        if video_data:
            return video_data, 0
        return None

    def _extract_web_data_and_status(self, url, video_id, fatal=True):
        try:
            video_data, status = super()._extract_web_data_and_status(
                url, video_id, fatal=fatal
            )
        except ExtractorError as upstream_error:
            fallback = self._extract_embed_photo(video_id)
            if fallback:
                return fallback
            raise upstream_error

        if video_data or status != -1:
            return video_data, status
        fallback = self._extract_embed_photo(video_id)
        if fallback:
            return fallback
        return video_data, status

    def _parse_aweme_video_web(self, aweme_detail, webpage_url, video_id, extract_flat=False):
        image_post = traverse_obj(aweme_detail, ("imagePost", {dict}))
        if not image_post:
            return super()._parse_aweme_video_web(
                aweme_detail, webpage_url, video_id, extract_flat=extract_flat
            )

        base_info = super()._parse_aweme_video_web(
            aweme_detail, webpage_url, video_id, extract_flat=True
        )
        author_info = {
            key: base_info.get(key)
            for key in (
                "channel",
                "channel_id",
                "channel_url",
                "uploader",
                "uploader_id",
                "uploader_url",
            )
            if base_info.get(key) is not None
        }
        title = traverse_obj(aweme_detail, ("desc", {str})) or video_id
        entries = []
        for index, image_url in enumerate(
            traverse_obj(image_post, ("images", ..., "imageURL", "urlList", 0, {url_or_none}))
        ):
            entries.append({
                "id": f"{video_id}_{index}",
                "url": self._proto_relative_url(image_url),
                "title": title,
                "ext": "jpg",
                "http_headers": {"Referer": webpage_url},
                **author_info,
            })

        audio_url = traverse_obj(aweme_detail, ("music", "playUrl", {url_or_none}))
        if audio_url:
            mime_type = (parse_qs(urlparse(audio_url).query).get("mime_type") or [None])[-1]
            ext = mimetype2ext(mime_type.replace("_", "/")) if mime_type else None
            ext = ext or "m4a"
            entries.append({
                "id": f"{video_id}_audio",
                "title": title,
                "formats": [{
                    "format_id": "audio",
                    "url": self._proto_relative_url(audio_url),
                    "ext": ext,
                    "acodec": "aac" if ext == "m4a" else ext,
                    "vcodec": "none",
                }],
                "http_headers": {"Referer": webpage_url},
                **author_info,
                **traverse_obj(aweme_detail, ("music", {
                    "track": ("title", {str}),
                    "album": ("album", {str}, filter),
                    "artists": (
                        "authorName",
                        {str},
                        {lambda value: re.split(r"(?:, | & )", value) if value else None},
                    ),
                    "duration": ("duration", {int_or_none}),
                })),
            })

        if not entries:
            raise ExtractorError("TikTok photo post did not contain downloadable media")

        result = self.playlist_result(
            entries,
            video_id,
            traverse_obj(aweme_detail, ("desc", {truncate_string(left=72)})),
        )
        result.update({
            key: value
            for key, value in base_info.items()
            if key not in {"id", "formats", "subtitles", "_type", "entries"}
            and value is not None
        })
        return result

__all__ = ["TikTokIE", "TikTokLiveIE", "TikTokUserIE", "TikTokVMIE"]
