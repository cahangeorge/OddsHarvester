from contextlib import asynccontextmanager
from datetime import date

import pytest

from oddsharvester.core.oddsportal_xhr import OddsPortalXHRSchemaError
from oddsharvester.core.scrapling_scraper import (
    MAX_DECODED_CACHE_BYTES,
    RequestedLeagueProvenanceError,
    ScraplingOddsPortalScraper,
    ScraplingProxyError,
    ScraplingUnavailableError,
    StaticListingRequiresBrowserError,
    _HTTPSessionLease,
    _parse_yyyymmdd,
)
from oddsharvester.utils.command_enum import CommandEnum
from oddsharvester.utils.proxy_manager import ProxyManager


@pytest.mark.asyncio
async def test_unexpected_match_programming_error_is_not_masked(monkeypatch):
    scraper = ScraplingOddsPortalScraper(engine="scrapling-http")

    async def broken_match(**_kwargs):
        raise AttributeError("internal bug")

    monkeypatch.setattr(scraper, "_scrape_match_xhr_with_failover", broken_match)

    with pytest.raises(AttributeError, match="internal bug"):
        await scraper._scrape_open_session(
            command=CommandEnum.UPCOMING_MATCHES,
            match_links=["https://www.oddsportal.com/football/a-b/c-d/#event"],
            sport="football",
            date_value=None,
            leagues=None,
            season=None,
            markets=["1x2"],
            max_pages=None,
            target_bookmaker=None,
            include_started=False,
        )


@pytest.mark.asyncio
async def test_multi_league_records_keep_listing_provenance(monkeypatch):
    scraper = ScraplingOddsPortalScraper(engine="scrapling-http")
    premier_link = "https://www.oddsportal.com/football/h2h/premier-home/premier-away/#one"
    brazil_link = "https://www.oddsportal.com/football/h2h/brazil-home/brazil-away/#two"

    async def collect_listing(*, page_url, **_kwargs):
        return [brazil_link] if "brazil" in page_url else [premier_link]

    async def scrape_match(*, match_link, **_kwargs):
        return {"match_link": match_link}

    monkeypatch.setattr(scraper, "_collect_listing_xhr", collect_listing)
    monkeypatch.setattr(scraper, "_scrape_match_xhr_with_failover", scrape_match)

    result = await scraper._scrape_open_session(
        command=CommandEnum.HISTORIC,
        match_links=None,
        sport="football",
        date_value=None,
        leagues=["england-premier-league", "brazil-serie-a"],
        season="2024",
        markets=["1x2"],
        max_pages=None,
        target_bookmaker=None,
        include_started=False,
    )

    assert result.success == [
        {"match_link": premier_link, "requested_league_slug": "england-premier-league"},
        {"match_link": brazil_link, "requested_league_slug": "brazil-serie-a"},
    ]


@pytest.mark.asyncio
async def test_cross_league_duplicate_link_fails_closed(monkeypatch):
    scraper = ScraplingOddsPortalScraper(engine="scrapling-http")
    duplicate = "https://www.oddsportal.com/football/h2h/home-team/away-team/#event"
    monkeypatch.setattr(
        scraper,
        "_collect_listing_xhr",
        lambda **_kwargs: _async_value([duplicate]),
    )

    with pytest.raises(RequestedLeagueProvenanceError, match="Requested league provenance is invalid"):
        await scraper._collect_links(
            command=CommandEnum.HISTORIC,
            sport="football",
            date_value=None,
            leagues=["england-premier-league", "brazil-serie-a"],
            season="2024",
            max_pages=None,
        )


@pytest.mark.asyncio
async def test_direct_match_links_do_not_fabricate_league_provenance(monkeypatch):
    scraper = ScraplingOddsPortalScraper(engine="scrapling-http")
    match_link = "https://www.oddsportal.com/football/h2h/home-team/away-team/#event"
    monkeypatch.setattr(
        scraper,
        "_scrape_match_xhr_with_failover",
        lambda **kwargs: _async_value({"match_link": kwargs["match_link"]}),
    )

    result = await scraper._scrape_open_session(
        command=CommandEnum.UPCOMING_MATCHES,
        match_links=[match_link],
        sport="football",
        date_value=None,
        leagues=["premier-league"],
        season=None,
        markets=["1x2"],
        max_pages=None,
        target_bookmaker=None,
        include_started=False,
    )

    assert result.success == [{"match_link": match_link}]


