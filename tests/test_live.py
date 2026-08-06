import os

import httpx
import pytest


pytestmark = pytest.mark.skipif(
    os.getenv("PINCHANA_TIKTOK_LIVE") != "1",
    reason="set PINCHANA_TIKTOK_LIVE=1 to run the operator-supplied live matrix",
)


def _fixture_url(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        pytest.skip(f"{name} is not configured")
    return value


def _scrape(url: str) -> httpx.Response:
    base_url = os.getenv("TIKTOK_LIVE_BASE_URL", "http://127.0.0.1:8081")
    return httpx.post(f"{base_url}/scrape", json={"url": url}, timeout=180.0)


@pytest.mark.parametrize(
    "variable",
    ["TIKTOK_LIVE_VIDEO_URL", "TIKTOK_LIVE_SHORT_URL"],
)
def test_live_public_video(variable):
    response = _scrape(_fixture_url(variable))
    response.raise_for_status()
    payload = response.json()
    assert payload["media_type"] == "video"
    assert payload["video_url"].startswith("/media/tiktok/")


def test_live_public_photo_post():
    response = _scrape(_fixture_url("TIKTOK_LIVE_PHOTO_URL"))
    response.raise_for_status()
    payload = response.json()
    assert payload["media_type"] == "carousel"
    assert payload["carousel"]
    assert [item["index"] for item in payload["carousel"]] == list(
        range(len(payload["carousel"]))
    )


def test_live_unavailable_post():
    response = _scrape(_fixture_url("TIKTOK_LIVE_UNAVAILABLE_URL"))
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"
