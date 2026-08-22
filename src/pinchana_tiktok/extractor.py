"""Small Pinchana extensions for yt-dlp's maintained TikTok extractors.

The upstream extractor owns TikTok's web request, challenge-cookie, format, and
metadata behavior.  This module deliberately contains only the Pinchana-specific
URL and photo-post support that yt-dlp does not expose as ordered image assets.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse, urlsplit, urlunsplit

from yt_dlp.extractor.tiktok import (
    TikTokIE as UpstreamTikTokIE,
    TikTokLiveIE,
    TikTokUserIE,
    TikTokVMIE,
)
from yt_dlp.utils import (
    determine_ext,
    ExtractorError,
    float_or_none,
    int_or_none,
    mimetype2ext,
    truncate_string,
    url_or_none,
)
from yt_dlp.utils.traversal import traverse_obj


class TikTokIE(UpstreamTikTokIE):
    """Anonymous TikTok player extractor with challenged-web fallback."""

    _PLAYER_API_URL = "https://www.tiktok.com/player/api/v1/items"
    _HD_PLAYBACK_HOSTS = (
        "api16-normal-no1a.tiktokv.eu",
        "api16-normal-c-useast1a.tiktokv.com",
        "api22-normal-c-useast1a.tiktokv.com",
    )

    @staticmethod
    def _timestamp_from_id(video_id: str) -> int | None:
        """Decode TikTok's creation timestamp from its Snowflake-style ID."""
        try:
            timestamp = int(video_id) >> 32
        except (TypeError, ValueError):
            return None
        return timestamp if 1_400_000_000 < timestamp < 4_102_444_800 else None

    @staticmethod
    def _preferred_image_url(urls) -> str | None:
        valid_urls = [url for url in urls or [] if url_or_none(url)]
        return next(
            (url for url in valid_urls if determine_ext(url) in {"jpg", "jpeg"}),
            valid_urls[0] if valid_urls else None,
        )

    def _player_metadata(self, item: dict, video_id: str, webpage_url: str) -> dict:
        author = item.get("author_info") or {}
        stats = item.get("statistics_info") or {}
        video = item.get("video_info") or {}
        video_meta = video.get("meta") or {}
        uploader_id = traverse_obj(author, ("unique_id", {str}))
        uploader_url = (
            f"https://www.tiktok.com/@{uploader_id}" if uploader_id else None
        )
        thumbnail = self._preferred_image_url(
            traverse_obj(video, ("cover", "url_list", {list}))
            or traverse_obj(video, ("origin_cover", "url_list", {list}))
        )
        description = traverse_obj(item, ("desc", {str})) or ""
        return {
            "id": traverse_obj(item, ("id_str", {str})) or video_id,
            "title": description or video_id,
            "description": description,
            "duration": float_or_none(video_meta.get("duration"), scale=1000),
            "timestamp": self._timestamp_from_id(video_id),
            "thumbnail": thumbnail,
            "uploader": traverse_obj(author, ("nickname", {str})),
            "uploader_id": uploader_id,
            "uploader_url": uploader_url,
            "channel": traverse_obj(author, ("nickname", {str})),
            "channel_id": traverse_obj(author, ("secret_id", {str})),
            "channel_url": uploader_url,
            "like_count": int_or_none(stats.get("digg_count")),
            "comment_count": int_or_none(stats.get("comment_count")),
            "repost_count": int_or_none(stats.get("share_count")),
            "webpage_url": webpage_url,
        }

    def _parse_player_video(
        self,
        item: dict,
        video_id: str,
        webpage_url: str,
    ) -> dict | None:
        video = item.get("video_info") or {}
        meta = video.get("meta") or {}
        formats = []
        profiles = video.get("profiles") or []
        if not profiles and video.get("url_list"):
            profiles = [{
                "gear_name": "default",
                "bitrate": meta.get("bitrate"),
                "play_addr": {
                    "width": meta.get("width"),
                    "height": meta.get("height"),
                    "url_list": video["url_list"],
                },
            }]
        for profile_index, profile in enumerate(profiles):
            if not isinstance(profile, dict):
                continue
            play_addr = profile.get("play_addr") or {}
            urls = [
                url for url in play_addr.get("url_list") or []
                if url_or_none(url)
            ]
            format_name = (
                profile.get("gear_name")
                or play_addr.get("url_key")
                or f"quality-{profile_index + 1}"
            )
            for mirror_index, media_url in enumerate(urls):
                formats.append({
                    "format_id": f"player-{format_name}-{mirror_index + 1}",
                    "format_note": "Original TikTok player API",
                    "url": self._proto_relative_url(media_url),
                    "ext": "mp4",
                    "width": int_or_none(play_addr.get("width") or meta.get("width")),
                    "height": int_or_none(play_addr.get("height") or meta.get("height")),
                    "fps": int_or_none(profile.get("fps")),
                    "tbr": float_or_none(profile.get("bitrate"), scale=1000),
                    "filesize": int_or_none(play_addr.get("data_size")),
                    "vcodec": traverse_obj(profile, ("codec_type", {str})),
                    "source_preference": len(urls) - mirror_index,
                    "http_headers": {"Referer": webpage_url},
                    "__player_api": True,
                })

        if not formats:
            return None
        return {
            **self._player_metadata(item, video_id, webpage_url),
            "formats": formats,
        }

    @staticmethod
    def _codec_name(value) -> str | None:
        codec = str(value or "").lower()
        if "265" in codec or "bytevc" in codec or "hevc" in codec:
            return "h265"
        if "264" in codec or "avc" in codec:
            return "h264"
        return codec or None

    def _parse_web_hd_formats(
        self,
        web_data: dict,
        webpage_url: str,
        minimum_height: int,
    ) -> list[dict]:
        """Turn TikTok's signed HD playback redirects into fresh CDN mirrors."""
        video = web_data.get("video") or {}
        bitrate_info = video.get("bitrateInfo") or video.get("bitRate") or []
        formats = []
        for profile_index, profile in enumerate(bitrate_info):
            if not isinstance(profile, dict):
                continue
            play_addr = profile.get("PlayAddr") or profile.get("playAddr") or {}
            height = int_or_none(play_addr.get("Height") or play_addr.get("height"))
            if not height or height <= minimum_height:
                continue
            playback_url = next(
                (
                    url
                    for url in play_addr.get("UrlList") or play_addr.get("urlList") or []
                    if url_or_none(url) and "/aweme/v1/play/" in url
                ),
                None,
            )
            if not playback_url:
                continue
            parsed_url = urlsplit(playback_url)
            format_name = (
                profile.get("GearName")
                or profile.get("gearName")
                or play_addr.get("UrlKey")
                or play_addr.get("urlKey")
                or f"quality-{profile_index + 1}"
            )
            for mirror_index, host in enumerate(self._HD_PLAYBACK_HOSTS):
                formats.append({
                    "format_id": f"hd-{format_name}-{mirror_index + 1}",
                    "format_note": "Original TikTok HD playback",
                    "url": urlunsplit((
                        parsed_url.scheme,
                        host,
                        parsed_url.path,
                        parsed_url.query,
                        parsed_url.fragment,
                    )),
                    "ext": "mp4",
                    "width": int_or_none(
                        play_addr.get("Width") or play_addr.get("width")
                    ),
                    "height": height,
                    "fps": int_or_none(
                        profile.get("BitrateFPS") or profile.get("bitrateFPS")
                    ),
                    "tbr": float_or_none(
                        profile.get("Bitrate") or profile.get("bitrate"),
                        scale=1000,
                    ),
                    "filesize": int_or_none(
                        play_addr.get("DataSize") or play_addr.get("dataSize")
                    ),
                    "vcodec": self._codec_name(
                        profile.get("CodecType") or profile.get("codecType")
                    ),
                    "acodec": "aac",
                    "source_preference": len(self._HD_PLAYBACK_HOSTS) - mirror_index,
                    "http_headers": {"Referer": webpage_url},
                    "__hd_refresh": True,
                })
        return formats

    def _extract_web_hd_formats(
        self,
        url: str,
        video_id: str,
        minimum_height: int,
    ) -> list[dict]:
        try:
            web_data, status = self._extract_web_data_and_status(
                url,
                video_id,
                fatal=False,
            )
        except ExtractorError as error:
            self.report_warning(f"Unable to discover TikTok HD rendition: {error}")
            return []
        if not web_data or status != 0:
            return []
        return self._parse_web_hd_formats(web_data, url, minimum_height)

    def _parse_player_photo(
        self,
        item: dict,
        video_id: str,
        webpage_url: str,
    ) -> dict | None:
        image_post = item.get("image_post_info") or {}
        metadata = self._player_metadata(item, video_id, webpage_url)
        entries = []
        for index, image in enumerate(image_post.get("images") or []):
            if not isinstance(image, dict):
                continue
            display_image = image.get("display_image") or {}
            image_url = self._preferred_image_url(display_image.get("url_list"))
            if not image_url:
                continue
            ext = determine_ext(image_url, "jpg")
            entries.append({
                "id": f"{video_id}_{index}",
                "url": self._proto_relative_url(image_url),
                "title": metadata["title"],
                "ext": "jpg" if ext == "jpeg" else ext,
                "width": int_or_none(display_image.get("width")),
                "height": int_or_none(display_image.get("height")),
                "http_headers": {"Referer": webpage_url},
                **{
                    key: metadata[key]
                    for key in ("uploader", "uploader_id", "uploader_url")
                    if metadata.get(key) is not None
                },
            })

        music = item.get("music_info") or {}
        audio_formats = []
        audio_urls = traverse_obj(item, ("video_info", "url_list", {list})) or []
        for mirror_index, media_url in enumerate(audio_urls):
            if not url_or_none(media_url):
                continue
            mime_type = (
                parse_qs(urlparse(media_url).query).get("mime_type") or [None]
            )[-1]
            ext = mimetype2ext(mime_type.replace("_", "/")) if mime_type else None
            audio_formats.append({
                "format_id": f"player-audio-{mirror_index + 1}",
                "url": self._proto_relative_url(media_url),
                "ext": ext or "mp3",
                "acodec": "mp3" if (ext or "mp3") == "mp3" else "aac",
                "vcodec": "none",
                "source_preference": len(audio_urls) - mirror_index,
                "http_headers": {"Referer": webpage_url},
            })
        if audio_formats:
            entries.append({
                "id": f"{video_id}_audio",
                "title": metadata["title"],
                "formats": audio_formats,
                "vcodec": "none",
                "track": traverse_obj(music, ("title", {str})),
                "artists": traverse_obj(
                    music,
                    ("author", {str}, {lambda value: [value]}),
                ),
                "http_headers": {"Referer": webpage_url},
            })

        if not entries:
            return None
        result = self.playlist_result(
            entries,
            video_id,
            truncate_string(metadata["title"], 72),
        )
        result.update({
            key: value
            for key, value in metadata.items()
            if key not in {"id", "formats", "_type", "entries"}
            and value is not None
        })
        return result

    def _extract_player_api(self, video_id: str, webpage_url: str) -> dict | None:
        """Use TikTok's anonymous player endpoint before its challenged web page."""
        response = self._download_json(
            self._PLAYER_API_URL,
            video_id,
            note="Downloading TikTok player metadata",
            query={"item_ids": video_id},
            headers={"Referer": "https://www.tiktok.com/"},
            fatal=False,
        )
        if not isinstance(response, dict) or int_or_none(response.get("status_code")) != 0:
            return None
        item = traverse_obj(response, ("items", 0, {dict}))
        if not item or str(item.get("id_str") or item.get("id")) != video_id:
            return None
        if item.get("image_post_info"):
            return self._parse_player_photo(item, video_id, webpage_url)
        return self._parse_player_video(item, video_id, webpage_url)

    def _real_extract(self, url):
        video_id, user_id = self._match_valid_url(url).group("id", "user_id")
        player_info = self._extract_player_api(video_id, url)
        if player_info:
            if player_info.get("formats"):
                player_height = max(
                    (
                        int_or_none(format_info.get("height")) or 0
                        for format_info in player_info["formats"]
                    ),
                    default=0,
                )
                canonical_url = self._create_url(user_id, video_id)
                player_info["formats"] = [
                    *self._extract_web_hd_formats(
                        canonical_url,
                        video_id,
                        player_height,
                    ),
                    *player_info["formats"],
                ]
            return player_info
        self.report_warning("TikTok player API did not return usable media; trying webpage")
        return super()._real_extract(url)

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
