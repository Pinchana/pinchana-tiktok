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
async def test_rate_limit_rotates_once_and_retries_once(monkeypatch):
    attempts = 0
    rotations = 0

    class Scraper:
        def extract_video(self, _url):
            nonlocal attempts
            attempts += 1
            raise RuntimeError("HTTP Error 429: Too Many Requests")

    async def fake_rotation():
        nonlocal rotations
        rotations += 1

    monkeypatch.setenv("VPN_ENABLED", "1")
    monkeypatch.setattr(main, "TikTokScraper", Scraper)
    monkeypatch.setattr(main, "trigger_rotation", fake_rotation)
    monkeypatch.setattr(main.storage, "is_cached", lambda _post_id: False)

    with pytest.raises(main.HTTPException) as exc_info:
        await main._process_scrape_request(
            SimpleNamespace(url="https://www.tiktok.com/@creator/video/123456")
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "rate_limited"
    assert attempts == 2
    assert rotations == 1


@pytest.mark.parametrize(
    ("message", "exception_type"),
    [
        ("This video was removed", main.MediaNotFoundError),
        ("Unable to parse an unexpected response", main.ExtractionError),
        ("HTTP Error 403: Forbidden", main.RateLimitError),
    ],
)
def test_extractor_errors_are_classified_without_broad_unavailable_matching(
    message, exception_type
):
    assert isinstance(main._classify_extraction_error(RuntimeError(message)), exception_type)

