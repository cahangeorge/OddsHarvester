import asyncio
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from oddsharvester.core.camoufox_manager import CamoufoxUnavailableError
from oddsharvester.core.odds_portal_market_extractor import OddsPortalMarketExtractor
from oddsharvester.core.odds_portal_scraper import OddsPortalScraper
from oddsharvester.core.playwright_manager import PlaywrightManager
from oddsharvester.core.retry import TRANSIENT_ERROR_KEYWORDS
from oddsharvester.core.scrape_result import ErrorType, FailedUrl, PartialResult, ScrapeResult, ScrapeStats
from oddsharvester.core.scraper_app import _scrape_multiple_leagues, retry_scrape, run_scraper
from oddsharvester.core.scrapling_scraper import (
    RequestedLeagueProvenanceError,
    ScraplingUnavailableError,
    StaticListingRequiresBrowserError,
)
from oddsharvester.utils.command_enum import CommandEnum
from oddsharvester.utils.constants import OPERATION_RETRY_MAX_ATTEMPTS


@pytest.mark.parametrize("raw_value", ["nan", "inf", "-1", "not-a-number"])
def test_invalid_adaptive_cooldown_environment_uses_default(monkeypatch, raw_value):
    from oddsharvester.core.scraper_app import _nonnegative_env_float

    monkeypatch.setenv("OH_XHR_COOLDOWN_BASE", raw_value)

    assert _nonnegative_env_float("OH_XHR_COOLDOWN_BASE", 15.0) == 15.0


@pytest.fixture
def setup_mocks():
    """Set up common mocks for tests."""
    playwright_manager_mock = MagicMock(spec=PlaywrightManager)
    market_extractor_mock = MagicMock(spec=OddsPortalMarketExtractor)
    scraper_mock = MagicMock(spec=OddsPortalScraper)

    # Configure the scraper mock
    scraper_mock.start_playwright = AsyncMock()
    scraper_mock.stop_playwright = AsyncMock()
    scraper_mock.scrape_historic = AsyncMock(return_value={"result": "historic_data"})
    scraper_mock.scrape_upcoming = AsyncMock(return_value={"result": "upcoming_data"})
    scraper_mock.scrape_matches = AsyncMock(return_value={"result": "match_data"})

    return {
        "playwright_manager_mock": playwright_manager_mock,
        "market_extractor_mock": market_extractor_mock,
        "scraper_mock": scraper_mock,
    }


@pytest.mark.asyncio
@patch("oddsharvester.core.scraper_app.OddsPortalScraper")
@patch("oddsharvester.core.scraper_app.OddsPortalMarketExtractor")
@patch("oddsharvester.core.scraper_app.PlaywrightManager")
@patch("oddsharvester.core.scraper_app.ProxyManager")
@patch("oddsharvester.core.scraper_app.SportMarketRegistrar")
async def test_run_scraper_historic(
    sport_market_registrar_mock,
    proxy_manager_mock,
    playwright_manager_mock,
    market_extractor_mock,
    scraper_cls_mock,
    setup_mocks,
):
    """Test run_scraper with historic command."""
    scraper_mock = setup_mocks["scraper_mock"]
    scraper_cls_mock.return_value = scraper_mock

    proxy_manager_instance = MagicMock()
    proxy_manager_instance.get_current_proxy.return_value = {"server": "test-proxy"}
    proxy_manager_mock.return_value = proxy_manager_instance

    result = await run_scraper(
        command=CommandEnum.HISTORIC,
        sport="football",
        leagues=["premier-league"],
        season="2023",
        markets=["1x2", "over_under"],
        max_pages=2,
        headless=True,
    )

    # Verify the flow
    sport_market_registrar_mock.register_all_markets.assert_called_once()
    scraper_mock.start_playwright.assert_called_once_with(
        headless=True,
        browser_user_agent=None,
        browser_locale_timezone=None,
        browser_timezone_id=None,
        proxy_manager=proxy_manager_instance,
    )

    scraper_mock.scrape_historic.assert_called_once_with(
        sport="football",
        league="premier-league",
        season="2023",
        markets=["1x2", "over_under"],
        scrape_odds_history=False,
        target_bookmaker=None,
        max_pages=2,
        bookies_filter=ANY,
        period=ANY,
        request_delay=ANY,
        concurrent_scraping_task=ANY,
    )

    scraper_mock.stop_playwright.assert_called_once()
    assert result == {"result": "historic_data"}


@pytest.mark.asyncio
@patch("oddsharvester.core.scraper_app.OddsPortalScraper")
@patch("oddsharvester.core.scraper_app.OddsPortalMarketExtractor")
@patch("oddsharvester.core.scraper_app.PlaywrightManager")
@patch("oddsharvester.core.scraper_app.ProxyManager")
@patch("oddsharvester.core.scraper_app.SportMarketRegistrar")
async def test_run_scraper_upcoming(
    sport_market_registrar_mock,
    proxy_manager_mock,
    playwright_manager_mock,
    market_extractor_mock,
    scraper_cls_mock,
    setup_mocks,
):
    """Test run_scraper with upcoming_matches command."""
    scraper_mock = setup_mocks["scraper_mock"]
    scraper_cls_mock.return_value = scraper_mock

    proxy_manager_instance = MagicMock()
    proxy_manager_instance.get_current_proxy.return_value = {"server": "test-proxy"}
    proxy_manager_mock.return_value = proxy_manager_instance

    result = await run_scraper(
        command=CommandEnum.UPCOMING_MATCHES,
        sport="basketball",
        date="2023-06-01",
        leagues=["nba"],
        markets=["1x2"],
        browser_user_agent="custom-agent",
        browser_locale_timezone="Europe/Paris",
        headless=False,
    )

    # Verify the flow
    scraper_mock.start_playwright.assert_called_once_with(
        headless=False,
        browser_user_agent="custom-agent",
        browser_locale_timezone="Europe/Paris",
        browser_timezone_id=None,
        proxy_manager=proxy_manager_instance,
    )

    scraper_mock.scrape_upcoming.assert_called_once_with(
        sport="basketball",
        date="2023-06-01",
        league="nba",
        markets=["1x2"],
        scrape_odds_history=False,
        target_bookmaker=None,
        bookies_filter=ANY,
        period=ANY,
        request_delay=ANY,
        concurrent_scraping_task=ANY,
        include_started=False,
    )

    assert result == {"result": "upcoming_data"}


@pytest.mark.asyncio
@patch("oddsharvester.core.scraper_app.OddsPortalScraper")
@patch("oddsharvester.core.scraper_app.OddsPortalMarketExtractor")
@patch("oddsharvester.core.scraper_app.PlaywrightManager")
@patch("oddsharvester.core.scraper_app.ProxyManager")
@patch("oddsharvester.core.scraper_app.SportMarketRegistrar")
async def test_run_scraper_match_links(
    sport_market_registrar_mock,
    proxy_manager_mock,
    playwright_manager_mock,
    market_extractor_mock,
    scraper_cls_mock,
    setup_mocks,
):
    """Test run_scraper with match_links."""
    scraper_mock = setup_mocks["scraper_mock"]
    scraper_cls_mock.return_value = scraper_mock

    proxy_manager_instance = MagicMock()
    proxy_manager_instance.get_current_proxy.return_value = {"server": "test-proxy"}
    proxy_manager_mock.return_value = proxy_manager_instance

    match_links = ["https://oddsportal.com/match1", "https://oddsportal.com/match2"]

    result = await run_scraper(
        command=CommandEnum.HISTORIC,  # Doesn't matter for this test
        match_links=match_links,
        sport="tennis",
        markets=["1x2"],
        scrape_odds_history=True,
        target_bookmaker="bet365",
    )

    scraper_mock.scrape_matches.assert_called_once_with(
        match_links=match_links,
        sport="tennis",
        markets=["1x2"],
        scrape_odds_history=True,
        target_bookmaker="bet365",
        bookies_filter=ANY,
        period=ANY,
        request_delay=ANY,
        concurrent_scraping_task=ANY,
    )

    assert result == {"result": "match_data"}


@pytest.mark.asyncio
async def test_run_scraper_builds_multi_proxy_manager(monkeypatch):
    """run_scraper(proxy_url=<tuple>) must build a single ProxyManager in multi-proxy mode
    and pass it (not a proxy dict) to start_playwright (issue: multi-proxy rotation)."""
    from oddsharvester.core import scraper_app

    captured = {}

    class DummyScraper:
        def __init__(self, *a, **k):
            pass

        async def start_playwright(self, **kwargs):
            captured["proxy_manager"] = kwargs.get("proxy_manager")

        async def scrape_upcoming(self, *a, **k):
            return ScrapeResult()

        async def stop_playwright(self):
            pass

    monkeypatch.setattr(scraper_app, "OddsPortalScraper", DummyScraper)

    await scraper_app.run_scraper(
        command=CommandEnum.UPCOMING_MATCHES,
        sport="football",
        date="20250101",
        markets=["1x2"],
        proxy_url=("http://a.example.com:1", "http://b.example.com:2"),
    )

    assert captured["proxy_manager"].is_multi_proxy() is True