@pytest.mark.asyncio
async def test_decoded_cache_is_egress_scoped_and_byte_bounded(monkeypatch):
    scraper = ScraplingOddsPortalScraper(engine="scrapling-http")
    calls: list[str] = []

    async def fetch(_url, lease, *, report_success=False):
        calls.append(lease.proxy_key)
        return "encoded"

    monkeypatch.setattr(scraper, "_fetch_text_with_lease", fetch)
    monkeypatch.setattr(
        "oddsharvester.core.scrapling_scraper.decode_xhr_payload",
        lambda _raw: {"d": {"blob": "x" * (MAX_DECODED_CACHE_BYTES // 2)}},
    )

    for proxy_key in ("proxy-a", "proxy-a", "proxy-b", "proxy-c"):
        await scraper._fetch_decoded(
            "https://www.oddsportal.com/match-event/example.dat",
            _HTTPSessionLease(client=object(), proxy_key=proxy_key),
        )

    assert calls == ["proxy-a", "proxy-b", "proxy-c"]
    assert scraper._decoded_cache_bytes <= MAX_DECODED_CACHE_BYTES
    assert all(key[0] != "proxy-a" for key in scraper._decoded_cache)


@pytest.mark.asyncio
async def test_listing_anti_bot_response_triggers_fallback(monkeypatch):
    scraper = ScraplingOddsPortalScraper(engine="scrapling-http")

    @asynccontextmanager
    async def lease(**_kwargs):
        yield None

    monkeypatch.setattr(scraper, "_lease_http_session", lease)
    monkeypatch.setattr(
        scraper,
        "_fetch_text_with_lease",
        lambda _url, _lease: _async_value(
            "<html><title>Checking your browser</title><div class='cf-chl-test'></div></html>"
        ),
    )

    with pytest.raises(ScraplingUnavailableError, match="Anti-bot response detected"):
        await scraper._collect_links(
            command=CommandEnum.UPCOMING_MATCHES,
            sport="football",
            date_value="20991231",
            leagues=None,
            season=None,
            max_pages=None,
        )


@pytest.mark.asyncio
async def test_unrecognized_empty_listing_triggers_fallback(monkeypatch):
    scraper = ScraplingOddsPortalScraper(engine="scrapling-http")

    @asynccontextmanager
    async def lease(**_kwargs):
        yield None

    monkeypatch.setattr(scraper, "_lease_http_session", lease)
    monkeypatch.setattr(
        scraper,
        "_fetch_text_with_lease",
        lambda _url, _lease: _async_value("<html><body></body></html>"),
    )

    with pytest.raises(StaticListingRequiresBrowserError, match="listing XHR contract changed"):
        await scraper._collect_links(
            command=CommandEnum.UPCOMING_MATCHES,
            sport="football",
            date_value="20991231",
            leagues=None,
            season=None,
            max_pages=None,
        )


@pytest.mark.parametrize(
    "href",
    [
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/x",
        "https://evil.example/football/h2h/home-team-id/away-team-id/",
        "https://user:pass@www.oddsportal.com/football/h2h/home-team-id/away-team-id/",
        "https://www.oddsportal.com:444/football/h2h/home-team-id/away-team-id/",
        "https://www.oddsportal.com/football/h2h/home-team-id/away-team-id/?redirect=https://evil.example",
        "https://www.oddsportal.com/football/h2h/../../latest/meta-data/",
    ],
)
def test_listing_rejects_untrusted_match_links(href):
    scraper = ScraplingOddsPortalScraper(engine="scrapling-http")
    html = f'<div class="eventRow"><a href="{href}">match</a></div>'

    assert scraper._extract_match_links_from_html(html) == []


def test_listing_accepts_trusted_relative_match_link():
    scraper = ScraplingOddsPortalScraper(engine="scrapling-http")
    href = "/football/h2h/home-team-id/away-team-id/#matchId:1X2;2"
    html = f'<div class="eventRow"><a href="{href}">match</a></div>'

    assert scraper._extract_match_links_from_html(html) == [
        "https://www.oddsportal.com/football/h2h/home-team-id/away-team-id/#matchId:1X2;2"
    ]


@pytest.mark.asyncio
async def test_static_no_matches_text_without_xhr_attestation_falls_back(monkeypatch):
    scraper = ScraplingOddsPortalScraper(engine="scrapling-http")

    @asynccontextmanager
    async def lease(**_kwargs):
        yield None

    monkeypatch.setattr(scraper, "_lease_http_session", lease)
    monkeypatch.setattr(
        scraper,
        "_fetch_text_with_lease",
        lambda _url, _lease: _async_value("<html><body><p>No matches found</p></body></html>"),
    )
    monkeypatch.setattr(scraper, "_open_session", lambda: _async_value(None))
    monkeypatch.setattr(scraper, "aclose", lambda: _async_value(None))

    with pytest.raises(StaticListingRequiresBrowserError, match="listing XHR contract changed"):
        await scraper.scrape(
            command=CommandEnum.UPCOMING_MATCHES,
            sport="football",
            date_value="20991231",
            markets=["1x2"],
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"period": "1st_half"}, "full-time"),
        ({"bookies_filter": "crypto"}, "all-bookies"),
        ({"preview_submarkets_only": True}, "preview-only"),
    ],
)
@pytest.mark.asyncio
async def test_fast_path_rejects_browser_only_request_semantics(overrides, message):
    scraper = ScraplingOddsPortalScraper(engine="scrapling-http")

    with pytest.raises(ScraplingUnavailableError, match=message):
        await scraper.scrape(
            command=CommandEnum.UPCOMING_MATCHES,
            match_links=[],
            sport="football",
            markets=["1x2"],
            **overrides,
        )


