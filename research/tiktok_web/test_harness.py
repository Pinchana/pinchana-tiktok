import base64
import json

from research.tiktok_web.harness import (
    classify_item,
    extract_item_struct,
    extract_image_assets,
    extract_renditions,
    parse_hydration_html,
    pacing_floor,
    rank_renditions,
    summarize_har,
    url_shape,
)


VIDEO_ID = "7680845042994466066"


def _item():
    return {
        "id": VIDEO_ID,
        "author": {"uniqueId": "creator"},
        "video": {
            "bitrateInfo": [
                {
                    "GearName": "adapt_1080_1",
                    "CodecType": "h265_hvc1",
                    "Bitrate": 1_089_000,
                    "PlayAddr": {
                        "Width": 1080,
                        "Height": 1920,
                        "DataSize": 4_000_000,
                        "UrlList": [
                            "https://v16-webapp-prime.tiktok.com/video/signed-value?token=secret",
                            "https://www.tiktok.com/aweme/v1/play/?video_id=secret&signaturev3=secret",
                        ],
                    },
                },
                {
                    "GearName": "normal_540_0",
                    "CodecType": "h264",
                    "Bitrate": 796_000,
                    "PlayAddr": {
                        "Width": 576,
                        "Height": 1024,
                        "DataSize": 3_000_000,
                        "UrlList": ["https://v19-webapp-prime.tiktok.com/video/other-secret"],
                    },
                },
            ],
        },
    }


def _html(item):
    payload = {
        "__DEFAULT_SCOPE__": {
            "webapp.video-detail": {
                "statusCode": 0,
                "itemInfo": {"itemStruct": item},
            }
        }
    }
    return (
        '<html><script type="application/json" '
        'id="__UNIVERSAL_DATA_FOR_REHYDRATION__">'
        f"{json.dumps(payload)}</script></html>"
    )


def test_hydration_path_and_quality_ranking():
    hydration = parse_hydration_html(_html(_item()))
    item, status = extract_item_struct(hydration)
    ranked = rank_renditions(extract_renditions(item))

    assert status == 0
    assert classify_item(item) == "video"
    assert [(row.width, row.height, row.codec) for row in ranked] == [
        (1080, 1920, "h265"),
        (576, 1024, "h264"),
    ]


def test_compatibility_policy_prefers_h264():
    ranked = rank_renditions(extract_renditions(_item()), policy="compatibility")
    assert ranked[0].codec == "h264"


def test_photo_assets_preserve_order_dimensions_and_mirrors():
    item = {
        "imagePost": {
            "images": [{
                "imageWidth": 1080,
                "imageHeight": 1439,
                "imageURL": {
                    "urlList": [
                        "https://p16-common-sign.tiktokcdn-eu.com/path?a=secret",
                        "https://p19-common-sign.tiktokcdn-eu.com/path?a=secret",
                    ]
                },
            }]
        }
    }

    assets = extract_image_assets(item)

    assert classify_item(item) == "photo"
    assert [(asset.index, asset.width, asset.height, len(asset.urls)) for asset in assets] == [
        (0, 1080, 1439, 2),
    ]


def test_url_shape_never_exposes_signed_values():
    safe = url_shape(
        "https://www.tiktok.com/aweme/v1/play/?video_id=123&signaturev3=top-secret"
    )
    assert safe == (
        "https://www.tiktok.com/aweme/v1/play/"
        "?signaturev3=<redacted>&video_id=<redacted>"
    )
    assert "top-secret" not in safe
    assert "123" not in safe
    assert url_shape("https://v19-web-newkey.tiktokcdn.com/opaque/hash/file") == (
        "https://v19-web-newkey.tiktokcdn.com/<redacted>"
    )