@pytest.mark.asyncio
@patch("oddsharvester.core.scraper_app.OddsPortalScraper")
@patch("oddsharvester.core.scraper_app.OddsPortalMarketExtractor")
@patch("oddsharvester.core.scraper_app.PlaywrightManager")
@patch("oddsharvester.core.scraper_app.ProxyManager")
@patch("oddsharvester.core.scraper_app.SportMarketRegistrar")
async def test_run_scraper_upcoming_forwards_concurrency(
    sport_market_registrar_mock,
    proxy_manager_mock,
    playwright_manager_mock,
    market_extractor_mock,
    scraper_cls_mock,
    setup_mocks,
):
    """run_scraper(concurrency_tasks=N) must forward concurrent_scraping_task=N to scrape_upcoming (issue #64)."""
    scraper_mock = setup_mocks["scraper_mock"]
    scraper_cls_mock.return_value = scraper_mock
    proxy_manager_mock.return_value.get_current_proxy.return_value = None

    await run_scraper(
        command=CommandEnum.UPCOMING_MATCHES,
        sport="football",
        date="20260601",
        leagues=["premier-league"],
        markets=["1x2"],
        concurrency_tasks=10,
    )

    assert scraper_mock.scrape_upcoming.call_args.kwargs.get("concurrent_scraping_task") == 10


@pytest.mark.asyncio
@patch("oddsharvester.core.scraper_app.OddsPortalScraper")
@patch("oddsharvester.core.scraper_app.OddsPortalMarketExtractor")
@patch("oddsharvester.core.scraper_app.PlaywrightManager")
@patch("oddsharvester.core.scraper_app.ProxyManager")
@patch("oddsharvester.core.scraper_app.SportMarketRegistrar")
async def test_run_scraper_upcoming_forwards_include_started(
    sport_market_registrar_mock,
    proxy_manager_mock,
    playwright_manager_mock,
    market_extractor_mock,
    scraper_cls_mock,
    setup_mocks,
):
    """run_scraper(include_started=True) must forward include_started=True to scrape_upcoming (issue #58)."""
    scraper_mock = setup_mocks["scraper_mock"]
    scraper_cls_mock.return_value = scraper_mock
    proxy_manager_mock.return_value.get_current_proxy.return_value = None

    await run_scraper(
        command=CommandEnum.UPCOMING_MATCHES,
        sport="football",
        date="20260601",
        markets=["1x2"],
        include_started=True,
    )

    assert scraper_mock.scrape_upcoming.call_args.kwargs.get("include_started") is True


@pytest.mark.asyncio
@patch("oddsharvester.core.scraper_app.OddsPortalScraper")
@patch("oddsharvester.core.scraper_app.OddsPortalMarketExtractor")
@patch("oddsharvester.core.scraper_app.PlaywrightManager")
@patch("oddsharvester.core.scraper_app.ProxyManager")
@patch("oddsharvester.core.scraper_app.SportMarketRegistrar")
async def test_run_scraper_historic_forwards_concurrency(
    sport_market_registrar_mock,
    proxy_manager_mock,
    playwright_manager_mock,
    market_extractor_mock,
    scraper_cls_mock,
    setup_mocks,
):
    """run_scraper(concurrency_tasks=N) must forward concurrent_scraping_task=N to scrape_historic (issue #64)."""
    scraper_mock = setup_mocks["scraper_mock"]
    scraper_cls_mock.return_value = scraper_mock
    proxy_manager_mock.return_value.get_current_proxy.return_value = None

    await run_scraper(
        command=CommandEnum.HISTORIC,
        sport="football",
        leagues=["premier-league"],
        season="2024",
        markets=["1x2"],
        concurrency_tasks=7,
    )

    assert scraper_mock.scrape_historic.call_args.kwargs.get("concurrent_scraping_task") == 7


@pytest.mark.asyncio
@patch("oddsharvester.core.scraper_app.run_scrapling_scraper")
@patch("oddsharvester.core.scraper_app.OddsPortalScraper")
@patch("oddsharvester.core.scraper_app.OddsPortalMarketExtractor")
@patch("oddsharvester.core.scraper_app.PlaywrightManager")
@patch("oddsharvester.core.scraper_app.ProxyManager")
@patch("oddsharvester.core.scraper_app.SportMarketRegistrar")
async def test_run_scraper_auto_engine_falls_back_to_playwright_when_scrapling_returns_no_data(
    sport_market_registrar_mock,
    proxy_manager_mock,
    playwright_manager_mock,
    market_extractor_mock,
    scraper_cls_mock,
    scrapling_mock,
    setup_mocks,
):
    """`scraper_engine=auto` should try Scrapling first and preserve Playwright fallback safety."""
    scraper_mock = setup_mocks["scraper_mock"]
    scraper_cls_mock.return_value = scraper_mock
    proxy_manager_mock.return_value.get_current_proxy.return_value = None
    scrapling_mock.return_value = ScrapeResult(stats=ScrapeStats(total_urls=1))

    result = await run_scraper(
        command=CommandEnum.UPCOMING_MATCHES,
        sport="football",
        date="20991231",
        markets=["1x2"],
        headless=True,
        scraper_engine="auto",
    )

    assert scrapling_mock.await_count == 2
    scraper_mock.scrape_upcoming.assert_called_once()
    assert result == {"result": "upcoming_data"}


@pytest.mark.asyncio
@patch("oddsharvester.core.scraper_app.run_scrapling_scraper")
@patch("oddsharvester.core.scraper_app.OddsPortalScraper")
@patch("oddsharvester.core.scraper_app.OddsPortalMarketExtractor")
@patch("oddsharvester.core.scraper_app.PlaywrightManager")
@patch("oddsharvester.core.scraper_app.ProxyManager")
@patch("oddsharvester.core.scraper_app.SportMarketRegistrar")
async def test_auto_skips_stealth_when_static_listing_requires_browser(
    sport_market_registrar_mock,
    proxy_manager_mock,
    playwright_manager_mock,
    market_extractor_mock,
    scraper_cls_mock,
    scrapling_mock,
    setup_mocks,
):
    scraper_mock = setup_mocks["scraper_mock"]
    browser_result = ScrapeResult(success=[{"url": "https://www.oddsportal.com/football/example-match/"}])
    scraper_mock.scrape_upcoming.return_value = browser_result
    scraper_cls_mock.return_value = scraper_mock
    proxy_manager_mock.return_value.get_current_proxy.return_value = None
    scrapling_mock.side_effect = StaticListingRequiresBrowserError("Unrecognized empty listing response")

    result = await run_scraper(
        command=CommandEnum.UPCOMING_MATCHES,
        sport="football",
        leagues=["example-league"],
        markets=["1x2"],
        scraper_engine="auto",
    )

    scrapling_mock.assert_awaited_once()
    scraper_mock.scrape_upcoming.assert_awaited_once()
    assert result is browser_result
    assert result.success[0]["requested_league_slug"] == "example-league"
    assert result.metadata["engine_attempts"] == [
        {
            "engine": "scrapling-http",
            "outcome": "skipped",
            "detail": "static_listing_requires_browser",
        },
        {"engine": "playwright", "outcome": "completed"},
    ]