def test_xhr_path_accepts_all_analysis_markets_and_rejects_unknown_market():
    scraper = ScraplingOddsPortalScraper(engine="scrapling-http")
    markets = [
        "1x2",
        "btts",
        "double_chance",
        "dnb",
        "over_under_1_5",
        "over_under_2_5",
        "over_under_3_5",
        "asian_handicap_-0_5",
    ]

    scraper._validate_supported_request(
        sport="football",
        markets=markets,
        scrape_odds_history=False,
        period="full_time",
        bookies_filter="all",
        preview_submarkets_only=False,
    )

    with pytest.raises(ScraplingUnavailableError, match="supports only these football markets"):
        scraper._validate_supported_request(
            sport="football",
            markets=[*markets, "correct_score_1_0"],
            scrape_odds_history=False,
            period="full_time",
            bookies_filter="all",
            preview_submarkets_only=False,
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("20260731", date(2026, 7, 31)),
        ("2026-07-31", date(2026, 7, 31)),
        ("31.07.2026", date(2026, 7, 31)),
    ],
)
def test_upcoming_date_parser_accepts_cli_api_and_display_formats(value, expected):
    assert _parse_yyyymmdd(value) == expected


@pytest.mark.asyncio
async def test_listing_timestamp_drift_triggers_browser_fallback(monkeypatch):
    scraper = ScraplingOddsPortalScraper(engine="scrapling-http")

    @asynccontextmanager
    async def lease(**_kwargs):
        yield None

    monkeypatch.setattr(scraper, "_lease_http_session", lease)
    monkeypatch.setattr(scraper, "_fetch_text_with_lease", lambda *_args, **_kwargs: _async_value("script"))
    monkeypatch.setattr(
        scraper,
        "_fetch_decoded",
        lambda *_args, **_kwargs: _async_value(
            {
                "d": {
                    "rows": [{"url": "/football/h2h/home-team-id/away-team-id/#Abc123"}],
                    "total": 1,
                    "onePage": 50,
                    "page": 1,
                    "pagination": {"pageCount": 1},
                }
            }
        ),
    )
    monkeypatch.setattr(
        "oddsharvester.core.scrapling_scraper.extract_page_bootstrap",
        lambda *_args, **_kwargs: ("https://www.oddsportal.com/ajax-listing/", "https://www.oddsportal.com/user-data"),
    )
    monkeypatch.setattr(
        "oddsharvester.core.scrapling_scraper.parse_user_data_script",
        lambda _script: {"bookiehash": "Xabc123"},
    )

    with pytest.raises(OddsPortalXHRSchemaError, match="parseable start timestamp"):
        await scraper._collect_listing_xhr(
            page_url="https://www.oddsportal.com/football/example/",
            date_filter=date(2026, 7, 31),
            max_pages=1,
        )


