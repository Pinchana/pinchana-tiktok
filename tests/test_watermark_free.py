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


def test_legacy_video_cache_is_invalidated_but_carousel_cache_is_preserved():
    assert not main._cached_media_ready({"media_type": "video"})
    assert main._cached_media_ready({
        "media_type": "video",
        "_tiktok_video_cache_version": main.TIKTOK_VIDEO_CACHE_VERSION,
    })
    assert main._cached_media_ready({"media_type": "carousel"})