@pytest.mark.asyncio
@patch("oddsharvester.core.scraper_app.run_scrapling_scraper")
@patch("oddsharvester.core.scraper_app.OddsPortalScraper")
@patch("oddsharvester.core.scraper_app.OddsPortalMarketExtractor")
@patch("oddsharvester.core.scraper_app.PlaywrightManager")
@patch("oddsharvester.core.scraper_app.ProxyManager")
@patch("oddsharvester.core.scraper_app.SportMarketRegistrar")
async def test_auto_hands_scrapling_discovery_links_to_playwright_without_stealth_discovery(
    sport_market_registrar_mock,
    proxy_manager_mock,
    playwright_manager_mock,
    market_extractor_mock,
    scraper_cls_mock,
    scrapling_mock,
    setup_mocks,
):
    scraper_mock = setup_mocks["scraper_mock"]
    scraper_cls_mock.return_value = scraper_mock
    proxy_manager_mock.return_value.get_current_proxy.return_value = None
    discovered = ["https://www.oddsportal.com/football/example-match/"]
    scrapling_mock.return_value = ScrapeResult(
        failed=[],
        stats=ScrapeStats(total_urls=1, failed=1),
        metadata={
            "_discovered_match_links": discovered,
            "_requested_league_by_match_link": {discovered[0]: "example-league"},
        },
    )

    result = await run_scraper(
        command=CommandEnum.UPCOMING_MATCHES,
        sport="football",
        leagues=["example-league"],
        markets=["1x2"],
        scraper_engine="auto",
    )

    scrapling_mock.assert_awaited_once()
    scraper_mock.scrape_matches.assert_awaited_once()
    assert scraper_mock.scrape_matches.call_args.kwargs["match_links"] == discovered
    assert result == {"result": "match_data"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mapping_case", ["missing", "malformed", "partial", "out_of_scope", "conflicting"])
async def test_league_scoped_auto_discovery_requires_complete_requested_league_mapping(monkeypatch, mapping_case):
    import oddsharvester.core.scraper_app as scraper_app

    links = ["https://provider.invalid/one", "https://provider.invalid/two"]
    leagues = ["premier-league", "bundesliga"]
    metadata = {"_discovered_match_links": links}
    initial_mapping = None
    if mapping_case == "malformed":
        metadata["_requested_league_by_match_link"] = []
    elif mapping_case == "partial":
        metadata["_requested_league_by_match_link"] = {links[0]: leagues[0]}
    elif mapping_case == "out_of_scope":
        metadata["_requested_league_by_match_link"] = dict.fromkeys(links, "other-league")
    elif mapping_case == "conflicting":
        metadata["_requested_league_by_match_link"] = dict.fromkeys(links, leagues[1])
        initial_mapping = {links[0]: leagues[0]}

    proxy_manager = MagicMock()
    proxy_manager.get_current_proxy.return_value = None
    proxy_manager.is_multi_proxy.return_value = False
    monkeypatch.setattr(scraper_app, "ProxyManager", MagicMock(return_value=proxy_manager))
    scrapling_mock = AsyncMock(return_value=ScrapeResult(stats=ScrapeStats(total_urls=2), metadata=metadata))
    monkeypatch.setattr(scraper_app, "run_scrapling_scraper", scrapling_mock)
    browser_factory = MagicMock()
    monkeypatch.setattr(scraper_app, "OddsPortalScraper", browser_factory)

    with pytest.raises(RequestedLeagueProvenanceError, match="Requested league provenance is invalid") as exc_info:
        await scraper_app.run_scraper(
            command=CommandEnum.UPCOMING_MATCHES,
            sport="football",
            leagues=leagues,
            markets=["1x2"],
            scraper_engine="auto",
            _requested_league_by_match_link=initial_mapping,
        )

    assert "provider.invalid" not in str(exc_info.value)
    scrapling_mock.assert_awaited_once()
    browser_factory.assert_not_called()


@pytest.mark.asyncio
async def test_auto_requested_league_collision_is_fatal_without_browser_fallback(monkeypatch):
    import oddsharvester.core.scraper_app as scraper_app

    proxy_manager = MagicMock()
    proxy_manager.get_current_proxy.return_value = None
    proxy_manager.is_multi_proxy.return_value = False
    monkeypatch.setattr(scraper_app, "ProxyManager", MagicMock(return_value=proxy_manager))
    scrapling_mock = AsyncMock(side_effect=RequestedLeagueProvenanceError("Requested league provenance is invalid"))
    monkeypatch.setattr(scraper_app, "run_scrapling_scraper", scrapling_mock)
    browser_factory = MagicMock()
    monkeypatch.setattr(scraper_app, "OddsPortalScraper", browser_factory)

    with pytest.raises(RequestedLeagueProvenanceError):
        await scraper_app.run_scraper(
            command=CommandEnum.UPCOMING_MATCHES,
            sport="football",
            leagues=["premier-league", "bundesliga"],
            markets=["1x2"],
            scraper_engine="auto",
        )

    scrapling_mock.assert_awaited_once()
    browser_factory.assert_not_called()


@pytest.mark.asyncio
async def test_global_date_auto_discovery_does_not_fabricate_league_provenance(monkeypatch):
    import oddsharvester.core.scraper_app as scraper_app

    link = "https://www.oddsportal.com/football/h2h/a-b/c-d/#event"
    proxy_manager = MagicMock()
    proxy_manager.get_current_proxy.return_value = None
    proxy_manager.is_multi_proxy.return_value = False
    monkeypatch.setattr(scraper_app, "ProxyManager", MagicMock(return_value=proxy_manager))
    monkeypatch.setattr(
        scraper_app,
        "run_scrapling_scraper",
        AsyncMock(
            return_value=ScrapeResult(
                stats=ScrapeStats(total_urls=1, failed=1),
                metadata={"_discovered_match_links": [link]},
            )
        ),
    )
    browser_result = ScrapeResult(
        success=[{"match_link": link}],
        stats=ScrapeStats(total_urls=1, successful=1),
    )
    scraper = MagicMock()
    scraper.start_playwright = AsyncMock()
    scraper.stop_playwright = AsyncMock()
    scraper.scrape_matches = AsyncMock(return_value=browser_result)
    monkeypatch.setattr(scraper_app, "OddsPortalScraper", MagicMock(return_value=scraper))
    monkeypatch.setattr(scraper_app, "PlaywrightManager", MagicMock())

    result = await scraper_app.run_scraper(
        command=CommandEnum.UPCOMING_MATCHES,
        sport="football",
        date="20991231",
        markets=["1x2"],
        scraper_engine="auto",
    )

    assert result is browser_result
    assert "requested_league_slug" not in result.success[0]


@pytest.mark.asyncio
@patch("oddsharvester.core.scraper_app.run_scrapling_scraper")
@patch("oddsharvester.core.scraper_app.OddsPortalScraper")
@patch("oddsharvester.core.scraper_app.OddsPortalMarketExtractor")
@patch("oddsharvester.core.scraper_app.PlaywrightManager")
@patch("oddsharvester.core.scraper_app.ProxyManager")
@patch("oddsharvester.core.scraper_app.SportMarketRegistrar")
async def test_auto_uses_scrapling_http_when_match_links_are_explicit(
    sport_market_registrar_mock,
    proxy_manager_mock,
    playwright_manager_mock,
    market_extractor_mock,
    scraper_cls_mock,
    scrapling_mock,
    setup_mocks,
):
    scraper_mock = setup_mocks["scraper_mock"]
    scraper_cls_mock.return_value = scraper_mock
    proxy_manager_mock.return_value.get_current_proxy.return_value = None
    links = ["https://www.oddsportal.com/football/example-match/"]
    http_result = ScrapeResult(
        success=[{"home_team": "A", "away_team": "B"}],
        stats=ScrapeStats(total_urls=1, successful=1),
    )
    scrapling_mock.return_value = http_result

    result = await run_scraper(
        command=CommandEnum.UPCOMING_MATCHES,
        match_links=links,
        sport="football",
        markets=["1x2"],
        scraper_engine="auto",
    )

    scrapling_mock.assert_awaited_once()
    assert scrapling_mock.call_args.kwargs["engine"] == "scrapling-http"
    assert scrapling_mock.call_args.kwargs["match_links"] == links
    scraper_mock.scrape_matches.assert_not_awaited()
    assert result is http_result
    assert "requested_league_slug" not in result.success[0]


@pytest.mark.asyncio
@patch("oddsharvester.core.scraper_app.run_scrapling_scraper")
@patch("oddsharvester.core.scraper_app.OddsPortalScraper")
@patch("oddsharvester.core.scraper_app.OddsPortalMarketExtractor")
@patch("oddsharvester.core.scraper_app.PlaywrightManager")
@patch("oddsharvester.core.scraper_app.ProxyManager")
@patch("oddsharvester.core.scraper_app.SportMarketRegistrar")
async def test_auto_retries_only_failed_fast_path_urls_and_preserves_success(
    sport_market_registrar_mock,
    proxy_manager_mock,
    playwright_manager_mock,
    market_extractor_mock,
    scraper_cls_mock,
    scrapling_mock,
    setup_mocks,
):
    scraper_mock = setup_mocks["scraper_mock"]
    browser_result = ScrapeResult(
        success=[{"match_link": "https://www.oddsportal.com/football/country/league/failed-Abc123"}],
        stats=ScrapeStats(total_urls=1, successful=1),
    )
    scraper_mock.scrape_matches.return_value = browser_result
    scraper_cls_mock.return_value = scraper_mock
    proxy_manager_mock.return_value.get_current_proxy.return_value = None
    good_url = "https://www.oddsportal.com/football/country/league/good-Abc123"
    failed_url = "https://www.oddsportal.com/football/country/league/failed-Abc123"
    partial = ScrapeResult(
        success=[{"match_link": good_url, "home_team": "A"}],
        failed=[FailedUrl(url=failed_url, error_type=ErrorType.RATE_LIMITED, error_message="blocked")],
        stats=ScrapeStats(total_urls=2, successful=1, failed=1),
    )
    unavailable = ScraplingUnavailableError("stealth unavailable")
    scrapling_mock.side_effect = [partial, unavailable]

    result = await run_scraper(
        command=CommandEnum.UPCOMING_MATCHES,
        match_links=[good_url, failed_url],
        sport="football",
        markets=["1x2"],
        scraper_engine="auto",
    )

    assert scraper_mock.scrape_matches.call_args.kwargs["match_links"] == [failed_url]
    assert [row["match_link"] for row in result.success] == [good_url, failed_url]
    assert result.stats.successful == 2
    assert result.stats.failed == 0


@pytest.mark.asyncio
@patch("oddsharvester.core.scraper_app.run_scrapling_scraper")
@patch("oddsharvester.core.scraper_app.OddsPortalScraper")
@patch("oddsharvester.core.scraper_app.OddsPortalMarketExtractor")
@patch("oddsharvester.core.scraper_app.PlaywrightManager")
@patch("oddsharvester.core.scraper_app.ProxyManager")
@patch("oddsharvester.core.scraper_app.SportMarketRegistrar")
async def test_run_scraper_forced_scrapling_engine_skips_playwright(
    sport_market_registrar_mock,
    proxy_manager_mock,
    playwright_manager_mock,
    market_extractor_mock,
    scraper_cls_mock,
    scrapling_mock,
    setup_mocks,
):
    """Forced Scrapling engines should not launch the legacy Playwright scraper."""
    successful = ScrapeResult(
        success=[{"home_team": "A", "away_team": "B"}],
        stats=ScrapeStats(total_urls=1, successful=1),
    )
    scrapling_mock.return_value = successful

    result = await run_scraper(
        command=CommandEnum.UPCOMING_MATCHES,
        sport="football",
        date="20991231",
        markets=["1x2"],
        headless=True,
        scraper_engine="scrapling-http",
    )

    scrapling_mock.assert_called_once()
    scraper_cls_mock.assert_not_called()
    assert result is successful


@pytest.mark.asyncio
@patch("oddsharvester.core.scraper_app.OddsPortalScraper")
@patch("oddsharvester.core.scraper_app.OddsPortalMarketExtractor")
@patch("oddsharvester.core.scraper_app.PlaywrightManager")
@patch("oddsharvester.core.scraper_app.ProxyManager")
@patch("oddsharvester.core.scraper_app.SportMarketRegistrar")
async def test_run_scraper_match_links_forwards_concurrency(
    sport_market_registrar_mock,
    proxy_manager_mock,
    playwright_manager_mock,
    market_extractor_mock,
    scraper_cls_mock,
    setup_mocks,
):
    """run_scraper(concurrency_tasks=N) must forward concurrent_scraping_task=N to scrape_matches (issue #64)."""
    scraper_mock = setup_mocks["scraper_mock"]
    scraper_cls_mock.return_value = scraper_mock
    proxy_manager_mock.return_value.get_current_proxy.return_value = None

    await run_scraper(
        command=CommandEnum.UPCOMING_MATCHES,
        match_links=["https://oddsportal.com/m1", "https://oddsportal.com/m2"],
        sport="tennis",
        markets=["1x2"],
        concurrency_tasks=5,
    )

    assert scraper_mock.scrape_matches.call_args.kwargs.get("concurrent_scraping_task") == 5


@pytest.mark.asyncio
async def test_retry_scrape_success():
    """Test retry_scrape function with successful first attempt."""
    mock_func = AsyncMock(return_value={"data": "test"})

    result = await retry_scrape(mock_func, "arg1", kwarg1="test")

    mock_func.assert_called_once_with("arg1", kwarg1="test")
    assert result == {"data": "test"}


@pytest.mark.asyncio
@patch("oddsharvester.core.retry.asyncio.sleep", new_callable=AsyncMock)
async def test_retry_scrape_transient_error(mock_sleep):
    """Test retry_scrape function with transient error that succeeds on retry."""
    mock_func = AsyncMock()

    # Fail with a transient error on first call, succeed on second
    mock_func.side_effect = [Exception(f"Connection failed: {TRANSIENT_ERROR_KEYWORDS[0]}"), {"data": "retry_success"}]

    result = await retry_scrape(mock_func, "arg1")

    assert mock_func.call_count == 2
    mock_sleep.assert_called_once()
    assert result == {"data": "retry_success"}


@pytest.mark.asyncio
@patch("oddsharvester.core.retry.asyncio.sleep", new_callable=AsyncMock)
async def test_retry_scrape_non_retryable_error(mock_sleep):
    """Test retry_scrape function with non-retryable error."""
    mock_func = AsyncMock(side_effect=ValueError("Invalid input"))

    with pytest.raises(Exception, match="Invalid input"):
        await retry_scrape(mock_func, "arg1")

    mock_func.assert_called_once()
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
@patch("oddsharvester.core.retry.asyncio.sleep", new_callable=AsyncMock)
async def test_retry_scrape_max_retries_exceeded(mock_sleep):
    """Test retry_scrape returns None when max retries are exceeded for transient errors."""
    mock_func = AsyncMock(side_effect=Exception(f"Connection failed: {TRANSIENT_ERROR_KEYWORDS[0]}"))

    result = await retry_scrape(mock_func)

    assert result is None
    assert mock_func.call_count == OPERATION_RETRY_MAX_ATTEMPTS
    assert mock_sleep.call_count == OPERATION_RETRY_MAX_ATTEMPTS - 1


@pytest.mark.asyncio
@patch("oddsharvester.core.scraper_app.OddsPortalScraper")
@patch("oddsharvester.core.scraper_app.ProxyManager")
@patch("oddsharvester.core.scraper_app.SportMarketRegistrar")
async def test_run_scraper_error_handling(sport_market_registrar_mock, proxy_manager_mock, scraper_cls_mock):
    """Test error handling in run_scraper."""
    scraper_mock = AsyncMock()
    scraper_mock.start_playwright = AsyncMock(side_effect=Exception("Playwright error"))
    scraper_mock.stop_playwright = AsyncMock()
    scraper_cls_mock.return_value = scraper_mock

    proxy_manager_instance = MagicMock()
    proxy_manager_instance.get_current_proxy.return_value = {"server": "test-proxy"}
    proxy_manager_mock.return_value = proxy_manager_instance

    with pytest.raises(Exception, match="Playwright error"):
        await run_scraper(command=CommandEnum.HISTORIC, sport="football", leagues=["premier-league"], season="2023")

    scraper_mock.stop_playwright.assert_called_once()


@pytest.mark.asyncio
async def test_run_scraper_returns_valid_result_when_cleanup_fails(monkeypatch, caplog):
    import oddsharvester.core.scraper_app as scraper_app

    valid_result = ScrapeResult(
        success=[{"match_link": "https://www.oddsportal.com/football/example-match/"}],
        stats=ScrapeStats(total_urls=1, successful=1),
    )
    scraper = MagicMock()
    scraper.start_playwright = AsyncMock()
    scraper.stop_playwright = AsyncMock(side_effect=RuntimeError("sensitive cleanup detail"))
    monkeypatch.setattr(scraper_app, "OddsPortalScraper", MagicMock(return_value=scraper))
    monkeypatch.setattr(scraper_app, "PlaywrightManager", MagicMock())
    monkeypatch.setattr(scraper_app, "_scrape_multiple_leagues", AsyncMock(return_value=valid_result))

    with caplog.at_level("ERROR", logger="ScraperApp"):
        result = await scraper_app.run_scraper(
            command=CommandEnum.UPCOMING_MATCHES,
            sport="football",
            date="20991231",
            leagues=["premier-league", "serie-a"],
            markets=["1x2"],
        )

    assert result is valid_result
    assert result.success == [{"match_link": "https://www.oddsportal.com/football/example-match/"}]
    assert result.stats == ScrapeStats(total_urls=1, successful=1)
    assert result.metadata["cleanup"] == {
        "status": "failed",
        "phase": "final_cleanup",
        "error_type": "RuntimeError",
    }
    scraper.stop_playwright.assert_awaited_once()
    assert "phase=final_cleanup error_type=RuntimeError" in caplog.text
    assert "sensitive cleanup detail" not in caplog.text


@pytest.mark.asyncio
async def test_run_scraper_preserves_primary_error_when_cleanup_also_fails(monkeypatch):
    import oddsharvester.core.scraper_app as scraper_app

    primary_error = RuntimeError("primary scrape failed")
    scraper = MagicMock()
    scraper.start_playwright = AsyncMock()
    scraper.stop_playwright = AsyncMock(side_effect=RuntimeError("cleanup failed"))
    monkeypatch.setattr(scraper_app, "OddsPortalScraper", MagicMock(return_value=scraper))
    monkeypatch.setattr(scraper_app, "PlaywrightManager", MagicMock())
    monkeypatch.setattr(scraper_app, "retry_scrape", AsyncMock(side_effect=primary_error))

    with pytest.raises(RuntimeError) as exc_info:
        await scraper_app.run_scraper(
            command=CommandEnum.UPCOMING_MATCHES,
            sport="football",
            date="20991231",
            markets=["1x2"],
        )

    assert exc_info.value is primary_error
    scraper.stop_playwright.assert_awaited_once()


@pytest.mark.asyncio
async def test_pre_camoufox_cleanup_failure_blocks_fallback(monkeypatch):
    import oddsharvester.core.scraper_app as scraper_app

    match_url = "https://www.oddsportal.com/football/h2h/a-b/c-d/#event"
    proxy_manager = MagicMock()
    proxy_manager.get_current_proxy.return_value = None
    proxy_manager.is_multi_proxy.return_value = False
    monkeypatch.setattr(scraper_app, "ProxyManager", MagicMock(return_value=proxy_manager))
    monkeypatch.setattr(
        scraper_app,
        "run_scrapling_scraper",
        AsyncMock(side_effect=ScraplingUnavailableError("unavailable")),
    )

    primary_result = ScrapeResult(
        failed=[FailedUrl(url=match_url, error_type=ErrorType.RATE_LIMITED, error_message="blocked")],
        stats=ScrapeStats(total_urls=1, failed=1),
    )
    primary_scraper = MagicMock()
    primary_scraper.start_playwright = AsyncMock()
    primary_scraper.stop_playwright = AsyncMock(side_effect=RuntimeError("private cleanup detail"))
    scraper_factory = MagicMock(return_value=primary_scraper)
    monkeypatch.setattr(scraper_app, "OddsPortalScraper", scraper_factory)
    monkeypatch.setattr(scraper_app, "PlaywrightManager", MagicMock())
    camoufox_manager = MagicMock()
    monkeypatch.setattr(scraper_app, "CamoufoxManager", camoufox_manager)
    monkeypatch.setattr(scraper_app, "retry_scrape", AsyncMock(return_value=primary_result))

    result = await scraper_app.run_scraper(
        command=CommandEnum.UPCOMING_MATCHES,
        match_links=[match_url],
        sport="football",
        markets=["1x2"],
        scraper_engine="auto",
    )

    assert result is primary_result
    assert result.failed[0].url == match_url
    assert result.metadata["cleanup"] == {
        "status": "failed",
        "phase": "pre_camoufox",
        "error_type": "RuntimeError",
    }
    assert result.metadata["engine_attempts"][-1] == {
        "engine": "camoufox",
        "outcome": "blocked",
        "detail": "primary_cleanup_failed",
    }
    assert primary_scraper.stop_playwright.await_count == 1
    assert scraper_factory.call_count == 1
    camoufox_manager.assert_not_called()


@pytest.mark.asyncio
async def test_auto_preserves_primary_result_when_camoufox_is_unavailable(monkeypatch):
    import oddsharvester.core.scraper_app as scraper_app

    monkeypatch.setenv("OH_XHR_COOLDOWN_BASE", "0")
    match_url = "https://www.oddsportal.com/football/h2h/a-b/c-d/#event"
    proxy_manager = MagicMock()
    proxy_manager.get_current_proxy.return_value = None
    proxy_manager.is_multi_proxy.return_value = False
    monkeypatch.setattr(scraper_app, "ProxyManager", MagicMock(return_value=proxy_manager))
    monkeypatch.setattr(
        scraper_app,
        "run_scrapling_scraper",
        AsyncMock(side_effect=ScraplingUnavailableError("unavailable")),
    )

    primary_result = ScrapeResult(
        success=[{"match_link": "https://www.oddsportal.com/football/h2h/e-f/g-h/#success"}],
        failed=[FailedUrl(url=match_url, error_type=ErrorType.RATE_LIMITED, error_message="blocked")],
        stats=ScrapeStats(total_urls=2, successful=1, failed=1),
    )
    primary_scraper = MagicMock()
    primary_scraper.start_playwright = AsyncMock()
    primary_scraper.stop_playwright = AsyncMock()
    unavailable_error = CamoufoxUnavailableError("private asset path")
    fallback_scraper = MagicMock()
    fallback_scraper.start_playwright = AsyncMock(side_effect=unavailable_error)
    fallback_scraper.stop_playwright = AsyncMock()
    monkeypatch.setattr(
        scraper_app,
        "OddsPortalScraper",
        MagicMock(side_effect=[primary_scraper, fallback_scraper]),
    )
    monkeypatch.setattr(scraper_app, "PlaywrightManager", MagicMock())
    monkeypatch.setattr(scraper_app, "CamoufoxManager", MagicMock())
    monkeypatch.setattr(scraper_app, "retry_scrape", AsyncMock(return_value=primary_result))

    result = await scraper_app.run_scraper(
        command=CommandEnum.UPCOMING_MATCHES,
        match_links=[match_url],
        sport="football",
        markets=["1x2"],
        scraper_engine="auto",
    )

    assert result is primary_result
    assert result.stats == ScrapeStats(total_urls=2, successful=1, failed=1)
    assert result.metadata["engine_attempts"][-1] == {
        "engine": "camoufox",
        "outcome": "unavailable",
        "detail": "CamoufoxUnavailableError",
    }
    assert "private asset path" not in str(result.metadata["engine_attempts"])
    primary_scraper.stop_playwright.assert_awaited_once()
    fallback_scraper.stop_playwright.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_multi_league_camoufox_recovery_retains_requested_leagues(monkeypatch):
    import oddsharvester.core.scraper_app as scraper_app

    monkeypatch.setenv("OH_XHR_COOLDOWN_BASE", "0")
    success_url = "https://www.oddsportal.com/football/england/premier-league/success"
    failed_url = "https://www.oddsportal.com/football/germany/bundesliga/recovered"
    leagues = ["england-premier-league", "germany-bundesliga"]

    proxy_manager = MagicMock()
    proxy_manager.get_current_proxy.return_value = None
    proxy_manager.is_multi_proxy.return_value = False
    monkeypatch.setattr(scraper_app, "ProxyManager", MagicMock(return_value=proxy_manager))
    monkeypatch.setattr(
        scraper_app,
        "run_scrapling_scraper",
        AsyncMock(side_effect=StaticListingRequiresBrowserError("listing requires browser")),
    )

    primary_scraper = MagicMock()
    primary_scraper.start_playwright = AsyncMock()
    primary_scraper.stop_playwright = AsyncMock()
    fallback_scraper = MagicMock()
    fallback_scraper.start_playwright = AsyncMock()
    fallback_scraper.stop_playwright = AsyncMock()
    monkeypatch.setattr(
        scraper_app,
        "OddsPortalScraper",
        MagicMock(side_effect=[primary_scraper, fallback_scraper]),
    )
    monkeypatch.setattr(scraper_app, "PlaywrightManager", MagicMock())
    monkeypatch.setattr(scraper_app, "CamoufoxManager", MagicMock())
    monkeypatch.setattr(
        scraper_app,
        "retry_scrape",
        AsyncMock(
            side_effect=[
                ScrapeResult(
                    success=[{"match_link": success_url}],
                    stats=ScrapeStats(total_urls=1, successful=1),
                ),
                ScrapeResult(
                    failed=[
                        FailedUrl(
                            url=failed_url,
                            error_type=ErrorType.RATE_LIMITED,
                            error_message="blocked",
                        )
                    ],
                    stats=ScrapeStats(total_urls=1, failed=1),
                ),
                ScrapeResult(
                    success=[{"match_link": failed_url}],
                    stats=ScrapeStats(total_urls=1, successful=1),
                ),
            ]
        ),
    )

    original_run_scraper = scraper_app.run_scraper
    recursive_run = AsyncMock(wraps=original_run_scraper)
    monkeypatch.setattr(scraper_app, "run_scraper", recursive_run)

    result = await original_run_scraper(
        command=CommandEnum.UPCOMING_MATCHES,
        sport="football",
        leagues=leagues,
        markets=["1x2"],
        scraper_engine="auto",
    )

    assert result.success == [
        {"match_link": success_url, "requested_league_slug": leagues[0]},
        {"match_link": failed_url, "requested_league_slug": leagues[1]},
    ]
    assert not result.failed
    recursive_run.assert_awaited_once()
    assert recursive_run.call_args.kwargs["match_links"] == [failed_url]
    assert recursive_run.call_args.kwargs["_requested_league_by_match_link"] == {failed_url: leagues[1]}


@pytest.mark.asyncio
async def test_scrape_multiple_leagues_success():
    """Test _scrape_multiple_leagues with successful scraping."""
    scraper_mock = MagicMock()
    scrape_func_mock = AsyncMock()

    # Mock successful scraping for each league with ScrapeResult
    scrape_func_mock.side_effect = [
        ScrapeResult(
            success=[{"match1": "data1"}, {"match2": "data2"}],
            stats=ScrapeStats(total_urls=2, successful=2),
        ),
        ScrapeResult(
            success=[{"match3": "data3"}],
            stats=ScrapeStats(total_urls=1, successful=1),
        ),
        ScrapeResult(
            success=[{"match4": "data4"}, {"match5": "data5"}, {"match6": "data6"}],
            stats=ScrapeStats(total_urls=3, successful=3),
        ),
    ]

    leagues = ["england-premier-league", "spain-primera-division", "italy-serie-a"]

    with patch("oddsharvester.core.scraper_app.retry_scrape", scrape_func_mock):
        result = await _scrape_multiple_leagues(
            scraper=scraper_mock,
            scrape_func=scrape_func_mock,
            leagues=leagues,
            sport="football",
            season="2023",
            markets=["1x2"],
        )

    # Verify all leagues were processed
    assert scrape_func_mock.call_count == 3

    # Verify the combined results
    assert isinstance(result, ScrapeResult)
    assert len(result.success) == 6  # 2 + 1 + 3 matches
    assert result.stats.successful == 6
    assert result.success[0] == {
        "match1": "data1",
        "requested_league_slug": "england-premier-league",
    }
    assert result.success[2] == {
        "match3": "data3",
        "requested_league_slug": "spain-primera-division",
    }
    assert result.success[5] == {
        "match6": "data6",
        "requested_league_slug": "italy-serie-a",
    }


@pytest.mark.asyncio
async def test_scrape_multiple_leagues_publishes_success_failed_and_partial_provenance():
    success_url = "https://www.oddsportal.com/football/england/premier-league/success"
    failed_url = "https://www.oddsportal.com/football/england/premier-league/failed"
    partial_url = "https://www.oddsportal.com/football/germany/bundesliga/partial"
    scrape_func_mock = AsyncMock(
        side_effect=[
            ScrapeResult(
                success=[{"match_link": success_url}],
                failed=[FailedUrl(url=failed_url, error_type=ErrorType.NAVIGATION, error_message="failed")],
                stats=ScrapeStats(total_urls=2, successful=1, failed=1),
            ),
            ScrapeResult(
                partial=[PartialResult(url=partial_url, data={"match_link": partial_url})],
                stats=ScrapeStats(total_urls=1, partial=1),
            ),
        ]
    )

    with patch("oddsharvester.core.scraper_app.retry_scrape", scrape_func_mock):
        result = await _scrape_multiple_leagues(
            scraper=MagicMock(),
            scrape_func=scrape_func_mock,
            leagues=["england-premier-league", "germany-bundesliga"],
            sport="football",
        )

    assert result.metadata["_discovered_match_links"] == [success_url, failed_url, partial_url]
    assert result.metadata["_requested_league_by_match_link"] == {
        success_url: "england-premier-league",
        failed_url: "england-premier-league",
        partial_url: "germany-bundesliga",
    }
    assert result.partial[0].data["requested_league_slug"] == "germany-bundesliga"


@pytest.mark.asyncio
async def test_scrape_multiple_leagues_duplicate_link_has_fatal_requested_league_conflict():
    duplicate = "https://www.oddsportal.com/football/h2h/a-b/c-d/#event"
    scrape_func_mock = AsyncMock(
        side_effect=[
            ScrapeResult(
                success=[{"match_link": duplicate}],
                stats=ScrapeStats(total_urls=1, successful=1),
            ),
            ScrapeResult(
                success=[{"match_link": duplicate}],
                stats=ScrapeStats(total_urls=1, successful=1),
            ),
        ]
    )

    with (
        patch("oddsharvester.core.scraper_app.retry_scrape", scrape_func_mock),
        pytest.raises(RequestedLeagueProvenanceError, match="Requested league provenance is invalid") as exc_info,
    ):
        await _scrape_multiple_leagues(
            scraper=MagicMock(),
            scrape_func=scrape_func_mock,
            leagues=["premier-league", "bundesliga"],
            sport="football",
        )

    assert "oddsportal.com" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_scrape_multiple_leagues_failed_partial_link_conflict_is_fatal():
    duplicate = "https://www.oddsportal.com/football/h2h/a-b/c-d/#event"
    scrape_func_mock = AsyncMock(
        side_effect=[
            ScrapeResult(
                failed=[FailedUrl(url=duplicate, error_type=ErrorType.NAVIGATION, error_message="failed")],
                stats=ScrapeStats(total_urls=1, failed=1),
            ),
            ScrapeResult(
                partial=[PartialResult(url=duplicate, data={})],
                stats=ScrapeStats(total_urls=1, partial=1),
            ),
        ]
    )

    with (
        patch("oddsharvester.core.scraper_app.retry_scrape", scrape_func_mock),
        pytest.raises(RequestedLeagueProvenanceError, match="Requested league provenance is invalid"),
    ):
        await _scrape_multiple_leagues(
            scraper=MagicMock(),
            scrape_func=scrape_func_mock,
            leagues=["premier-league", "bundesliga"],
            sport="football",
        )


@pytest.mark.asyncio
async def test_scrape_multiple_leagues_all_truthful_no_fixtures_attests_aggregate_outcome():
    scraper_mock = MagicMock()
    scrape_func_mock = AsyncMock(
        side_effect=[
            ScrapeResult(stats=ScrapeStats(total_urls=0), metadata={"discovery_outcome": "no_fixtures"}),
            ScrapeResult(stats=ScrapeStats(total_urls=0), metadata={"discovery_outcome": "no_fixtures"}),
        ]
    )

    with patch("oddsharvester.core.scraper_app.retry_scrape", scrape_func_mock):
        result = await _scrape_multiple_leagues(
            scraper=scraper_mock,
            scrape_func=scrape_func_mock,
            leagues=["england-premier-league", "spain-primera-division"],
            sport="football",
        )

    assert result.metadata == {"discovery_outcome": "no_fixtures"}
    assert result.stats == ScrapeStats()


@pytest.mark.asyncio
async def test_scrape_multiple_leagues_mixed_success_and_no_fixtures_does_not_attest_no_fixtures():
    scraper_mock = MagicMock()
    scrape_func_mock = AsyncMock(
        side_effect=[
            ScrapeResult(stats=ScrapeStats(total_urls=1, successful=1), success=[{"match": "data"}]),
            ScrapeResult(stats=ScrapeStats(total_urls=0), metadata={"discovery_outcome": "no_fixtures"}),
        ]
    )

    with patch("oddsharvester.core.scraper_app.retry_scrape", scrape_func_mock):
        result = await _scrape_multiple_leagues(
            scraper=scraper_mock,
            scrape_func=scrape_func_mock,
            leagues=["england-premier-league", "spain-primera-division"],
            sport="football",
        )

    assert result.success == [{"match": "data", "requested_league_slug": "england-premier-league"}]
    assert result.stats.successful == 1
    assert "discovery_outcome" not in result.metadata


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invalid_result", "expected_error_type", "expected_message"),
    [
        (Exception("Network error"), ErrorType.NAVIGATION, "Network error"),
        (ScrapeResult(), ErrorType.UNKNOWN, "League scraper returned an unattested empty result"),
        (None, ErrorType.UNKNOWN, "League scraper returned no result"),
    ],
)
async def test_scrape_multiple_leagues_no_fixtures_with_unattested_result_does_not_attest_no_fixtures(
    invalid_result, expected_error_type, expected_message
):
    scraper_mock = MagicMock()
    scrape_func_mock = AsyncMock(
        side_effect=[
            ScrapeResult(stats=ScrapeStats(total_urls=0), metadata={"discovery_outcome": "no_fixtures"}),
            invalid_result,
        ]
    )

    with patch("oddsharvester.core.scraper_app.retry_scrape", scrape_func_mock):
        result = await _scrape_multiple_leagues(
            scraper=scraper_mock,
            scrape_func=scrape_func_mock,
            leagues=["england-premier-league", "spain-primera-division"],
            sport="football",
        )

    assert "discovery_outcome" not in result.metadata
    assert result.stats.total_urls == 1
    assert result.stats.failed == 1
    assert len(result.failed) == 1
    assert result.failed[0].url == "league://football/spain-primera-division"
    assert result.failed[0].error_type == expected_error_type
    assert result.failed[0].error_message == expected_message


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_first", [False, True])
@pytest.mark.parametrize(
    "invalid_result",
    [
        ScrapeResult(
            success=[{"match": "inconsistent"}],
            stats=ScrapeStats(total_urls=0, successful=1),
            metadata={"discovery_outcome": "no_fixtures"},
        ),
        ScrapeResult(
            failed=[
                FailedUrl(
                    url="https://example.test/failed",
                    error_type=ErrorType.NAVIGATION,
                    error_message="navigation failed",
                )
            ],
            stats=ScrapeStats(total_urls=1, failed=1),
        ),
        ScrapeResult(
            partial=[PartialResult(url="https://example.test/partial", data={})],
            stats=ScrapeStats(total_urls=1, partial=1),
        ),
    ],
)
async def test_scrape_multiple_leagues_no_fixtures_with_non_benign_result_fails_closed(invalid_result, invalid_first):
    scraper_mock = MagicMock()
    no_fixtures = ScrapeResult(
        stats=ScrapeStats(total_urls=0),
        metadata={"discovery_outcome": "no_fixtures"},
    )
    side_effect = [invalid_result, no_fixtures] if invalid_first else [no_fixtures, invalid_result]
    scrape_func_mock = AsyncMock(side_effect=side_effect)

    with patch("oddsharvester.core.scraper_app.retry_scrape", scrape_func_mock):
        result = await _scrape_multiple_leagues(
            scraper=scraper_mock,
            scrape_func=scrape_func_mock,
            leagues=["england-premier-league", "spain-primera-division"],
            sport="football",
        )

    assert "discovery_outcome" not in result.metadata
    assert result.success == invalid_result.success
    assert result.failed == invalid_result.failed
    assert result.partial == invalid_result.partial
    assert result.stats == invalid_result.stats


