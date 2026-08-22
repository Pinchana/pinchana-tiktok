from yt_dlp import YoutubeDL

from pinchana_tiktok import main
from pinchana_tiktok.extractor import TikTokIE


VIDEO_ID = "7106686413101468970"
WEBPAGE_URL = f"https://www.tiktok.com/@creator/video/{VIDEO_ID}"


def _extractor():
    return TikTokIE(YoutubeDL({"quiet": True, "no_warnings": True}))


def _base_item():
    return {
        "id": int(VIDEO_ID),
        "id_str": VIDEO_ID,
        "desc": "Player API video",
        "author_info": {
            "nickname": "Creator",
            "unique_id": "creator",
            "secret_id": "sec-user",
        },
        "statistics_info": {
            "digg_count": 12,
            "comment_count": 3,
            "share_count": 4,
        },
        "video_info": {
            "meta": {"duration": 15714, "width": 576, "height": 1024},
            "cover": {"url_list": ["https://img.example/cover.jpeg?token=1"]},
        },
    }


def test_player_api_video_preserves_each_fresh_cdn_mirror(monkeypatch):
    item = _base_item()
    item["video_info"]["profiles"] = [{
        "gear_name": "normal_540_0",
        "bitrate": 192118,
        "codec_type": "h264",
        "fps": 60,
        "play_addr": {
            "data_size": 377368,
            "width": 576,
            "height": 1024,
            "url_list": [
                "https://v45.tiktokcdn-eu.com/video.mp4",
                "https://v16m.tiktokcdn-eu.com/video.mp4",
                "https://api16-normal-no1a.tiktokv.eu/aweme/v1/play/?token=1",
            ],
        },
    }]
    extractor = _extractor()
    observed = {}

    def download_json(url, video_id, **kwargs):
        observed.update(url=url, video_id=video_id, kwargs=kwargs)
        return {"status_code": 0, "items": [item]}

    monkeypatch.setattr(extractor, "_download_json", download_json)

    info = extractor._extract_player_api(VIDEO_ID, WEBPAGE_URL)

    assert observed["url"] == extractor._PLAYER_API_URL
    assert observed["kwargs"]["query"] == {"item_ids": VIDEO_ID}
    assert info["id"] == VIDEO_ID
    assert info["duration"] == 15.714
    assert info["uploader_id"] == "creator"
    assert info["thumbnail"].startswith("https://img.example/cover.jpeg")
    assert [item["source_preference"] for item in info["formats"]] == [3, 2, 1]
    assert all(item["format_note"] == "Original TikTok player API" for item in info["formats"])
    assert main._without_watermarked_formats(info)["formats"] == info["formats"]
    assert [item["url"].split("/")[2] for item in info["formats"]] == [
        "v45.tiktokcdn-eu.com",
        "v16m.tiktokcdn-eu.com",
        "api16-normal-no1a.tiktokv.eu",
    ]


def test_player_api_photo_prefers_jpeg_and_includes_soundtrack():
    item = _base_item()
    item["image_post_info"] = {
        "images": [{
            "display_image": {
                "width": 720,
                "height": 1280,
                "url_list": [
                    "https://img.example/photo.webp?token=1",
                    "https://img.example/photo.jpeg?token=1",
                ],
            },
        }],
    }
    item["music_info"] = {"title": "Original sound", "author": "Creator"}
    item["video_info"]["url_list"] = [
        "https://v16-ies-music.tiktokcdn-eu.com/audio?mime_type=audio_mpeg",
        "https://api16-normal-no1a.tiktokv.eu/aweme/v1/play/?token=1",
    ]

    info = _extractor()._parse_player_photo(item, VIDEO_ID, WEBPAGE_URL)

    assert info["_type"] == "playlist"
    assert info["entries"][0]["url"].startswith("https://img.example/photo.jpeg")
    assert info["entries"][0]["ext"] == "jpg"
    assert info["entries"][0]["width"] == 720
    audio = info["entries"][1]
    assert audio["vcodec"] == "none"
    assert audio["track"] == "Original sound"
    assert len(audio["formats"]) == 2
    assert audio["formats"][0]["ext"] == "mp3"


def test_player_api_mismatch_falls_back_instead_of_serving_wrong_post(monkeypatch):
    item = _base_item()
    item["id_str"] = "999"
    extractor = _extractor()
    monkeypatch.setattr(
        extractor,
        "_download_json",
        lambda *_args, **_kwargs: {"status_code": 0, "items": [item]},
    )

    assert extractor._extract_player_api(VIDEO_ID, WEBPAGE_URL) is None