@pytest.mark.asyncio
async def test_http_session_pool_leases_entered_clients_and_closes_managers(monkeypatch):
    from scrapling import fetchers

    async def run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr("oddsharvester.core.scrapling_scraper.asyncio.to_thread", run_inline)
    created_managers = []

    class Client:
        def __init__(self):
            self.urls = []

        def get(self, url):
            self.urls.append(url)
            return type("Response", (), {"text": "fixture"})()

    class SessionManager:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.client = Client()
            self.closed = False
            created_managers.append(self)

        def __enter__(self):
            return self.client

        def __exit__(self, *_args):
            self.closed = True

    monkeypatch.setattr(fetchers, "FetcherSession", SessionManager)
    scraper = ScraplingOddsPortalScraper(engine="scrapling-http", concurrency_tasks=2)

    await scraper._open_session()

    assert await scraper._fetch_text("https://www.oddsportal.com/football/") == "fixture"
    assert len(created_managers) == 2
    assert all(manager.kwargs["impersonate"] == "chrome" for manager in created_managers)
    assert sum(len(manager.client.urls) for manager in created_managers) == 1

    await scraper.aclose()

    assert all(manager.closed for manager in created_managers)


@pytest.mark.asyncio
async def test_http_session_pool_assigns_sticky_authenticated_proxy_sessions(monkeypatch):
    from scrapling import fetchers

    created_kwargs = []

    class SessionManager:
        def __init__(self, **kwargs):
            created_kwargs.append(kwargs)

        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(fetchers, "FetcherSession", SessionManager)
    manager = ProxyManager(
        proxy_urls=[
            "http://first-user:first-pass@proxy-a.example:8000",
            "http://user%2Btwo:p%40ss@proxy-b.example:8001",
        ]
    )
    scraper = ScraplingOddsPortalScraper(
        engine="scrapling-http",
        concurrency_tasks=2,
        proxy_manager=manager,
    )

    await scraper._open_session()

    assert {item["proxy"] for item in created_kwargs} == {
        "http://first-user:first-pass@proxy-a.example:8000",
        "http://user%2Btwo:p%40ss@proxy-b.example:8001",
    }
    assert set(scraper._http_session_pools) == {
        "http://proxy-a.example:8000",
        "http://proxy-b.example:8001",
    }

    await scraper.aclose()


@pytest.mark.asyncio
async def test_single_egress_soft_blocks_apply_backoff_and_half_open_recovery(monkeypatch):
    clock = {"now": 100.0}
    sleeps: list[float] = []

    async def run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    async def advance_clock(delay):
        sleeps.append(delay)
        clock["now"] += delay

    class Client:
        def get(self, _url):
            return type(
                "Response",
                (),
                {"status": 200, "body": b"<!DOCTYPE html><html><body>soft block</body></html>"},
            )()

    monkeypatch.setattr("oddsharvester.core.scrapling_scraper.asyncio.to_thread", run_inline)
    monkeypatch.setattr("oddsharvester.core.scrapling_scraper.asyncio.sleep", advance_clock)
    monkeypatch.setattr(
        "oddsharvester.core.scrapling_scraper.time.monotonic",
        lambda: clock["now"],
    )
    scraper = ScraplingOddsPortalScraper(
        engine="scrapling-http",
        request_delay=0,
        egress_cooldown_base=1,
        egress_cooldown_max=4,
    )
    lease = _HTTPSessionLease(client=Client(), proxy_key="direct")

    for _ in range(3):
        with pytest.raises(ScraplingProxyError, match="HTML soft block"):
            await scraper._fetch_decoded("https://www.oddsportal.com/ajax-test", lease)

    assert scraper._is_egress_unhealthy("direct")
    assert scraper._egress_metadata() == {
        "mode": "direct_or_single",
        "max_consecutive_failures": 3,
        "cooldown_remaining_seconds": 4.0,
        "open_circuits": 1,
    }

    with pytest.raises(ScraplingProxyError, match="HTML soft block"):
        await scraper._fetch_decoded("https://www.oddsportal.com/ajax-test", lease)

    assert sleeps == [1.0, 2.0, 4.0]


