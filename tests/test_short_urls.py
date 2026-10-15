import httpx
import pytest
from yt_dlp.utils import ExtractorError

from pinchana_tiktok import api, main
from pinchana_tiktok.api import TikTokScraper, is_short_tiktok_url


@pytest.fixture
def mock_redirects(monkeypatch):
    real_client = httpx.Client
    requests = []
    options = {}

    def install(handler):
        def trace(request):
            requests.append(str(request.url))
            return handler(request)

        def client(**kwargs):
            options.update(kwargs)
            # Record the production proxy option without making real proxy I/O.
            kwargs.pop("proxy", None)
            return real_client(transport=httpx.MockTransport(trace), **kwargs)

        monkeypatch.setattr(api.httpx, "Client", client)
        return requests, options

    return install


@pytest.mark.parametrize("direct_web", ["false", "true"])
@pytest.mark.parametrize("kind", ["video", "photo"])
@pytest.mark.parametrize(
    ("url", "requested"),
    [
        ("https://www.tt.site/t/ZGdC5nG4S/", "https://www.tt.site/t/ZGdC5nG4S/"),
        ("https://tt.site/t/ZGdC5nG4S/", "https://www.tt.site/t/ZGdC5nG4S/"),
        ("http://tt.site/t/ZGdC5nG4S/", "https://www.tt.site/t/ZGdC5nG4S/"),
        ("https://vm.tiktok.com/short", "https://vm.tiktok.com/short"),
        ("https://vt.tiktok.com/short", "https://vt.tiktok.com/short"),
        ("https://www.tiktok.com/t/short", "https://www.tiktok.com/t/short"),
        ("https://t.tiktok.com/short", "https://t.tiktok.com/short"),
    ],
)
def test_short_resolver_stops_before_post(
    monkeypatch, mock_redirects, direct_web, kind, url, requested,
):
    canonical = f"https://www.tiktok.com/@creator/{kind}/7696594552634739998?_t=track"
    requests, options = mock_redirects(
        lambda _request: httpx.Response(301, headers={"Location": canonical})
    )
    monkeypatch.setenv("TIKTOK_DIRECT_WEB_PRIMARY", direct_web)
    monkeypatch.setenv("TIKTOK_PROXY_URL", "http://gluetun:8888")

    scraper = TikTokScraper()
    assert scraper.resolve_short_url(url) == canonical
    assert requests == [requested]
    assert options["follow_redirects"] is False
    assert options["proxy"] == "http://gluetun:8888"
    assert scraper._direct_web_primary is (direct_web == "true")
    assert main.canonicalize_tiktok_url(canonical) == canonical.split("?")[0]


@pytest.mark.parametrize(
    "url",
    [
        "https://tt.site.evil.example/t/short",
        "https://evil.tt.site/t/short",
        "https://www.tt.site@evil.example/t/short",
        "https://user@www.tt.site/t/short",
        "https://www.tt.site:8443/t/short",
        "https://www.tt.site:bad/t/short",
        "https://www.tt.site/anything/short",
        "https://evil.example/t/short?url=https://vm.tiktok.com/short",
        "https://www.tiktok.com/@creator/video/123?share=/t/short",
        "file://www.tt.site/t/short",
    ],
)
def test_invalid_input_is_rejected_before_network(mock_redirects, url):
    requests, _ = mock_redirects(
        lambda _request: pytest.fail("invalid input must not make a request")
    )
    assert not is_short_tiktok_url(url)
    with pytest.raises(ExtractorError, match="Unsupported TikTok short URL"):
        TikTokScraper().resolve_short_url(url)
    assert requests == []


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/@creator/video/123",
        "https://www.tiktok.com.evil.example/@creator/video/123",
        "https://127.0.0.1/@creator/video/123",
        "https://internal.tiktok.com/@creator/video/123",
        "http://www.tiktok.com/@creator/video/123",
        "http://vm.tiktok.com/short",
        "https://user:password@www.tiktok.com/@creator/video/123",
        "https://www.tiktok.com:8443/@creator/video/123",
        "https://www.tiktok.com/",
        "https://www.tiktok.com/login",
        "https://www.tiktok.com/@creator/video/123/extra",
        "https://www.tt.site/login",
        "https://evil.tt.site/t/short",
        "javascript:alert(1)",
    ],
)
def test_untrusted_redirect_is_never_followed(mock_redirects, target):
    requests, _ = mock_redirects(
        lambda _request: httpx.Response(302, headers={"Location": target})
    )
    with pytest.raises(ExtractorError, match="Invalid TikTok short URL redirect"):
        TikTokScraper().resolve_short_url("https://www.tt.site/t/short")
    assert requests == ["https://www.tt.site/t/short"]


