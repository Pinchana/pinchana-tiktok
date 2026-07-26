from http.cookiejar import Cookie
from types import SimpleNamespace

import pytest
from yt_dlp import YoutubeDL
from yt_dlp.utils import ExtractorError

from pinchana_tiktok.api import TikTokScraper, TikTokSessionCache
from pinchana_tiktok.extractor import TikTokIE


def _cookie(name="ttwid", value="session-value"):
    return Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=".tiktok.com",
        domain_specified=True,
        domain_initial_dot=True,
        path="/",
        path_specified=True,
        secure=True,
        expires=None,
        discard=True,
        comment=None,
        comment_url=None,
        rest={},
        rfc2109=False,
    )


def test_session_cache_reuses_cookies_device_id_and_request_pacing(monkeypatch):
    device_ids = iter(("7250000000000000001", "7250000000000000002"))
    monkeypatch.setattr(
        TikTokSessionCache,
        "_new_device_id",
        staticmethod(lambda: next(device_ids)),
    )
    monkeypatch.setenv("TIKTOK_REQUEST_INTERVAL_SECONDS", "0.75")
    cache = TikTokSessionCache()

    first = TikTokScraper(session_cache=cache)
    first._ydl.cookiejar.set_cookie(_cookie())
    cache.capture(first._ydl)

    second = TikTokScraper(session_cache=cache)
    second_cookies = {
        (cookie.domain, cookie.path, cookie.name): cookie.value
        for cookie in second._ydl.cookiejar
    }
    assert second_cookies[(".tiktok.com", "/", "ttwid")] == "session-value"
    assert second._ydl.params["sleep_interval_requests"] == 0.75
    assert second._ydl.params["extractor_args"]["tiktok"]["device_id"] == [
        "7250000000000000001"
    ]

    cache.clear()
    third = TikTokScraper(session_cache=cache)
    assert not list(third._ydl.cookiejar)
    assert third._ydl.params["extractor_args"]["tiktok"]["device_id"] == [
        "7250000000000000002"
    ]


def test_video_extraction_uses_web_before_app_api(monkeypatch):
    ie = TikTokIE(YoutubeDL({"quiet": True}))
    app_calls = 0

    def fail_if_app_called(_video_id):
        nonlocal app_calls
        app_calls += 1
        raise AssertionError("app API should only be a fallback")

    monkeypatch.setattr(
        ie,
        "_extract_web_data_and_status",
        lambda _url, _video_id: ({"video": {"playAddr": "https://example.com/video.mp4"}}, 0),
    )
    monkeypatch.setattr(
        ie,
        "_parse_aweme_video_web",
        lambda _data, _url, video_id: {"id": video_id},
    )
    monkeypatch.setattr(ie, "_extract_aweme_app", fail_if_app_called)

    result = ie._real_extract(
        "https://www.tiktok.com/@creator/video/7663781221171776789"
    )

    assert result == {"id": "7663781221171776789"}
    assert app_calls == 0


def test_app_api_is_called_once_after_web_failure(monkeypatch):
    ie = TikTokIE(YoutubeDL({"quiet": True}))
    app_calls = 0

    def fail_web(_url, _video_id):
        raise ExtractorError("Unable to extract universal data for rehydration")

    def extract_app(video_id):
        nonlocal app_calls
        app_calls += 1
        return {"id": video_id}

    monkeypatch.setattr(ie, "_extract_web_data_and_status", fail_web)
    monkeypatch.setattr(ie, "_extract_aweme_app", extract_app)

    result = ie._real_extract(
        "https://www.tiktok.com/@creator/video/7663781221171776789"
    )

    assert result == {"id": "7663781221171776789"}
    assert app_calls == 1


def test_embed_page_is_only_used_after_canonical_page_misses(monkeypatch):
    ie = TikTokIE(YoutubeDL({"quiet": True}))
    events = []
    fallback_data = {
        "webapp.video-detail": {
            "statusCode": 0,
            "itemInfo": {"itemStruct": {"id": "photo", "imagePost": {"images": []}}},
        }
    }

    def download_canonical(url, _video_id, _note, **_kwargs):
        events.append(("canonical", url))
        return "<html>no metadata</html>", SimpleNamespace(url=url)

    def download_embed(url, _video_id, **_kwargs):
        events.append(("embed", url))
        return "<html>embed metadata</html>"

    monkeypatch.setattr(ie, "_download_webpage_handle", download_canonical)
    monkeypatch.setattr(ie, "_get_universal_data", lambda _html, _video_id: {})
    monkeypatch.setattr(
        ie,
        "_solve_challenge_and_set_cookies",
        lambda _html: (_ for _ in ()).throw(ExtractorError("no challenge")),
    )
    monkeypatch.setattr(ie, "_download_webpage", download_embed)
    monkeypatch.setattr(ie, "_parse_frontity_video_data", lambda _html, _video_id: fallback_data)

    video_data, status = ie._extract_web_data_and_status(
        "https://www.tiktok.com/@creator/photo/7663781221171776789",
        "7663781221171776789",
    )

    assert video_data["id"] == "photo"
    assert status == 0
    assert [event[0] for event in events] == ["canonical", "embed"]
