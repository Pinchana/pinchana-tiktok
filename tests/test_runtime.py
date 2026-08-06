import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from pinchana_tiktok import main
from pinchana_tiktok.extractor import TikTokIE
from yt_dlp import YoutubeDL


def test_download_ydl_uses_configured_proxy(monkeypatch, tmp_path):
    monkeypatch.setenv("TIKTOK_PROXY_URL", "http://gluetun:8888")
    monkeypatch.setenv("TIKTOK_REQUEST_INTERVAL_SECONDS", "0.25")

    ydl = main._build_ydl(str(tmp_path / "%(id)s.%(ext)s"))

    assert ydl.params["proxy"] == "http://gluetun:8888"
    assert ydl.params["sleep_interval_requests"] == 0.25


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


def test_short_link_redirect_is_resolved_without_mobile_api(monkeypatch):
    ie = TikTokIE(YoutubeDL({"quiet": True, "no_warnings": True}))
    redirected_url = "https://www.tiktok.com/@creator/photo/7663781221171776789"

    monkeypatch.setattr(
        ie,
        "_download_webpage_handle",
        lambda *_args, **_kwargs: ("", SimpleNamespace(url=redirected_url)),
    )
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

    assert ie._real_extract("https://vm.tiktok.com/shortcode/") == {
        "id": "7663781221171776789"
    }


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