@pytest.mark.asyncio
async def test_half_open_recovery_reserves_only_one_probe(monkeypatch):
    monkeypatch.setattr(
        "oddsharvester.core.scrapling_scraper.time.monotonic",
        lambda: 100.0,
    )
    scraper = ScraplingOddsPortalScraper(
        engine="scrapling-http",
        request_delay=0,
    )
    scraper._egress_failures["direct"] = 3
    scraper._egress_cooldown_until["direct"] = 100.0

    first_probe = await scraper._pace("direct")
    second_probe = await scraper._pace("direct")

    assert first_probe is True
    assert second_probe is False
    assert not scraper._is_egress_unhealthy(
        "direct",
        allow_half_open_probe=first_probe,
    )
    assert scraper._is_egress_unhealthy(
        "direct",
        allow_half_open_probe=second_probe,
    )


@pytest.mark.asyncio
async def test_stealth_session_uses_configured_proxy(monkeypatch):
    from scrapling import fetchers

    created_kwargs = []

    class StealthSession:
        def __init__(self, **kwargs):
            created_kwargs.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(fetchers, "AsyncStealthySession", StealthSession)
    manager = ProxyManager(proxy_url="http://user:pass@proxy.example:8000")
    scraper = ScraplingOddsPortalScraper(engine="scrapling-stealth", proxy_manager=manager)

    await scraper._open_session()

    assert created_kwargs[0]["proxy"] == "http://user:pass@proxy.example:8000"
    assert scraper._stealth_proxy_key == "http://proxy.example:8000"

    await scraper.aclose()


@pytest.mark.asyncio
async def test_partial_http_session_initialization_closes_entered_managers(monkeypatch):
    from scrapling import fetchers

    created_managers = []

    class SessionManager:
        def __init__(self, **_kwargs):
            self.closed = False
            created_managers.append(self)

        def __enter__(self):
            if len(created_managers) == 2:
                raise RuntimeError("second session failed")
            return object()

        def __exit__(self, *_args):
            self.closed = True

    monkeypatch.setattr(fetchers, "FetcherSession", SessionManager)
    scraper = ScraplingOddsPortalScraper(engine="scrapling-http", concurrency_tasks=2)

    with pytest.raises(RuntimeError, match="second session failed"):
        await scraper.scrape(
            command=CommandEnum.UPCOMING_MATCHES,
            sport="football",
            match_links=["https://www.oddsportal.com/football/example/"],
            markets=["1x2"],
        )

    assert created_managers[0].closed is True
    assert scraper._http_sessions == []
    assert scraper._http_session_pools == {}


@pytest.mark.asyncio
async def test_http_session_cleanup_continues_after_one_manager_fails(monkeypatch):
    from scrapling import fetchers

    created_managers = []

    class SessionManager:
        def __init__(self, **_kwargs):
            self.closed = False
            created_managers.append(self)

        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            self.closed = True
            if self is created_managers[0]:
                raise RuntimeError("close failed")

    monkeypatch.setattr(fetchers, "FetcherSession", SessionManager)
    scraper = ScraplingOddsPortalScraper(engine="scrapling-http", concurrency_tasks=2)
    await scraper._open_session()

    await scraper.aclose()

    assert all(manager.closed for manager in created_managers)
    assert scraper._http_sessions == []
    assert scraper._http_session_pools == {}


async def _async_value(value):
    return value


def test_response_text_callable_non_string_is_always_string():
    from oddsharvester.core.scrapling_scraper import _response_text

    class Response:
        def text(self):
            return 42

    assert _response_text(Response()) == "42"


def test_response_text_callable_bytes_decodes_to_string():
    from oddsharvester.core.scrapling_scraper import _response_text

    class Response:
        def text(self):
            return b"fixture"

    assert _response_text(Response()) == "fixture"


def test_response_text_falls_back_to_body_when_text_handler_is_empty():
    from oddsharvester.core.scrapling_scraper import _response_text

    class EmptyTextHandler:
        def __call__(self):
            return ""

    class Response:
        text = EmptyTextHandler()
        body = b"<html><body>fixture</body></html>"

    assert _response_text(Response()) == "<html><body>fixture</body></html>"