def test_known_redirect_chain_is_bounded_and_preserves_query(mock_redirects):
    redirects = {
        "https://www.tt.site/t/start?share=1": "/t/second?share=2",
        "https://www.tt.site/t/second?share=2": "https://vm.tiktok.com/third",
        "https://vm.tiktok.com/third": "//www.tiktok.com/@creator/photo/123?_r=1",
    }
    requests, _ = mock_redirects(
        lambda request: httpx.Response(301, headers={"Location": redirects[str(request.url)]})
    )
    assert TikTokScraper().resolve_short_url("https://www.tt.site/t/start?share=1") == (
        "https://www.tiktok.com/@creator/photo/123?_r=1"
    )
    assert requests == list(redirects)


def test_redirect_loop_stops_before_repeating_request(mock_redirects):
    requests, _ = mock_redirects(
        lambda _request: httpx.Response(301, headers={"Location": "/t/start"})
    )
    with pytest.raises(ExtractorError, match="redirect loop"):
        TikTokScraper().resolve_short_url("https://www.tt.site/t/start")
    assert len(requests) == 1


def test_redirect_limit(mock_redirects):
    requests, _ = mock_redirects(
        lambda request: httpx.Response(301, headers={"Location": str(request.url) + "x"})
    )
    with pytest.raises(ExtractorError, match="Too many"):
        TikTokScraper().resolve_short_url("https://www.tt.site/t/start")
    assert len(requests) == 3


@pytest.mark.parametrize("status, headers", [(200, {}), (301, {})])
def test_missing_redirect_has_clear_error(mock_redirects, status, headers):
    mock_redirects(lambda _request: httpx.Response(status, headers=headers))
    with pytest.raises(ExtractorError, match="did not redirect"):
        TikTokScraper().resolve_short_url("https://www.tt.site/t/start")


@pytest.mark.parametrize(("status", "error_type"), [
    (403, main.RateLimitError),
    (404, main.MediaNotFoundError),
    (429, main.RateLimitError),
    (500, main.UpstreamUnavailableError),
    (502, main.UpstreamUnavailableError),
    (503, main.UpstreamUnavailableError),
    (504, main.UpstreamUnavailableError),
    (520, main.UpstreamUnavailableError),
])
def test_http_failures_keep_existing_service_classification(mock_redirects, status, error_type):
    requests, _ = mock_redirects(lambda _request: httpx.Response(status))
    url = "https://www.tt.site/t/start"
    with pytest.raises(ExtractorError, match=f"HTTP Error {status}") as caught:
        TikTokScraper().resolve_short_url(url)
    error = main._request_error("short_url", caught.value, url=url)
    assert error.status_code == status
    assert isinstance(main._classify_extraction_error(error), error_type)
    assert len(requests) == (2 if status >= 500 else 1)


