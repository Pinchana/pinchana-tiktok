import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from yt_dlp import YoutubeDL

from pinchana_core.storage import MediaStorage
from pinchana_tiktok import main
from pinchana_tiktok.extractor import TikTokIE


def _parse_frontity(payload: dict):
    extractor = TikTokIE(YoutubeDL({"quiet": True, "no_warnings": True}))
    extractor._search_json = lambda *args, **kwargs: payload
    return extractor._parse_frontity_video_data("<html></html>", "123")


def test_frontity_regular_video_does_not_expose_watermarked_url_as_playback():
    parsed = _parse_frontity({
        "source": {
            "data": {
                "video": {
                    "videoData": {
                        "itemInfos": {
                            "id": "123",
                            "video": {
                                "urls": ["https://cdn.example/watermarked.mp4"],
                            },
                        },
                    },
                },
            },
        },
    })

    assert parsed is None


def test_frontity_photo_post_remains_supported():
    parsed = _parse_frontity({
        "source": {
            "data": {
                "photo": {
                    "videoData": {
                        "itemInfos": {
                            "id": "123",
                            "text": "Photo post",
                            "video": {"duration": 10},
                        },
                        "imagePostInfo": {
                            "displayImages": [{
                                "urlList": ["https://cdn.example/image.jpg"],
                                "width": 1080,
                                "height": 1920,
                            }],
                        },
                    },
                },
            },
        },
    })

    item = parsed["webapp.video-detail"]["itemInfo"]["itemStruct"]
    assert item["imagePost"]["images"] == [{
        "imageURL": {"urlList": ["https://cdn.example/image.jpg"]},
        "imageWidth": 1080,
        "imageHeight": 1920,
    }]
    assert "playAddr" not in item["video"]
    assert "downloadAddr" not in item["video"]


def test_watermarked_formats_are_removed_without_mutating_extracted_info():
    info = {
        "id": "123",
        "formats": [
            {
                "format_id": "download",
                "format_note": "watermarked",
                "url": "https://cdn.example/watermarked.mp4",
            },
            {
                "format_id": "bytevc1_1080p",
                "format_note": None,
                "url": "https://cdn.example/clean.mp4",
            },
        ],
    }

    filtered = main._without_watermarked_formats(info)

    assert [item["format_id"] for item in filtered["formats"]] == ["bytevc1_1080p"]
    assert len(info["formats"]) == 2


def test_watermarked_only_video_is_rejected():
    info = {
        "formats": [{
            "format_id": "download",
            "format_note": "Download video, watermarked",
            "url": "https://cdn.example/watermarked.mp4",
        }],
    }

    try:
        main._without_watermarked_formats(info)
    except main.ExtractionError as error:
        assert "watermark-free" in str(error)
    else:
        raise AssertionError("Watermarked-only video should be rejected")


def test_format_order_prefers_stable_mp4_before_testing_formats(monkeypatch):
    monkeypatch.setenv("TIKTOK_FORMAT_ATTEMPTS", "3")
    ydl = YoutubeDL({"quiet": True, "no_warnings": True})
    info = {
        "formats": [
            {
                "format_id": "testing-mp4",
                "url": "https://cdn.example/testing.mp4",
                "ext": "mp4",
                "height": 2160,
                "__needs_testing": True,
            },
            {
                "format_id": "stable-webm",
                "url": "https://cdn.example/stable.webm",
                "ext": "webm",
                "height": 1080,
            },
            {
                "format_id": "stable-mp4",
                "url": "https://cdn.example/stable.mp4",
                "ext": "mp4",
                "height": 720,
            },
        ],
    }

    ordered = main._ordered_video_formats(info, ydl)

    assert [item["format_id"] for item in ordered] == [
        "stable-mp4",
        "stable-webm",
        "testing-mp4",
    ]


def test_format_order_reserves_player_fallback_after_hd_mirrors(monkeypatch):
    monkeypatch.setenv("TIKTOK_FORMAT_ATTEMPTS", "3")
    ydl = YoutubeDL({"quiet": True, "no_warnings": True})
    info = {
        "formats": [
            {
                "format_id": f"hd-{index}",
                "url": f"https://api{index}.tiktokv.com/play",
                "ext": "mp4",
                "height": 1920,
                "__hd_refresh": True,
            }
            for index in range(3)
        ] + [{
            "format_id": "player-540",
            "url": "https://v45.tiktokcdn-eu.com/video.mp4",
            "ext": "mp4",
            "height": 1024,
            "__player_api": True,
        }],
    }

    ordered = main._ordered_video_formats(info, ydl)

    assert [item["format_id"] for item in ordered] == [
        "hd-2",
        "hd-1",
        "player-540",
    ]


def test_format_order_prefers_direct_cdn_before_playback_rewrite(monkeypatch):
    monkeypatch.setenv("TIKTOK_FORMAT_ATTEMPTS", "3")
    ydl = YoutubeDL({"quiet": True, "no_warnings": True})
    info = {
        "formats": [
            {
                "format_id": "playback-fallback",
                "url": "https://api16-normal-no1a.tiktokv.eu/aweme/v1/play/",
                "ext": "mp4",
                "height": 1920,
                "preference": -10,
                "__hd_refresh": True,
            },
            {
                "format_id": "direct-v16",
                "url": "https://v16-webapp-prime.tiktok.com/video.mp4",
                "ext": "mp4",
                "height": 1920,
                "source_preference": 2,
                "__hd_refresh": True,
                "__direct_web": True,
            },
            {
                "format_id": "direct-v19",
                "url": "https://v19-webapp-prime.tiktok.com/video.mp4",
                "ext": "mp4",
                "height": 1920,
                "source_preference": 1,
                "__hd_refresh": True,
                "__direct_web": True,
            },
        ],
    }

    ordered = main._ordered_video_formats(info, ydl)

    assert [item["format_id"] for item in ordered] == [
        "direct-v16",
        "direct-v19",
        "playback-fallback",
    ]