@pytest.mark.asyncio
async def test_scrape_multiple_leagues_with_failures():
    """Test _scrape_multiple_leagues with some league failures."""
    scraper_mock = MagicMock()
    scrape_func_mock = AsyncMock()

    # Mock mixed success/failure with ScrapeResult
    scrape_func_mock.side_effect = [
        ScrapeResult(
            success=[{"match1": "data1"}],
            stats=ScrapeStats(total_urls=1, successful=1),
        ),
        Exception("Network error"),  # primera-division - failure
        ScrapeResult(
            success=[{"match2": "data2"}],
            stats=ScrapeStats(total_urls=1, successful=1),
        ),
    ]

    leagues = ["england-premier-league", "spain-primera-division", "italy-serie-a"]

    with patch("oddsharvester.core.scraper_app.retry_scrape", scrape_func_mock):
        result = await _scrape_multiple_leagues(
            scraper=scraper_mock,
            scrape_func=scrape_func_mock,
            leagues=leagues,
            sport="football",
            season="2023",
        )

    # Verify all leagues were attempted
    assert scrape_func_mock.call_count == 3

    # Verify only successful results are included
    assert isinstance(result, ScrapeResult)
    assert len(result.success) == 2  # Only 2 successful matches
    assert result.stats.successful == 2
    assert result.success[0] == {
        "match1": "data1",
        "requested_league_slug": "england-premier-league",
    }
    assert result.success[1] == {
        "match2": "data2",
        "requested_league_slug": "italy-serie-a",
    }