def test_har_summary_uses_wall_clock_between_request_starts(tmp_path):
    html = _html(_item()).encode()
    entries = [
        {
            "startedDateTime": "2026-09-03T02:34:10.279Z",
            "time": 10_737.31,
            "request": {
                "method": "GET",
                "url": "https://vt.tiktok.com/ZSq19PkKF",
                "headersSize": 1,
                "headers": [],
                "cookies": [],
            },
            "response": {
                "status": 301,
                "redirectURL": f"https://www.tiktok.com/@creator/video/{VIDEO_ID}?_t=secret",
                "headers": [],
                "content": {},
            },
            "timings": {"dns": 5213.792, "connect": 5298.682, "ssl": 58.578},
        },
        {
            "startedDateTime": "2026-09-03T02:34:15.803Z",
            "time": 1089.546,
            "request": {
                "method": "GET",
                "url": f"https://www.tiktok.com/@creator/video/{VIDEO_ID}?_t=secret",
                "headersSize": -1,
                "headers": [{
                    "name": "referer",
                    "value": "https://www.tiktok.com/@creator/video/123?_t=secret",
                }],
                "cookies": [],
            },
            "response": {
                "status": 200,
                "redirectURL": "",
                "headers": [{
                    "name": "location",
                    "value": "https://www.tiktok.com/@creator/video/123?_t=secret",
                }],
                "content": {
                    "encoding": "base64",
                    "text": base64.b64encode(html).decode(),
                    "size": len(html),
                },
            },
            "timings": {"dns": 203.291, "connect": 409.973, "wait": 408.648},
        },
    ]
    path = tmp_path / "capture.har"
    path.write_text(json.dumps({"log": {"pages": [{}], "entries": entries}}))

    summary = summarize_har(path, video_id=VIDEO_ID)

    assert summary["redirect_to_document_start_ms"] == 5524.0
    assert summary["hydration"]["item_id"] == VIDEO_ID
    assert summary["hydration"]["renditions"][0]["url_count"] == 2
    assert "secret" not in json.dumps(summary)


def test_har_summary_reports_photo_detail_when_page_hydration_is_empty(tmp_path):
    page_payload = {"__DEFAULT_SCOPE__": {"webapp.app-context": {}, "webapp.a-b": {}}}
    page_html = (
        '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">'
        f"{json.dumps(page_payload)}</script>"
    ).encode()
    photo_item = {
        "id": VIDEO_ID,
        "imagePost": {
            "images": [{
                "imageWidth": 864,
                "imageHeight": 1536,
                "imageURL": {"urlList": [
                    "https://p16-common-sign.tiktokcdn-eu.com/opaque?x-signature=secret",
                    "https://p19-common-sign.tiktokcdn-eu.com/opaque?x-signature=secret",
                ]},
            }]
        },
        "music": {"playUrl": "https://music.example/opaque", "duration": 60},
    }
    detail_body = json.dumps({
        "statusCode": 0,
        "itemInfo": {"itemStruct": photo_item},
    }).encode()
    entries = [
        {
            "_resourceType": "document",
            "startedDateTime": "2026-09-03T03:02:10.156Z",
            "time": 149,
            "request": {"method": "GET", "url": "https://vt.tiktok.com/short", "headers": [], "cookies": []},
            "response": {
                "status": 301,
                "redirectURL": f"https://www.tiktok.com/@creator/photo/{VIDEO_ID}?_t=secret",
                "headers": [],
                "content": {},
            },
            "timings": {},
        },
        {
            "_resourceType": "document",
            "startedDateTime": "2026-09-03T03:02:10.305Z",
            "time": 369,
            "request": {"method": "GET", "url": f"https://www.tiktok.com/@creator/photo/{VIDEO_ID}", "headers": [], "cookies": []},
            "response": {
                "status": 200,
                "redirectURL": "",
                "headers": [],
                "content": {"encoding": "base64", "text": base64.b64encode(page_html).decode(), "size": len(page_html)},
            },
            "timings": {},
        },
        {
            "_resourceType": "fetch",
            "startedDateTime": "2026-09-03T03:02:11.974Z",
            "time": 230,
            "request": {
                "method": "GET",
                "url": f"https://www.tiktok.com/api/item/detail/?itemId={VIDEO_ID}&msToken=secret",
                "headers": [{"name": "user-agent", "value": "browser"}],
                "cookies": [],
            },
            "response": {
                "status": 200,
                "httpVersion": "http/2.0",
                "headers": [],
                "content": {"encoding": "base64", "text": base64.b64encode(detail_body).decode(), "size": len(detail_body)},
            },
            "timings": {},
        },
    ]
    path = tmp_path / "photo.har"
    path.write_text(json.dumps({"log": {"pages": [{}], "entries": entries}}))

    summary = summarize_har(path, video_id=VIDEO_ID)

    assert summary["hydration"]["usable"] is False
    assert summary["item_detail_api"]["item_type"] == "photo"
    assert summary["item_detail_api"]["image_count"] == 1
    assert summary["item_detail_api"]["images"][0]["url_count"] == 2
    assert "secret" not in json.dumps(summary)


def test_pacing_floor_counts_runner_and_each_yt_dlp_gap():
    assert pacing_floor(
        interval=2.0,
        short_request_seconds=0.3,
        metadata_requests=3,
    ) == {
        "yt_dlp_internal_seconds": 4.0,
        "runner_between_resolve_and_extract_seconds": 1.7,
        "combined_seconds": 5.7,
    }