@pytest.mark.parametrize("failure", [500, 502, 503, 504, httpx.ReadTimeout, httpx.ConnectError])
@pytest.mark.parametrize("kind", ["video", "photo"])
def test_tt_site_outage_uses_same_code_on_legacy_host(mock_redirects, failure, kind):
    canonical = f"https://www.tiktok.com/@creator/{kind}/7695429756879195425"

    def handler(request):
        if request.url.host == "www.tt.site":
            if isinstance(failure, int):
                return httpx.Response(failure)
            raise failure("temporary transport failure", request=request)
        assert str(request.url) == "https://vm.tiktok.com/ZGdC5uWuP/?share=1"
        return httpx.Response(301, headers={"Location": canonical})

    requests, _ = mock_redirects(handler)
    assert TikTokScraper().resolve_short_url("https://tt.site/t/ZGdC5uWuP?share=1") == canonical
    assert requests == [
        "https://www.tt.site/t/ZGdC5uWuP?share=1",
        "https://vm.tiktok.com/ZGdC5uWuP/?share=1",
    ]


def test_fallback_does_not_follow_untrusted_destination(mock_redirects):
    def handler(request):
        if request.url.host == "www.tt.site":
            return httpx.Response(502)
        return httpx.Response(301, headers={"Location": "https://evil.example/@creator/video/123"})

    requests, _ = mock_redirects(handler)
    with pytest.raises(ExtractorError, match="Invalid TikTok short URL redirect"):
        TikTokScraper().resolve_short_url("https://www.tt.site/t/start")
    assert requests == ["https://www.tt.site/t/start", "https://vm.tiktok.com/start/"]


def test_fallback_preserves_total_request_limit(mock_redirects):
    def handler(request):
        if request.url.path == "/t/start":
            return httpx.Response(301, headers={"Location": "/t/second"})
        if request.url.host == "www.tt.site":
            return httpx.Response(502)
        return httpx.Response(301, headers={"Location": "https://vt.tiktok.com/third"})

    requests, _ = mock_redirects(handler)
    with pytest.raises(ExtractorError, match="Too many"):
        TikTokScraper().resolve_short_url("https://www.tt.site/t/start")
    assert len(requests) == 3
    assert requests[-1] == "https://vm.tiktok.com/second/"


def test_outage_on_final_allowed_request_keeps_http_status(mock_redirects):
    def handler(request):
        if request.url.path == "/t/start":
            return httpx.Response(301, headers={"Location": "/t/second"})
        if request.url.path == "/t/second":
            return httpx.Response(301, headers={"Location": "/t/third"})
        return httpx.Response(502)

    requests, _ = mock_redirects(handler)
    with pytest.raises(ExtractorError, match="HTTP Error 502"):
        TikTokScraper().resolve_short_url("https://www.tt.site/t/start")
    assert len(requests) == 3


@pytest.mark.asyncio
async def test_persistent_short_host_outage_returns_503_after_one_request_time_retry(
    monkeypatch, mock_redirects,
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    class Runner:
        async def run(self, function, *args, **_kwargs):
            return function(*args)

    requests, _ = mock_redirects(lambda _request: httpx.Response(502))
    rotate = AsyncMock()
    monkeypatch.setattr(main, "UPSTREAM_RUNNER", Runner())
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(main, "trigger_rotation", rotate)
    monkeypatch.setenv("VPN_ENABLED", "1")

    with pytest.raises(main.HTTPException) as caught:
        await main._process_scrape_request(SimpleNamespace(url="https://www.tt.site/t/start"))
    assert caught.value.status_code == 503
    assert caught.value.detail == {
        "code": "upstream_unavailable",
        "message": "TikTok is temporarily unavailable",
        "stage": "short_url",
    }
    assert requests == ["https://www.tt.site/t/start", "https://vm.tiktok.com/start/"] * 2
    rotate.assert_not_awaited()


def test_transport_failure_is_preserved(mock_redirects):
    def timeout(request):
        raise httpx.ReadTimeout("timeout", request=request)

    mock_redirects(timeout)
    with pytest.raises(httpx.ReadTimeout):
        TikTokScraper().resolve_short_url("https://www.tt.site/t/start")


def test_in_process_plugin_advertises_tt_site():
    plugin = main.registry.get("tiktok")
    assert "tt.site" in plugin.route_patterns