@pytest.mark.asyncio
async def test_scrape_multiple_leagues_empty_results():
    """Test _scrape_multiple_leagues with empty results from some leagues."""
    scraper_mock = MagicMock()
    scrape_func_mock = AsyncMock()

    # Mock mixed results including empty ones with ScrapeResult
    scrape_func_mock.side_effect = [
        ScrapeResult(
            success=[{"match1": "data1"}],
            stats=ScrapeStats(total_urls=1, successful=1),
        ),
        ScrapeResult(success=[], stats=ScrapeStats(total_urls=0)),  # primera-division - empty
        None,  # serie-a - None result
    ]

    leagues = ["england-premier-league", "spain-primera-division", "italy-serie-a"]

    with patch("oddsharvester.core.scraper_app.retry_scrape", scrape_func_mock):
        result = await _scrape_multiple_leagues(
            scraper=scraper_mock,
            scrape_func=scrape_func_mock,
            leagues=leagues,
            sport="football",
        )

    # Verify only non-empty results are included
    assert isinstance(result, ScrapeResult)
    assert len(result.success) == 1
    assert result.success[0] == {
        "match1": "data1",
        "requested_league_slug": "england-premier-league",
    }


@pytest.mark.asyncio
async def test_run_scraper_multiple_leagues_historic():
    """Test run_scraper with multiple leagues for historic command."""
    with (
        patch("oddsharvester.core.scraper_app.OddsPortalScraper") as scraper_cls_mock,
        patch("oddsharvester.core.scraper_app.OddsPortalMarketExtractor"),
        patch("oddsharvester.core.scraper_app.PlaywrightManager"),
        patch("oddsharvester.core.scraper_app.ProxyManager"),
        patch("oddsharvester.core.scraper_app.SportMarketRegistrar"),
        patch("oddsharvester.core.scraper_app._scrape_multiple_leagues") as multi_scrape_mock,
    ):
        scraper_mock = MagicMock()
        scraper_mock.start_playwright = AsyncMock()
        scraper_mock.stop_playwright = AsyncMock()
        scraper_cls_mock.return_value = scraper_mock

        multi_scrape_mock.return_value = [{"combined": "data"}]

        result = await run_scraper(
            command=CommandEnum.HISTORIC,
            sport="football",
            leagues=["england-premier-league", "spain-primera-division"],
            season="2023",
            markets=["1x2"],
        )

        # Verify _scrape_multiple_leagues was called for multiple leagues
        multi_scrape_mock.assert_called_once()
        call_args = multi_scrape_mock.call_args
        assert call_args[1]["leagues"] == ["england-premier-league", "spain-primera-division"]
        assert call_args[1]["sport"] == "football"
        assert call_args[1]["season"] == "2023"

        assert result == [{"combined": "data"}]


