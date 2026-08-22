import asyncio
import threading
import time
import pytest

from pinchana_tiktok import main
from pinchana_tiktok.api import TikTokScraper
from pinchana_tiktok.extractor import TikTokIE
from yt_dlp import YoutubeDL


def test_scraper_session_uses_configured_transport(monkeypatch):
    monkeypatch.setenv("TIKTOK_PROXY_URL", "http://gluetun:8888")
    monkeypatch.setenv("TIKTOK_REQUEST_INTERVAL_SECONDS", "0.25")

    scraper = TikTokScraper()

    assert scraper._ydl.params["proxy"] == "http://gluetun:8888"
    assert scraper._ydl.params["sleep_interval_requests"] == 0.25


def test_download_temporarily_reuses_extraction_ydl(monkeypatch, tmp_path):
    ydl = YoutubeDL({"quiet": True, "no_warnings": True})
    original_params = ydl.params.copy()
    observed = {}

    def process(info, download):
        observed["ydl"] = ydl
        observed["outtmpl"] = ydl.params["outtmpl"]
        observed["download"] = download
        return info

    monkeypatch.setattr(ydl, "process_ie_result", process)

    result = main._download_with_ydl(
        ydl,
        {"id": "123"},
        main._download_options(str(tmp_path / "video.%(ext)s"), fmt="best"),
    )

    assert result["id"] == "123"
    assert observed["ydl"] is ydl
    assert observed["outtmpl"] == {"default": str(tmp_path / "video.%(ext)s")}
    assert observed["download"] is True
    assert ydl.params == original_params


@pytest.mark.asyncio
async def test_upstream_runner_bounds_concurrency():
    runner = main.TikTokUpstreamRunner(concurrency=2, interval=0)
    lock = threading.Lock()
    active = 0
    peak = 0

    def work():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with lock:
            active -= 1

    await asyncio.gather(*(runner.run(work) for _ in range(6)))

    assert peak == 2


@pytest.mark.asyncio
async def test_upstream_runner_paces_job_starts():
    runner = main.TikTokUpstreamRunner(concurrency=2, interval=0.03)
    starts = []

    def work():
        starts.append(time.monotonic())

    await asyncio.gather(runner.run(work), runner.run(work))

    assert starts[1] - starts[0] >= 0.02


@pytest.mark.parametrize(
    "url",
    [
        "https://www.tiktok.com/share/video/7663781221171776789",
        "https://www.tiktokv.com/share/video/7663781221171776789",
    ],
)
def test_share_urls_use_upstream_numeric_id_without_redirect(monkeypatch, url):
    ie = TikTokIE(YoutubeDL({"quiet": True, "no_warnings": True}))
    monkeypatch.setattr(
        ie,
        "_extract_web_data_and_status",
        lambda _url, _video_id: ({"video": {}, "id": "7663781221171776789"}, 0),
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
            AssertionError("mobile API must not be called")
        ),
    )

    assert ie._real_extract(url) == {
        "id": "7663781221171776789"
    }


def test_photo_url_is_normalized_only_for_upstream_matching(monkeypatch):
    scraper = TikTokScraper()
    observed = {}

    def extract(_ie, url):
        observed["url"] = url
        return {"id": "7663781221171776789"}

    monkeypatch.setattr(TikTokIE, "extract", extract)

    info = scraper.extract_video(
        "https://www.tiktok.com/@creator/photo/7663781221171776789"
    )

    assert info["id"] == "7663781221171776789"
    assert observed["url"] == (
        "https://www.tiktok.com/@creator/video/7663781221171776789"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("proxy", "vpn_enabled", "expected"),
    [
        ("http://gluetun:8888", True, "proxy"),
        ("", True, "vpn_namespace"),
        ("", False, "direct"),
    ],
)
async def test_health_reports_egress_mode(monkeypatch, proxy, vpn_enabled, expected):
    monkeypatch.setenv("TIKTOK_PROXY_URL", proxy)
    monkeypatch.setattr(main.gluetun, "enabled", vpn_enabled)

    async def status():
        return {"status": "running" if vpn_enabled else "disabled"}

    monkeypatch.setattr(main.gluetun, "get_vpn_status", status)

    result = await main.health_check()

    assert result["egress_mode"] == expected
