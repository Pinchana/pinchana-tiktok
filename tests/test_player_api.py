from urllib.parse import urlsplit

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


def test_web_hd_redirect_is_routed_to_tiktok_playback_hosts():
    web_data = {
        "video": {
            "bitrateInfo": [
                {
                    "GearName": "lowest_540_0",
                    "CodecType": "h264",
                    "PlayAddr": {
                        "Width": 576,
                        "Height": 1024,
                        "UrlList": ["https://www.tiktok.com/aweme/v1/play/?low=1"],
                    },
                },
                {
                    "GearName": "adapt_lower_720_1",
                    "CodecType": "h265_hvc1",
                    "Bitrate": 746598,
                    "BitrateFPS": 30,
                    "PlayAddr": {
                        "Width": 720,
                        "Height": 1280,
                        "DataSize": "2840807",
                        "UrlList": [
                            "https://v16-webapp-prime.tiktok.com/video.mp4",
                            "https://www.tiktok.com/aweme/v1/play/?signaturev3=hd",
                        ],
                    },
                },
            ],
        },
    }

    formats = _extractor()._parse_web_hd_formats(
        web_data,
        WEBPAGE_URL,
        minimum_height=1024,
    )

    assert len(formats) == 4
    assert all(item["height"] == 1280 for item in formats)
    assert all(item["vcodec"] == "h265" for item in formats)
    assert all(item["__hd_refresh"] for item in formats)
    assert [urlsplit(item["url"]).hostname for item in formats] == [
        "v16-webapp-prime.tiktok.com",
        "api16-normal-no1a.tiktokv.eu",
        "api16-normal-c-useast1a.tiktokv.com",
        "api22-normal-c-useast1a.tiktokv.com",
    ]
    assert formats[0]["__direct_web"] is True
    assert all("signaturev3=hd" in item["url"] for item in formats[1:])


def test_direct_web_video_uses_validated_hydration_without_player_api(monkeypatch):
    extractor = _extractor()
    item = {
        "id": VIDEO_ID,
        "desc": "Direct video",
        "author": {"uniqueId": "creator"},
        "video": {
            "width": 1080,
            "height": 1920,
            "bitrateInfo": [{
                "GearName": "adapt_1080_1",
                "CodecType": "h265_hvc1",
                "Bitrate": 1_089_740,
                "PlayAddr": {
                    "Width": 1080,
                    "Height": 1920,
                    "DataSize": 1_948_319,
                    "UrlList": [
                        "https://v16-webapp-prime.tiktok.com/video.mp4",
                        "https://v19-webapp-prime.tiktok.com/video.mp4",
                        "https://www.tiktok.com/aweme/v1/play/?signaturev3=hd",
                    ],
                },
            }],
        },
    }
    player_calls = 0

    monkeypatch.setattr(
        extractor,
        "_configuration_arg",
        lambda key: ["true"] if key == "direct_web_primary" else [],
    )
    monkeypatch.setattr(
        extractor,
        "_download_webpage_handle",
        lambda *args, **kwargs: (
            "<html>hydration</html>",
            type("Handle", (), {"url": WEBPAGE_URL})(),
        ),
    )
    monkeypatch.setattr(
        extractor,
        "_get_universal_data",
        lambda *_args: {
            "webapp.video-detail": {
                "statusCode": 0,
                "itemInfo": {"itemStruct": item},
            }
        },
    )

    def player_api(*_args):
        nonlocal player_calls
        player_calls += 1
        return None

    monkeypatch.setattr(extractor, "_extract_player_api", player_api)

    info = extractor._real_extract(WEBPAGE_URL)

    assert player_calls == 0
    assert [urlsplit(row["url"]).hostname for row in info["formats"][:2]] == [
        "v16-webapp-prime.tiktok.com",
        "v19-webapp-prime.tiktok.com",
    ]
    assert all(row["__direct_web"] for row in info["formats"][:2])
    assert info["formats"][0]["height"] == 1920
    assert info["formats"][0]["vcodec"] == "h265"


def test_direct_web_mismatched_item_falls_back_to_player_api(monkeypatch):
    extractor = _extractor()
    monkeypatch.setattr(
        extractor,
        "_configuration_arg",
        lambda key: ["true"] if key == "direct_web_primary" else [],
    )
    monkeypatch.setattr(
        extractor,
        "_download_webpage_handle",
        lambda *args, **kwargs: (
            "<html>hydration</html>",
            type("Handle", (), {"url": WEBPAGE_URL})(),
        ),
    )
    monkeypatch.setattr(
        extractor,
        "_get_universal_data",
        lambda *_args: {
            "webapp.video-detail": {
                "statusCode": 0,
                "itemInfo": {"itemStruct": {"id": "999", "video": {}}},
            }
        },
    )
    monkeypatch.setattr(
        extractor,
        "_extract_player_api",
        lambda video_id, _url: {"id": video_id, "formats": []},
    )

    info = extractor._real_extract(WEBPAGE_URL)

    assert info == {"id": VIDEO_ID, "formats": []}


def test_photo_skips_direct_web_and_uses_player_api(monkeypatch):
    extractor = _extractor()
    monkeypatch.setattr(
        extractor,
        "_configuration_arg",
        lambda key: ["true"] if key == "direct_web_primary" else [],
    )
    monkeypatch.setattr(
        extractor,
        "_extract_direct_web_video",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("photo posts must not request canonical hydration")
        ),
    )
    monkeypatch.setattr(
        extractor,
        "_extract_player_api",
        lambda video_id, _url: {"id": video_id, "_type": "playlist", "entries": []},
    )

    info = extractor._real_extract(
        f"https://www.tiktok.com/@creator/photo/{VIDEO_ID}"
    )

    assert info["_type"] == "playlist"