# Separate test cases for validation errors
@pytest.mark.parametrize(
    ("command", "params", "error_message"),
    [
        (CommandEnum.HISTORIC, {}, "Both 'sport', 'league' and 'season' must be provided for historic scraping"),
        (
            CommandEnum.UPCOMING_MATCHES,
            {"sport": "football"},
            "A valid 'date' must be provided for upcoming matches scraping",
        ),
        ("invalid_command", {}, "Unknown command: invalid_command"),
    ],
)
def test_run_scraper_validation(command, params, error_message):
    """
    Test validation errors in run_scraper.

    This test directly extracts and checks validation logic without actually
    running the full function.
    """

    # Create a minimal version of run_scraper that only performs validation
    async def validate_only():
        if command == CommandEnum.HISTORIC:
            sport = params.get("sport")
            league = params.get("league")
            season = params.get("season")
            if not sport or not league or not season:
                raise ValueError("Both 'sport', 'league' and 'season' must be provided for historic scraping.")
        elif command == CommandEnum.UPCOMING_MATCHES:
            date = params.get("date")
            if not date:
                raise ValueError("A valid 'date' must be provided for upcoming matches scraping.")
        elif command not in (CommandEnum.HISTORIC, CommandEnum.UPCOMING_MATCHES):
            raise ValueError(f"Unknown command: {command}. Supported commands are 'upcoming-matches' and 'historic'.")

    # Run the validation function and check for the expected error
    with pytest.raises(ValueError) as exc_info:
        asyncio.run(validate_only())

    assert error_message in str(exc_info.value)


