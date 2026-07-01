import json
from pathlib import Path

from pinchana_tiktok.media import (
    classify_tiktok_media,
    find_motion_video_url,
    find_still_image_urls,
    normalize_tiktok_info,
)
from pinchana_tiktok.main import _debug_json_urls, _save_debug_json


FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    with open(FIXTURES / name, "r", encoding="utf-8") as f:
        return json.load(f)


def test_classifies_video_from_formats():
    raw = load_fixture("normal_video_info.sanitized.json")

    assert classify_tiktok_media(raw) == "video"


def test_classifies_video_with_raw_web_cover_as_video():
    raw = load_fixture("normal_video_info.sanitized.json")
    raw["__raw_web"] = {
        "video": {
            "cover": "https://cdn.example/video/cover.jpeg",
            "playAddr": "https://cdn.example/video/play.mp4",
        }
    }

    assert classify_tiktok_media(raw) == "video"


def test_classifies_single_photo():
    raw = load_fixture("normal_photo_web.sanitized.json")

    assert classify_tiktok_media(raw) == "photo"


def test_classifies_photo_carousel():
    raw = load_fixture("live_photo_web_s6ierra_7653490043109018910.sanitized.json")

    assert classify_tiktok_media(raw) == "photo_carousel"


def test_real_live_photo_web_fixture_has_no_motion_signal():
    raw = load_fixture("live_photo_web_s6ierra_7653490043109018910.sanitized.json")

    assert len(find_still_image_urls(raw)) == 3
    assert find_motion_video_url(raw) is None
    assert classify_tiktok_media({"__raw_web": raw}) == "photo_carousel"


def test_classifies_live_photo_when_motion_url_exposed():
    raw = load_fixture("live_photo_synthetic_with_motion.sanitized.json")
    normalized = normalize_tiktok_info({"__raw_aweme": raw})

    assert normalized["kind"] == "live_photo"
    assert normalized["image_url"] == "https://cdn.example/live/still.jpeg"
    assert normalized["video_url"] == "https://cdn.example/live/motion.mp4"
    assert normalized["audio_url"] == "https://cdn.example/live/audio.m4a?mime_type=audio_mp4"
    assert normalized["duration"] == 3
    assert normalized["available"] is True


def test_live_photo_partial_explicit_marker():
    raw = {
        "imagePost": {
            "images": [
                {"imageURL": {"urlList": ["https://cdn.example/live/still.jpeg"]}},
            ],
        },
        "livePhoto": {"enabled": True},
    }
    normalized = normalize_tiktok_info({"__raw_aweme": raw})

    assert normalized["kind"] == "live_photo"
    assert normalized["available"] is False
    assert normalized["reason"] == "Motion asset not exposed in current TikTok responses"


def test_debug_json_urls_are_reported_for_saved_files(tmp_path):
    _save_debug_json(
        "123",
        tmp_path,
        {
            "raw_web": {"ok": True},
            "raw_aweme": None,
            "raw_aweme_error": {"error": "failed"},
            "extractor_info": {"id": "123"},
            "normalized": {"kind": "photo"},
        },
    )

    assert _debug_json_urls("123", tmp_path) == {
        "raw_web": "/media/tiktok/123/debug/raw-web.json",
        "raw_aweme_error": "/media/tiktok/123/debug/raw-aweme-error.json",
        "extractor_info": "/media/tiktok/123/debug/extractor-info.json",
        "normalized": "/media/tiktok/123/debug/normalized.json",
    }
