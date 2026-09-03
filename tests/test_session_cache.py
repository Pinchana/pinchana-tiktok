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


def test_session_cache_reuses_cookies_without_mobile_identity(monkeypatch):
    monkeypatch.setenv("TIKTOK_REQUEST_INTERVAL_SECONDS", "0.75")
    monkeypatch.setenv("TIKTOK_INTERNAL_REQUEST_INTERVAL_SECONDS", "0.1")
    monkeypatch.setenv("TIKTOK_PROXY_URL", "http://proxy.example:8888")
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
    assert second._ydl.params["sleep_interval_requests"] == 0.1
    assert second._ydl.params["proxy"] == "http://proxy.example:8888"
    assert "device_id" not in second._ydl.params["extractor_args"]["tiktok"]
    assert "app_info" not in second._ydl.params["extractor_args"]["tiktok"]

    cache.clear()
    third = TikTokScraper(session_cache=cache)
    assert not list(third._ydl.cookiejar)
    assert "device_id" not in third._ydl.params["extractor_args"]["tiktok"]


def test_video_extraction_uses_player_api_with_optional_hd_discovery(monkeypatch):
    ie = TikTokIE(YoutubeDL({"quiet": True}))
    monkeypatch.setattr(
        ie,
        "_extract_player_api",
        lambda video_id, _url: {"id": video_id, "formats": [{"url": "https://cdn.example/video.mp4"}]},
    )
    monkeypatch.setattr(ie, "_extract_web_hd_formats", lambda *_args: [])

    result = ie._real_extract(
        "https://www.tiktok.com/@creator/video/7663781221171776789"
    )

    assert result["id"] == "7663781221171776789"
    assert result["formats"][0]["url"] == "https://cdn.example/video.mp4"


def test_video_extraction_falls_back_to_web_without_mobile_api(monkeypatch):
    ie = TikTokIE(YoutubeDL({"quiet": True}))
    monkeypatch.setattr(ie, "_extract_player_api", lambda _video_id, _url: None)
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
    monkeypatch.setattr(
        ie,
        "_extract_aweme_app",
        lambda _video_id: (_ for _ in ()).throw(
            AssertionError("anonymous extraction must not call the mobile API")
        ),
    )

    result = ie._real_extract(
        "https://www.tiktok.com/@creator/video/7663781221171776789"
    )

    assert result == {"id": "7663781221171776789"}


def test_mobile_app_api_is_never_called_after_web_failure(monkeypatch):
    ie = TikTokIE(YoutubeDL({"quiet": True}))
    app_calls = 0

    def fail_web(_url, _video_id):
        raise ExtractorError("Unable to extract universal data for rehydration")

    def extract_app(_video_id):
        nonlocal app_calls
        app_calls += 1
        raise AssertionError("anonymous extraction must not call the mobile API")

    monkeypatch.setattr(ie, "_extract_player_api", lambda _video_id, _url: None)
    monkeypatch.setattr(ie, "_extract_web_data_and_status", fail_web)
    monkeypatch.setattr(ie, "_extract_aweme_app", extract_app)

    with pytest.raises(ExtractorError, match="universal data"):
        ie._real_extract("https://www.tiktok.com/@creator/video/7663781221171776789")

    assert app_calls == 0


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
        return "<html>no metadata</html>", SimpleNamespace(url=url, extensions={})

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