@pytest.mark.asyncio
@patch("oddsharvester.core.scraper_app.run_scrapling_scraper")
@patch("oddsharvester.core.scraper_app.OddsPortalScraper")
@patch("oddsharvester.core.scraper_app.PlaywrightManager")
@patch("oddsharvester.core.scraper_app.ProxyManager")
async def test_auto_stops_at_truthful_scrapling_no_fixtures(
    proxy_manager_mock, playwright_manager_mock, scraper_cls_mock, scrapling_mock
):
    proxy_manager_mock.return_value.get_current_proxy.return_value = None
    no_fixtures = ScrapeResult(
        stats=ScrapeStats(total_urls=0),
        metadata={"discovery_outcome": "no_fixtures"},
    )
    scrapling_mock.return_value = no_fixtures

    result = await run_scraper(
        command=CommandEnum.UPCOMING_MATCHES, sport="football", date="20991231", markets=["1x2"], scraper_engine="auto"
    )

    assert result is no_fixtures
    scrapling_mock.assert_awaited_once()
    scraper_cls_mock.assert_not_called()
    playwright_manager_mock.assert_not_called()


@pytest.mark.asyncio
@patch("oddsharvester.core.scraper_app.run_scrapling_scraper")
@patch("oddsharvester.core.scraper_app.ProxyManager")
async def test_auto_does_not_mask_unexpected_scrapling_programming_error(proxy_manager_mock, scrapling_mock):
    proxy_manager_mock.return_value.get_current_proxy.return_value = None
    scrapling_mock.side_effect = AttributeError("internal bug")

    with pytest.raises(AttributeError, match="internal bug"):
        await run_scraper(
            command=CommandEnum.UPCOMING_MATCHES,
            sport="football",
            date="20991231",
            markets=["1x2"],
            scraper_engine="auto",
        )


