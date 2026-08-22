from types import SimpleNamespace

import pytest

from pinchana_tiktok import main


@pytest.mark.asyncio
async def test_login_gated_post_fails_once_without_vpn_rotation(monkeypatch):
    attempts = 0
    rotations = 0

    class Scraper:
        def extract_video(self, _url):
            nonlocal attempts
            attempts += 1
            raise RuntimeError(
                "This post may not be comfortable for some audiences. Log in for access. "
                "Use --cookies-from-browser or --cookies for the authentication."
            )

    async def fake_rotation():
        nonlocal rotations
        rotations += 1

    monkeypatch.setattr(main, "TikTokScraper", Scraper)
    monkeypatch.setattr(main, "trigger_rotation", fake_rotation)
    monkeypatch.setattr(main.storage, "is_cached", lambda _post_id: False)

    with pytest.raises(main.HTTPException) as exc_info:
        await main._process_scrape_request(
            SimpleNamespace(url="https://www.tiktok.com/@creator/video/7656488676364422422")
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "authentication_required"
    assert attempts == 1
    assert rotations == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected_attempts", "expected_cache_clears"),
    [
        ("HTTP Error 429: Too Many Requests", 2, 1),
        (
            "[TikTok] 7663781221171776789: Unable to extract universal data for rehydration",
            3,
            2,
        ),
    ],
)
async def test_rate_limit_retries_fresh_session_then_reconnects_once(
    monkeypatch, message, expected_attempts, expected_cache_clears
):
    attempts = 0
    rotations = 0
    cache_clears = 0

    class Scraper:
        def extract_video(self, _url):
            nonlocal attempts
            attempts += 1
            raise RuntimeError(message)

    async def fake_rotation():
        nonlocal rotations
        rotations += 1

    def fake_clear_session_cache():
        nonlocal cache_clears
        cache_clears += 1

    monkeypatch.setenv("VPN_ENABLED", "1")
    monkeypatch.setenv("TIKTOK_RETRY_DELAY_SECONDS", "0")
    monkeypatch.setattr(main, "TikTokScraper", Scraper)
    monkeypatch.setattr(main, "trigger_rotation", fake_rotation)
    monkeypatch.setattr(main.tiktok_session_cache, "clear", fake_clear_session_cache)
    monkeypatch.setattr(main.storage, "is_cached", lambda _post_id: False)
    monkeypatch.setattr(main, "_probe_oembed", lambda _url: _async_value("available"))

    with pytest.raises(main.HTTPException) as exc_info:
        await main._process_scrape_request(
            SimpleNamespace(url="https://www.tiktok.com/@creator/video/123456")
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "rate_limited"
    assert attempts == expected_attempts
    assert rotations == 1
    assert cache_clears == expected_cache_clears


@pytest.mark.asyncio
async def test_challenge_failure_retries_fresh_session_before_vpn(monkeypatch):
    attempts = 0
    rotations = 0

    class Scraper:
        def extract_video(self, _url):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError(
                    "Unable to extract universal data for rehydration"
                )
            return {"id": "123456"}

    async def fake_rotation():
        nonlocal rotations
        rotations += 1

    async def fake_build_response(_video_id, info, _scraper):
        return info

    monkeypatch.setenv("VPN_ENABLED", "1")
    monkeypatch.setenv("TIKTOK_RETRY_DELAY_SECONDS", "0")
    monkeypatch.setattr(main, "TikTokScraper", Scraper)
    monkeypatch.setattr(main, "trigger_rotation", fake_rotation)
    monkeypatch.setattr(main, "_download_and_build_response", fake_build_response)
    monkeypatch.setattr(main.storage, "is_cached", lambda _post_id: False)
    monkeypatch.setattr(main, "_probe_oembed", lambda _url: _async_value("available"))

    result = await main._process_scrape_request(
        SimpleNamespace(url="https://www.tiktok.com/@creator/video/123456")
    )

    assert result == {"id": "123456"}
    assert attempts == 2
    assert rotations == 0


@pytest.mark.parametrize(
    ("message", "exception_type"),
    [
        ("This video was removed", main.MediaNotFoundError),
        ("Unable to parse an unexpected response", main.ExtractionError),
        ("HTTP Error 403: Forbidden", main.RateLimitError),
        (
            "Unable to extract universal data for rehydration",
            main.ExtractionError,
        ),
    ],
)
def test_extractor_errors_are_classified_without_broad_unavailable_matching(
    message, exception_type
):
    assert isinstance(main._classify_extraction_error(RuntimeError(message)), exception_type)


def test_media_404_is_classified_as_refreshable_with_stage_context():
    error = main.TikTokRequestError(
        "video_download",
        RuntimeError("HTTP Error 404: Not Found"),
        url="https://v16.tiktokcdn.com/video.mp4",
        format_id="play_addr",
    )

    assert isinstance(main._classify_extraction_error(error), main.RateLimitError)
    assert error.stage == "video_download"
    assert error.host == "v16.tiktokcdn.com"
    assert error.status_code == 404


@pytest.mark.asyncio
async def test_media_403_refreshes_urls_before_vpn_rotation(monkeypatch):
    extractions = 0
    downloads = 0
    rotations = 0

    class Scraper:
        def extract_video(self, _url):
            nonlocal extractions
            extractions += 1
            return {"id": "123456"}

    async def fake_build_response(_video_id, info, _scraper):
        nonlocal downloads
        downloads += 1
        if downloads == 1:
            raise main.TikTokRequestError(
                "video_download",
                RuntimeError("HTTP Error 403: Forbidden"),
                url="https://v16.tiktokcdn.com/video.mp4",
                format_id="play_addr",
            )
        return info

    async def fake_rotation():
        nonlocal rotations
        rotations += 1

    monkeypatch.setenv("VPN_ENABLED", "1")
    monkeypatch.setenv("TIKTOK_RETRY_DELAY_SECONDS", "0")
    monkeypatch.setattr(main, "TikTokScraper", Scraper)
    monkeypatch.setattr(main, "_download_and_build_response", fake_build_response)
    monkeypatch.setattr(main, "trigger_rotation", fake_rotation)
    monkeypatch.setattr(main.storage, "is_cached", lambda _post_id: False)

    result = await main._process_scrape_request(
        SimpleNamespace(url="https://www.tiktok.com/@creator/video/123456")
    )

    assert result == {"id": "123456"}
    assert extractions == 2
    assert downloads == 2
    assert rotations == 0


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_transport_failure_retries_without_rotation(monkeypatch):
    attempts = 0
    rotations = 0

    class Scraper:
        def extract_video(self, _url):
            nonlocal attempts
            attempts += 1
            raise RuntimeError("Connection reset by peer")

    async def fake_rotation():
        nonlocal rotations
        rotations += 1

    monkeypatch.setattr(main, "TikTokScraper", Scraper)
    monkeypatch.setattr(main, "trigger_rotation", fake_rotation)
    monkeypatch.setattr(main.storage, "is_cached", lambda _post_id: False)
    monkeypatch.setattr(main.asyncio, "sleep", lambda _delay: _async_value(None))

    with pytest.raises(main.HTTPException) as exc_info:
        await main._process_scrape_request(
            SimpleNamespace(url="https://www.tiktok.com/@creator/video/123456")
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "upstream_unavailable"
    assert attempts == 2
    assert rotations == 0


@pytest.mark.asyncio
async def test_oembed_not_found_disambiguates_missing_web_data(monkeypatch):
    class Scraper:
        def extract_video(self, _url):
            raise RuntimeError("Unable to extract universal data for rehydration")

    monkeypatch.setattr(main, "TikTokScraper", Scraper)
    monkeypatch.setattr(main.storage, "is_cached", lambda _post_id: False)
    monkeypatch.setattr(main, "_probe_oembed", lambda _url: _async_value("not_found"))

    with pytest.raises(main.HTTPException) as exc_info:
        await main._process_scrape_request(
            SimpleNamespace(url="https://www.tiktok.com/@creator/video/123456")
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["code"] == "not_found"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://www.tiktok.com/@creator/video/7663781221171776789?_r=1&_t=tracking",
            "https://www.tiktok.com/@creator/video/7663781221171776789",
        ),
        (
            "https://m.tiktok.com/@creator/photo/7663781221171776789#share",
            "https://www.tiktok.com/@creator/photo/7663781221171776789",
        ),
        (
            "https://www.tiktok.com/v/7663781221171776789?lang=en",
            "https://www.tiktok.com/@_/video/7663781221171776789",
        ),
        (
            "https://www.tiktok.com/share/video/7663781221171776789?share_app_id=1233",
            "https://www.tiktok.com/share/video/7663781221171776789",
        ),
    ],
)
def test_canonicalize_tiktok_url_removes_tracking_data(url, expected):
    assert main.canonicalize_tiktok_url(url) == expected