@pytest.mark.asyncio
async def test_hevc_video_is_converted_to_share_compatible_h264(monkeypatch, tmp_path):
    source = tmp_path / "video.mp4"
    source.write_bytes(b"hevc")
    observed = {}

    class Process:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def create_process(*args, **kwargs):
        observed.update(args=args, kwargs=kwargs)
        Path(args[-1]).write_bytes(b"h264")
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    result = await main._transcode_hevc_for_sharing(source)

    assert result == source
    assert source.read_bytes() == b"h264"
    assert "libx264" in observed["args"]
    assert "yuv420p" in observed["args"]
    assert observed["kwargs"]["stderr"] == asyncio.subprocess.PIPE


def test_hevc_transcoding_is_opt_in(monkeypatch):
    monkeypatch.delenv("TIKTOK_TRANSCODE_HEVC", raising=False)
    assert not main._transcode_hevc_enabled()

    monkeypatch.setenv("TIKTOK_TRANSCODE_HEVC", "true")
    assert main._transcode_hevc_enabled()


@pytest.mark.asyncio
async def test_video_download_tries_alternate_format_in_same_session(
    monkeypatch,
    tmp_path,
):
    scraper = SimpleNamespace(
        _ydl=YoutubeDL({"quiet": True, "no_warnings": True})
    )
    calls = []

    async def download(candidate_scraper, info, options, **_kwargs):
        calls.append((candidate_scraper, (info.get("formats") or [{}])[0].get("format_id")))
        if len(calls) == 1:
            raise RuntimeError("HTTP Error 403: Forbidden")
        if options.get("skip_download"):
            return info
        video_file = tmp_path / "123" / "video.mp4"
        video_file.parent.mkdir(parents=True, exist_ok=True)
        video_file.write_bytes(b"video")
        return {
            **info,
            "format_id": (info.get("formats") or [{}])[0].get("format_id"),
        }

    monkeypatch.setattr(main, "storage", MediaStorage(tmp_path))
    monkeypatch.setattr(main, "_download_with_ydl_bounded", download)

    response = await main._download_and_build_response(
        "123",
        {
            "id": "123",
            "title": "video",
            "formats": [
                {
                    "format_id": "low",
                    "url": "https://cdn.example/low.mp4",
                    "ext": "mp4",
                    "height": 720,
                },
                {
                    "format_id": "high",
                    "url": "https://cdn.example/high.mp4",
                    "ext": "mp4",
                    "height": 1080,
                },
            ],
        },
        scraper,
    )

    assert [format_id for _, format_id in calls[:2]] == ["high", "low"]
    assert all(candidate_scraper is scraper for candidate_scraper, _ in calls)
    assert response.video_url == "/media/tiktok/123/video.mp4"


@pytest.mark.asyncio
async def test_watermarked_only_scrape_returns_extraction_failure(monkeypatch, tmp_path):
    class Scraper:
        def extract_video(self, _url):
            return {
                "id": "123",
                "formats": [{
                    "format_id": "download",
                    "format_note": "watermarked",
                    "url": "https://cdn.example/watermarked.mp4",
                }],
            }

    monkeypatch.setattr(main, "TikTokScraper", Scraper)
    monkeypatch.setattr(main, "storage", MediaStorage(tmp_path))

    with pytest.raises(main.HTTPException) as exc_info:
        await main._process_scrape_request(
            SimpleNamespace(url="https://www.tiktok.com/@creator/video/123")
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail["code"] == "extraction_failed"


def test_cache_requires_current_version_and_nonempty_media(tmp_path, monkeypatch):
    monkeypatch.setattr(main.storage, "base_path", tmp_path)
    assert not main._cached_media_ready({"media_type": "video"})
    assert not main._cached_media_ready({"media_type": "carousel"})

    cached = {
        "media_type": "video",
        "_tiktok_video_cache_version": main.TIKTOK_VIDEO_CACHE_VERSION,
        "video_url": "/media/tiktok/123/video.mp4",
    }
    media = tmp_path / "123" / "video.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"")
    assert not main._cached_media_ready(cached)

    media.write_bytes(b"video")
    assert main._cached_media_ready(cached)


@pytest.mark.asyncio
async def test_incomplete_carousel_is_retryable_and_not_cached(monkeypatch, tmp_path):
    class Storage(MediaStorage):
        saved = False

        def save_metadata(self, post_id, metadata):
            self.saved = True
            return super().save_metadata(post_id, metadata)

    storage = Storage(tmp_path)

    async def incomplete_download(_scraper, _info, _options, **_kwargs):
        image = tmp_path / "123" / "images" / "01.jpg"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"image")
        return _info

    class Scraper:
        def extract_video(self, _url):
            return {
                "_type": "playlist",
                "entries": [
                    {"url": "https://cdn.example/1.jpg", "ext": "jpg"},
                    {"url": "https://cdn.example/2.jpg", "ext": "jpg"},
                ],
            }

    monkeypatch.setattr(main, "storage", storage)
    monkeypatch.setattr(main, "TikTokScraper", Scraper)
    monkeypatch.setattr(main, "_download_with_ydl_bounded", incomplete_download)

    with pytest.raises(main.HTTPException) as exc_info:
        await main._process_scrape_request(
            SimpleNamespace(url="https://www.tiktok.com/@creator/photo/123")
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "media_download_failed"
    assert storage.saved is False