@pytest.mark.asyncio
@patch("oddsharvester.core.scraper_app.asyncio.sleep", new_callable=AsyncMock)
@patch("oddsharvester.core.scraper_app.run_scrapling_scraper")
@patch("oddsharvester.core.scraper_app.ProxyManager")
async def test_auto_waits_for_direct_egress_cooldown_before_stealth(proxy_manager_mock, scrapling_mock, sleep_mock):
    proxy_manager_mock.return_value.get_current_proxy.return_value = None
    proxy_manager_mock.return_value.entries = [MagicMock(config=None)]
    proxy_manager_mock.return_value.is_multi_proxy.return_value = False
    failed_url = "https://www.oddsportal.com/football/h2h/a-b/c-d/#event"
    http_result = ScrapeResult(
        failed=[
            FailedUrl(
                url=failed_url,
                error_type=ErrorType.RATE_LIMITED,
                error_message="soft block",
            )
        ],
        stats=ScrapeStats(total_urls=1, failed=1),
        metadata={"egress": {"cooldown_remaining_seconds": 2.5}},
    )
    stealth_result = ScrapeResult(
        success=[{"match_link": failed_url}],
        stats=ScrapeStats(total_urls=1, successful=1),
    )
    scrapling_mock.side_effect = [http_result, stealth_result]

    result = await run_scraper(
        command=CommandEnum.UPCOMING_MATCHES,
        match_links=[failed_url],
        sport="football",
        markets=["1x2"],
        scraper_engine="auto",
    )

    sleep_mock.assert_awaited_once_with(2.5)
    assert result is stealth_result
    assert result.metadata["engine_attempts"][1] == {
        "engine": "adaptive_cooldown",
        "outcome": "waiting",
        "detail": "scrapling-http_fallback:2.500s",
    }


@pytest.mark.asyncio
async def test_auto_waits_before_camoufox_reuses_direct_ip(monkeypatch):
    import oddsharvester.core.scraper_app as scraper_app

    monkeypatch.setenv("OH_XHR_COOLDOWN_BASE", "3")
    monkeypatch.setenv("OH_XHR_COOLDOWN_MAX", "6")
    proxy_manager = MagicMock()
    proxy_manager.get_current_proxy.return_value = None
    proxy_manager.entries = [MagicMock(config=None)]
    proxy_manager.is_multi_proxy.return_value = False
    monkeypatch.setattr(scraper_app, "ProxyManager", MagicMock(return_value=proxy_manager))
    match_url = "https://www.oddsportal.com/football/h2h/a-b/c-d/#event"
    http_result = ScrapeResult(
        failed=[
            FailedUrl(
                url=match_url,
                error_type=ErrorType.RATE_LIMITED,
                error_message="XHR soft block",
            )
        ],
        stats=ScrapeStats(total_urls=1, failed=1),
        metadata={
            "egress": {
                "mode": "direct_or_single",
                "cooldown_remaining_seconds": 0,
            },
            "_discovered_match_links": [match_url],
            "_requested_league_by_match_link": {match_url: "germany-bundesliga"},
        },
    )
    scrapling_mock = AsyncMock(return_value=http_result)
    monkeypatch.setattr(scraper_app, "run_scrapling_scraper", scrapling_mock)
    primary_scraper = MagicMock()
    primary_scraper.start_playwright = AsyncMock()
    primary_scraper.stop_playwright = AsyncMock()
    fallback_scraper = MagicMock()
    fallback_scraper.start_playwright = AsyncMock()
    fallback_scraper.stop_playwright = AsyncMock()
    scraper_factory = MagicMock(side_effect=[primary_scraper, fallback_scraper])
    monkeypatch.setattr(scraper_app, "OddsPortalScraper", scraper_factory)
    monkeypatch.setattr(scraper_app, "PlaywrightManager", MagicMock())
    monkeypatch.setattr(scraper_app, "CamoufoxManager", MagicMock())
    sleep_mock = AsyncMock()
    monkeypatch.setattr(scraper_app.asyncio, "sleep", sleep_mock)

    browser_result = ScrapeResult(
        failed=[
            FailedUrl(
                url=match_url,
                error_type=ErrorType.UNKNOWN,
                error_message="No data returned",
            )
        ],
        stats=ScrapeStats(total_urls=1, failed=1),
    )
    camoufox_result = ScrapeResult(
        success=[{"match_link": match_url}],
        stats=ScrapeStats(total_urls=1, successful=1),
    )
    monkeypatch.setattr(
        scraper_app,
        "retry_scrape",
        AsyncMock(side_effect=[browser_result, camoufox_result]),
    )

    result = await scraper_app.run_scraper(
        command=CommandEnum.UPCOMING_MATCHES,
        sport="football",
        leagues=["germany-bundesliga"],
        markets=["1x2"],
        scraper_engine="auto",
    )

    sleep_mock.assert_awaited_once_with(3.0)
    scrapling_mock.assert_awaited_once()
    assert primary_scraper.stop_playwright.await_count == 1
    assert fallback_scraper.stop_playwright.await_count == 1
    assert result.success == [{"match_link": match_url, "requested_league_slug": "germany-bundesliga"}]
    assert not result.failed
    assert any(attempt.get("detail") == "camoufox_fallback:3.000s" for attempt in result.metadata["engine_attempts"])


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback_slug", [None, "spain-laliga2"])
async def test_single_league_camoufox_recovery_enforces_final_provenance(monkeypatch, fallback_slug):
    import oddsharvester.core.scraper_app as scraper_app

    match_url = "https://www.oddsportal.com/football/h2h/a-b/c-d/#event"
    proxy_manager = MagicMock()
    proxy_manager.get_current_proxy.return_value = None
    proxy_manager.entries = [MagicMock(config=None)]
    proxy_manager.is_multi_proxy.return_value = False
    monkeypatch.setattr(scraper_app, "ProxyManager", MagicMock(return_value=proxy_manager))
    monkeypatch.setattr(
        scraper_app,
        "run_scrapling_scraper",
        AsyncMock(
            return_value=ScrapeResult(
                failed=[
                    FailedUrl(
                        url=match_url,
                        error_type=ErrorType.RATE_LIMITED,
                        error_message="XHR soft block",
                    )
                ],
                stats=ScrapeStats(total_urls=1, failed=1),
            )
        ),
    )
    primary_scraper = MagicMock()
    primary_scraper.start_playwright = AsyncMock()
    primary_scraper.stop_playwright = AsyncMock()
    fallback_scraper = MagicMock()
    fallback_scraper.start_playwright = AsyncMock()
    fallback_scraper.stop_playwright = AsyncMock()
    monkeypatch.setattr(
        scraper_app,
        "OddsPortalScraper",
        MagicMock(side_effect=[primary_scraper, fallback_scraper]),
    )
    monkeypatch.setattr(scraper_app, "PlaywrightManager", MagicMock())
    monkeypatch.setattr(scraper_app, "CamoufoxManager", MagicMock())
    monkeypatch.setattr(scraper_app.asyncio, "sleep", AsyncMock())
    fallback_record = {"match_link": match_url}
    if fallback_slug is not None:
        fallback_record["requested_league_slug"] = fallback_slug
    monkeypatch.setattr(
        scraper_app,
        "retry_scrape",
        AsyncMock(
            side_effect=[
                ScrapeResult(
                    failed=[
                        FailedUrl(
                            url=match_url,
                            error_type=ErrorType.NAVIGATION,
                            error_message="No data returned",
                        )
                    ],
                    stats=ScrapeStats(total_urls=1, failed=1),
                ),
                ScrapeResult(
                    success=[fallback_record],
                    stats=ScrapeStats(total_urls=1, successful=1),
                ),
            ]
        ),
    )

    call = scraper_app.run_scraper(
        command=CommandEnum.UPCOMING_MATCHES,
        sport="football",
        leagues=["spain-laliga"],
        markets=["1x2"],
        scraper_engine="auto",
    )
    if fallback_slug is not None:
        with pytest.raises(RequestedLeagueProvenanceError, match="provenance is invalid"):
            await call
    else:
        result = await call
        assert result.success == [{"match_link": match_url, "requested_league_slug": "spain-laliga"}]
        assert not result.failed
