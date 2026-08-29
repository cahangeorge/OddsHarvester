import asyncio
import logging
import math
import os
from urllib.parse import urlsplit

from oddsharvester.core.browser.cookies import CookieDismisser
from oddsharvester.core.browser.market_navigation import MarketTabNavigator
from oddsharvester.core.browser.scrolling import PageScroller
from oddsharvester.core.browser.selection import SelectionManager
from oddsharvester.core.camoufox_manager import CamoufoxManager, CamoufoxUnavailableError
from oddsharvester.core.odds_portal_market_extractor import OddsPortalMarketExtractor
from oddsharvester.core.odds_portal_scraper import OddsPortalScraper
from oddsharvester.core.oddsportal_xhr import (
    OddsPortalXHRDecodeError,
    OddsPortalXHRSchemaError,
)
from oddsharvester.core.playwright_manager import PlaywrightManager
from oddsharvester.core.retry import RetryConfig, classify_error, is_retryable_error, retry_with_backoff
from oddsharvester.core.scrape_result import ErrorType, FailedUrl, ScrapeResult
from oddsharvester.core.scrapling_scraper import (
    DEFAULT_EGRESS_COOLDOWN_BASE_SECONDS,
    DEFAULT_EGRESS_COOLDOWN_MAX_SECONDS,
    RequestedLeagueProvenanceError,
    ScraplingProxyError,
    ScraplingUnavailableError,
    StaticListingRequiresBrowserError,
    run_scrapling_scraper,
)
from oddsharvester.core.sport_market_registry import SportMarketRegistrar
from oddsharvester.utils.bookies_filter_enum import BookiesFilter
from oddsharvester.utils.command_enum import CommandEnum
from oddsharvester.utils.constants import (
    DEFAULT_REQUEST_DELAY_S,
    OPERATION_RETRY_BASE_DELAY,
    OPERATION_RETRY_MAX_ATTEMPTS,
    OPERATION_RETRY_MAX_DELAY,
)
from oddsharvester.utils.proxy_manager import ProxyManager
from oddsharvester.utils.scraper_engine import SCRAPLING_ENGINES, ScraperEngine
from oddsharvester.utils.utils import validate_and_convert_period

logger = logging.getLogger("ScraperApp")


def _nonnegative_env_float(name: str, default: float) -> float:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError:
        logger.warning("Ignoring invalid numeric environment value for %s", name)
        return default
    if not math.isfinite(value) or value < 0:
        logger.warning("Ignoring invalid environment value for %s", name)
        return default
    return value


async def run_scraper(
    command: CommandEnum,
    match_links: list | None = None,
    sport: str | None = None,
    date: str | None = None,
    leagues: list[str] | None = None,
    season: str | None = None,
    markets: list | None = None,
    max_pages: int | None = None,
    proxy_url: str | list[str] | tuple[str, ...] | None = None,
    proxy_user: str | None = None,
    proxy_pass: str | None = None,
    browser_user_agent: str | None = None,
    browser_locale_timezone: str | None = None,
    browser_timezone_id: str | None = None,
    base_url: str | None = None,
    target_bookmaker: str | None = None,
    scrape_odds_history: bool = False,
    headless: bool = True,
    preview_submarkets_only: bool = False,
    bookies_filter: str = BookiesFilter.ALL.value,
    period: str | None = None,
    request_delay: float = DEFAULT_REQUEST_DELAY_S,
    concurrency_tasks: int = 3,
    http_concurrency_tasks: int = 12,
    scraper_engine: str = ScraperEngine.PLAYWRIGHT.value,
    include_started: bool = False,
    _proxy_manager: ProxyManager | None = None,
    _requested_league_by_match_link: dict[str, str] | None = None,
) -> ScrapeResult | None:
    """
    Runs the scraping process and handles execution.

    Returns:
        ScrapeResult containing successful matches, failed URLs, and statistics.
        Returns None if a fatal error occurs during initialization.
    """

    bookies_filter_enum = BookiesFilter(bookies_filter)
    period_enum = validate_and_convert_period(period, sport)

    proxy_count = len(proxy_url) if isinstance(proxy_url, list | tuple) else int(bool(proxy_url))
    logger.info(
        f"Starting scraper with parameters: command={command}, match_links={match_links}, "
        f"sport={sport}, date={date}, leagues={leagues}, season={season}, markets={markets}, "
        f"max_pages={max_pages}, proxy_count={proxy_count}, browser_user_agent={browser_user_agent}, "
        f"browser_locale_timezone={browser_locale_timezone}, browser_timezone_id={browser_timezone_id}, "
        f"scrape_odds_history={scrape_odds_history}, target_bookmaker={target_bookmaker}, "
        f"headless={headless}, preview_submarkets_only={preview_submarkets_only}, "
        f"bookies_filter={bookies_filter}, period={period}, base_url={base_url}, scraper_engine={scraper_engine}"
    )

    if base_url:
        host = urlsplit(base_url).netloc.lower()
        if (
            host != "oddsportal.com"
            and not host.endswith(".oddsportal.com")
            and not browser_locale_timezone
            and not browser_timezone_id
        ):
            logger.warning(
                "Regional base URL '%s' is set but no --locale/--timezone provided. "
                "OddsPortal mirrors localise content; pass --locale and --timezone matching "
                "the region (see GitHub issue #45) for consistent results.",
                base_url,
            )

    if _proxy_manager is not None:
        proxy_manager = _proxy_manager
    elif isinstance(proxy_url, list | tuple):
        proxy_manager = ProxyManager(proxy_urls=list(proxy_url), proxy_user=proxy_user, proxy_pass=proxy_pass)
    else:
        proxy_manager = ProxyManager(proxy_url=proxy_url, proxy_user=proxy_user, proxy_pass=proxy_pass)

    proxy_config = proxy_manager.get_current_proxy()
    has_alternate_egress = proxy_manager.is_multi_proxy()
    cooldown_base = _nonnegative_env_float(
        "OH_XHR_COOLDOWN_BASE",
        DEFAULT_EGRESS_COOLDOWN_BASE_SECONDS,
    )
    cooldown_max = max(
        cooldown_base,
        _nonnegative_env_float(
            "OH_XHR_COOLDOWN_MAX",
            DEFAULT_EGRESS_COOLDOWN_MAX_SECONDS,
        ),
    )
    normalized_engine = (scraper_engine or ScraperEngine.PLAYWRIGHT.value).lower()
    if normalized_engine == ScraperEngine.AUTO.value and os.environ.get("ODDSHARVESTER_PIPELINE_V2") == "0":
        normalized_engine = ScraperEngine.PLAYWRIGHT.value

    attempts: list[dict[str, str]] = []
    repair_metadata = {"status": "repair_skipped", "reason": "operator_only"}
    browser_attempted = False
    primary_cleanup_attempted = False
    primary_cleanup_diagnostic: dict[str, str] | None = None
    completed_result: ScrapeResult | None = None
    fast_path_success: list[dict] = []
    fast_path_egress: list[dict] = []
    anti_bot_observed = False
    requested_league_by_match_link = dict(_requested_league_by_match_link or {})
    league_scoped_auto_discovery = normalized_engine == ScraperEngine.AUTO.value and not match_links and bool(leagues)
    requested_leagues = set(leagues or [])

    async def wait_before_same_egress_fallback(
        *,
        reason: str,
        seconds: float,
    ) -> None:
        if has_alternate_egress or seconds <= 0:
            return
        wait_seconds = min(seconds, cooldown_max)
        attempts.append(
            {
                "engine": "adaptive_cooldown",
                "outcome": "waiting",
                "detail": f"{reason}:{wait_seconds:.3f}s",
            }
        )
        logger.warning(
            "Direct egress cooldown: waiting %.3fs before %s",
            wait_seconds,
            reason,
        )
        await asyncio.sleep(wait_seconds)

    async def wait_for_result_cooldown(result: ScrapeResult | None, *, reason: str) -> None:
        if not isinstance(result, ScrapeResult):
            return
        egress = result.metadata.get("egress")
        if not isinstance(egress, dict):
            return
        raw_seconds = egress.get("cooldown_remaining_seconds", 0)
        if isinstance(raw_seconds, int | float):
            await wait_before_same_egress_fallback(
                reason=reason,
                seconds=float(raw_seconds),
            )

    def retain_requested_league_mapping(result: ScrapeResult) -> None:
        discovered_links = result.metadata.get("_discovered_match_links")
        discovered_mapping = result.metadata.get("_requested_league_by_match_link")
        if league_scoped_auto_discovery and discovered_links is not None:
            valid = (
                isinstance(discovered_links, list)
                and all(isinstance(link, str) for link in discovered_links)
                and isinstance(discovered_mapping, dict)
                and set(discovered_mapping) == set(discovered_links)
                and all(
                    isinstance(link, str) and isinstance(league, str) and league in requested_leagues
                    for link, league in discovered_mapping.items()
                )
            )
            if not valid:
                raise RequestedLeagueProvenanceError("Requested league provenance is invalid")
        if not isinstance(discovered_mapping, dict):
            return
        for link, league in discovered_mapping.items():
            if not isinstance(link, str) or not isinstance(league, str):
                continue
            existing = requested_league_by_match_link.get(link)
            if existing is not None and existing != league:
                raise RequestedLeagueProvenanceError("Requested league provenance is invalid")
            requested_league_by_match_link[link] = league

    def enforce_single_requested_league(result: ScrapeResult) -> None:
        """Complete final provenance for an exact single-league discovery.

        Browser fallbacks can return the same match with a normalized URL that
        no longer matches the discovery URL byte-for-byte.  The outer request
        is nevertheless scoped to one explicit league.  Preserve that
        authoritative scope after all fallback merges, while still rejecting
        any conflicting provenance supplied by an adapter.
        """

        if not league_scoped_auto_discovery or len(requested_leagues) != 1:
            return
        requested_league = next(iter(requested_leagues))
        for record in result.success:
            existing = record.get("requested_league_slug")
            if existing is not None and existing != requested_league:
                raise RequestedLeagueProvenanceError("Requested league provenance is invalid")
            record["requested_league_slug"] = requested_league
        for partial in result.partial:
            existing = partial.data.get("requested_league_slug")
            if existing is not None and existing != requested_league:
                raise RequestedLeagueProvenanceError("Requested league provenance is invalid")
            partial.data["requested_league_slug"] = requested_league

    def retain_fast_path_success(result: ScrapeResult) -> None:
        known_links = {str(row.get("match_link") or "") for row in fast_path_success}
        fast_path_success.extend(row for row in result.success if str(row.get("match_link") or "") not in known_links)

    def combine_fast_path_success(result: ScrapeResult | None) -> ScrapeResult | None:
        if not isinstance(result, ScrapeResult) or not fast_path_success:
            return result
        known_links = {str(row.get("match_link") or "") for row in fast_path_success}
        result.success = fast_path_success + [
            row for row in result.success if str(row.get("match_link") or "") not in known_links
        ]
        result.stats.successful = len(result.success)
        result.stats.failed = len(result.failed)
        result.stats.partial = len(result.partial)
        result.stats.total_urls = result.stats.successful + result.stats.failed + result.stats.partial
        return result

    def merge_retry_result(primary: ScrapeResult, retry: ScrapeResult) -> ScrapeResult:
        retry_success_urls = {str(row.get("match_link") or "") for row in retry.success}
        primary_success_urls = {str(row.get("match_link") or "") for row in primary.success}
        primary.success.extend(
            row for row in retry.success if str(row.get("match_link") or "") not in primary_success_urls
        )
        primary.failed = [failure for failure in primary.failed if failure.url not in retry_success_urls]
        existing_failures = {failure.url for failure in primary.failed}
        primary.failed.extend(failure for failure in retry.failed if failure.url not in existing_failures)
        primary.partial.extend(retry.partial)
        primary.metadata.update(retry.metadata)
        primary.stats.successful = len(primary.success)
        primary.stats.failed = len(primary.failed)
        primary.stats.partial = len(primary.partial)
        primary.stats.total_urls = primary.stats.successful + primary.stats.failed + primary.stats.partial
        return primary

    def attach_cleanup_diagnostic(result: ScrapeResult, diagnostic: dict[str, str]) -> None:
        result.metadata["cleanup"] = diagnostic

    async def cleanup_primary_scraper(*, phase: str) -> dict[str, str] | None:
        nonlocal primary_cleanup_attempted, primary_cleanup_diagnostic
        if primary_cleanup_attempted:
            return primary_cleanup_diagnostic
        primary_cleanup_attempted = True
        try:
            await scraper.stop_playwright()
        except Exception as exc:
            primary_cleanup_diagnostic = {
                "status": "failed",
                "phase": phase[:40],
                "error_type": type(exc).__name__[:80],
            }
            logger.error(
                "Scraper cleanup failed: phase=%s error_type=%s",
                primary_cleanup_diagnostic["phase"],
                primary_cleanup_diagnostic["error_type"],
            )
        return primary_cleanup_diagnostic

    async def with_execution_metadata(result: ScrapeResult | None) -> ScrapeResult | None:
        nonlocal completed_result, repair_metadata
        result = combine_fast_path_success(result)
        if isinstance(result, ScrapeResult):
            retain_requested_league_mapping(result)
        camoufox_recovery_needed = bool(
            isinstance(result, ScrapeResult) and result.failed and (anti_bot_observed or _should_try_camoufox(result))
        )
        if (
            normalized_engine == ScraperEngine.AUTO.value
            and browser_attempted
            and isinstance(result, ScrapeResult)
            and camoufox_recovery_needed
        ):
            attempts.append({"engine": ScraperEngine.PLAYWRIGHT.value, "outcome": "fallback_triggered"})
            primary_result = result
            camoufox_links = [failure.url for failure in result.failed]
            # Camoufox must never overlap a primary browser whose cleanup did
            # not complete successfully.
            cleanup_diagnostic = await cleanup_primary_scraper(phase="pre_camoufox")
            if cleanup_diagnostic is not None:
                attach_cleanup_diagnostic(primary_result, cleanup_diagnostic)
                attempts.append(
                    {
                        "engine": ScraperEngine.CAMOUFOX.value,
                        "outcome": "blocked",
                        "detail": "primary_cleanup_failed",
                    }
                )
                result = primary_result
            else:
                await wait_before_same_egress_fallback(
                    reason="camoufox_fallback",
                    seconds=cooldown_base,
                )
                try:
                    fallback = await run_scraper(
                        command=command,
                        match_links=camoufox_links,
                        sport=sport,
                        date=date,
                        leagues=leagues,
                        season=season,
                        markets=markets,
                        max_pages=max_pages,
                        proxy_url=proxy_url,
                        proxy_user=proxy_user,
                        proxy_pass=proxy_pass,
                        browser_user_agent=browser_user_agent,
                        browser_locale_timezone=browser_locale_timezone,
                        browser_timezone_id=browser_timezone_id,
                        base_url=base_url,
                        target_bookmaker=target_bookmaker,
                        scrape_odds_history=scrape_odds_history,
                        headless=headless,
                        preview_submarkets_only=preview_submarkets_only,
                        bookies_filter=bookies_filter,
                        period=period,
                        request_delay=request_delay,
                        concurrency_tasks=min(concurrency_tasks, 2),
                        http_concurrency_tasks=http_concurrency_tasks,
                        scraper_engine=ScraperEngine.CAMOUFOX.value,
                        include_started=include_started,
                        _proxy_manager=proxy_manager,
                        _requested_league_by_match_link={
                            link: requested_league_by_match_link[link]
                            for link in camoufox_links
                            if link in requested_league_by_match_link
                        },
                    )
                except CamoufoxUnavailableError as exc:
                    attempts.append(
                        {
                            "engine": ScraperEngine.CAMOUFOX.value,
                            "outcome": "unavailable",
                            "detail": type(exc).__name__,
                        }
                    )
                    result = primary_result
                else:
                    attempts.append({"engine": ScraperEngine.CAMOUFOX.value, "outcome": "completed"})
                    if isinstance(fallback, ScrapeResult):
                        result = merge_retry_result(primary_result, fallback)
                    else:
                        result = primary_result
        elif normalized_engine == ScraperEngine.AUTO.value and browser_attempted:
            browser_succeeded = bool(result.success) if isinstance(result, ScrapeResult) else result is not None
            attempts.append(
                {
                    "engine": ScraperEngine.PLAYWRIGHT.value,
                    "outcome": "completed" if browser_succeeded else "failed",
                }
            )
        if isinstance(result, ScrapeResult):
            enforce_single_requested_league(result)
            result.metadata.setdefault("cache", {"status": "disabled"})
            result.metadata.update(
                {
                    "engine_attempts": attempts,
                    "repair": repair_metadata,
                    "xhr_egress_attempts": fast_path_egress,
                }
            )
            completed_result = result
        return result

    async def try_scrapling(engine: str) -> ScrapeResult | None:
        nonlocal anti_bot_observed
        try:
            result = await run_scrapling_scraper(
                engine=engine,
                scraper_options={
                    "base_url": base_url,
                    "locale": browser_locale_timezone,
                    "timezone_id": browser_timezone_id,
                    "geo": os.environ.get("OH_XHR_GEO", "RO"),
                    "proxy": proxy_config,
                    "proxy_manager": proxy_manager,
                    "concurrency_tasks": min(concurrency_tasks, 3)
                    if engine == ScraperEngine.SCRAPLING_STEALTH.value
                    else http_concurrency_tasks,
                    "request_delay": request_delay,
                    "egress_cooldown_base": cooldown_base,
                    "egress_cooldown_max": cooldown_max,
                },
                command=CommandEnum(command),
                match_links=match_links,
                sport=sport,
                date_value=date,
                leagues=leagues,
                season=season,
                markets=markets,
                max_pages=max_pages,
                target_bookmaker=target_bookmaker,
                scrape_odds_history=scrape_odds_history,
                period=period_enum.value if period_enum is not None else None,
                bookies_filter=bookies_filter_enum.value,
                preview_submarkets_only=preview_submarkets_only,
                include_started=include_started,
                requested_league_by_match_link=requested_league_by_match_link,
            )
            retain_requested_league_mapping(result)
            outcome = "partial" if result.success and result.failed else "success" if result.success else "no_records"
            attempts.append({"engine": engine, "outcome": outcome})
            egress = result.metadata.get("egress")
            if isinstance(egress, dict):
                fast_path_egress.append(dict(egress))
            if any(failure.error_type.value == ErrorType.RATE_LIMITED.value for failure in result.failed):
                anti_bot_observed = True
            return result
        except (RequestedLeagueProvenanceError, StaticListingRequiresBrowserError):
            raise
        except ScraplingProxyError as exc:
            anti_bot_observed = True
            attempts.append({"engine": engine, "outcome": "blocked", "detail": type(exc).__name__})
            if normalized_engine != ScraperEngine.AUTO.value:
                raise
            await wait_before_same_egress_fallback(
                reason=f"{engine}_fallback",
                seconds=cooldown_base,
            )
        except ScraplingUnavailableError as exc:
            attempts.append({"engine": engine, "outcome": "unavailable", "detail": str(exc)})
            if normalized_engine != ScraperEngine.AUTO.value:
                raise
        except (OddsPortalXHRDecodeError, OddsPortalXHRSchemaError) as exc:
            attempts.append({"engine": engine, "outcome": "failed", "detail": type(exc).__name__})
            if normalized_engine != ScraperEngine.AUTO.value:
                raise
        except Exception:
            logger.exception("Unexpected Scrapling fast-path failure")
            raise
        return None

    if normalized_engine in SCRAPLING_ENGINES:
        scrapling_result = await try_scrapling(normalized_engine)
        if scrapling_result is not None:
            return await with_execution_metadata(scrapling_result)
    elif normalized_engine == ScraperEngine.AUTO.value:
        # Deterministic quality/cost cascade. Each attempt is recorded in report v1.1.
        if match_links:
            for engine in (ScraperEngine.SCRAPLING_HTTP.value, ScraperEngine.SCRAPLING_STEALTH.value):
                scrapling_result = await try_scrapling(engine)
                if scrapling_result and scrapling_result.success and not scrapling_result.failed:
                    return await with_execution_metadata(scrapling_result)
                if scrapling_result and scrapling_result.success:
                    retain_fast_path_success(scrapling_result)
                    match_links = [failed.url for failed in scrapling_result.failed]
                if scrapling_result is not None:
                    await wait_for_result_cooldown(
                        scrapling_result,
                        reason=f"{engine}_fallback",
                    )
        else:
            for engine in (ScraperEngine.SCRAPLING_HTTP.value, ScraperEngine.SCRAPLING_STEALTH.value):
                try:
                    scrapling_result = await try_scrapling(engine)
                except StaticListingRequiresBrowserError:
                    attempts.append(
                        {
                            "engine": engine,
                            "outcome": "skipped",
                            "detail": "static_listing_requires_browser",
                        }
                    )
                    break
                if isinstance(scrapling_result, ScrapeResult) and scrapling_result.is_truthful_no_fixtures():
                    return await with_execution_metadata(scrapling_result)
                if scrapling_result and scrapling_result.success and not scrapling_result.failed:
                    return await with_execution_metadata(scrapling_result)
                if scrapling_result and scrapling_result.success:
                    retain_fast_path_success(scrapling_result)
                    match_links = [failed.url for failed in scrapling_result.failed]
                    await wait_for_result_cooldown(
                        scrapling_result,
                        reason=f"{engine}_fallback",
                    )
                    continue
                discovered_links = (
                    scrapling_result.metadata.pop("_discovered_match_links", []) if scrapling_result else []
                )
                if discovered_links:
                    attempts[-1]["outcome"] = "discovery_completed"
                    match_links = list(discovered_links)
                    break
                if scrapling_result is not None:
                    await wait_for_result_cooldown(
                        scrapling_result,
                        reason=f"{engine}_fallback",
                    )
    SportMarketRegistrar.register_all_markets()
    playwright_manager = CamoufoxManager() if normalized_engine == ScraperEngine.CAMOUFOX.value else PlaywrightManager()
    cookie_dismisser = CookieDismisser()
    selection_manager = SelectionManager()
    tab_navigator = MarketTabNavigator()
    scroller = PageScroller()

    market_extractor = OddsPortalMarketExtractor(
        scroller=scroller,
        tab_navigator=tab_navigator,
        selection_manager=selection_manager,
    )

    scraper = OddsPortalScraper(
        playwright_manager=playwright_manager,
        market_extractor=market_extractor,
        scroller=scroller,
        cookie_dismisser=cookie_dismisser,
        selection_manager=selection_manager,
        preview_submarkets_only=preview_submarkets_only,
        base_url=base_url,
    )

    try:
        await scraper.start_playwright(
            headless=headless,
            browser_user_agent=browser_user_agent,
            browser_locale_timezone=browser_locale_timezone,
            browser_timezone_id=browser_timezone_id,
            proxy_manager=proxy_manager,
        )
        browser_attempted = True

        if match_links and sport:
            logger.info(f"""
                Scraping specific matches: {match_links} for sport: {sport}, markets={markets},
                scrape_odds_history={scrape_odds_history}, target_bookmaker={target_bookmaker},
                bookies_filter={bookies_filter}, period={period}
            """)
            match_result = await retry_scrape(
                scraper.scrape_matches,
                match_links=match_links,
                sport=sport,
                markets=markets,
                scrape_odds_history=scrape_odds_history,
                target_bookmaker=target_bookmaker,
                bookies_filter=bookies_filter_enum,
                period=period_enum,
                request_delay=request_delay,
                concurrent_scraping_task=concurrency_tasks,
            )
            _annotate_requested_league(match_result, requested_league_by_match_link)
            return await with_execution_metadata(match_result)

        if command == CommandEnum.HISTORIC:
            if not sport or not leagues:
                raise ValueError("Both 'sport' and 'leagues' must be provided for historic scraping.")

            printable_season = season if season else "current"
            logger.info(
                "\n                Scraping historical odds for "
                f"sport={sport}, leagues={leagues}, season={printable_season}, "
                f"markets={markets}, scrape_odds_history={scrape_odds_history}, "
                f"target_bookmaker={target_bookmaker}, max_pages={max_pages}\n            "
            )

            if len(leagues) == 1:
                league_result = await retry_scrape(
                    scraper.scrape_historic,
                    sport=sport,
                    league=leagues[0],
                    season=season,
                    markets=markets,
                    scrape_odds_history=scrape_odds_history,
                    target_bookmaker=target_bookmaker,
                    max_pages=max_pages,
                    bookies_filter=bookies_filter_enum,
                    period=period_enum,
                    request_delay=request_delay,
                    concurrent_scraping_task=concurrency_tasks,
                )
                _annotate_requested_league(league_result, leagues[0])
                return await with_execution_metadata(league_result)
            else:
                return await with_execution_metadata(
                    await _scrape_multiple_leagues(
                        scraper=scraper,
                        scrape_func=scraper.scrape_historic,
                        leagues=leagues,
                        sport=sport,
                        season=season,
                        markets=markets,
                        scrape_odds_history=scrape_odds_history,
                        target_bookmaker=target_bookmaker,
                        max_pages=max_pages,
                        bookies_filter=bookies_filter_enum,
                        period=period_enum,
                        request_delay=request_delay,
                        concurrent_scraping_task=concurrency_tasks,
                    )
                )

        elif command == CommandEnum.UPCOMING_MATCHES:
            if not date and not leagues:
                raise ValueError("Either 'date' or 'leagues' must be provided for upcoming matches scraping.")

            if leagues:
                logger.info(f"""
                    Scraping upcoming matches for sport={sport}, date={date}, leagues={leagues}, markets={markets},
                    scrape_odds_history={scrape_odds_history}, target_bookmaker={target_bookmaker}
                """)

                if len(leagues) == 1:
                    league_result = await retry_scrape(
                        scraper.scrape_upcoming,
                        sport=sport,
                        date=date,
                        league=leagues[0],
                        markets=markets,
                        scrape_odds_history=scrape_odds_history,
                        target_bookmaker=target_bookmaker,
                        bookies_filter=bookies_filter_enum,
                        period=period_enum,
                        request_delay=request_delay,
                        concurrent_scraping_task=concurrency_tasks,
                        include_started=include_started,
                    )
                    _annotate_requested_league(league_result, leagues[0])
                    return await with_execution_metadata(league_result)
                else:
                    return await with_execution_metadata(
                        await _scrape_multiple_leagues(
                            scraper=scraper,
                            scrape_func=scraper.scrape_upcoming,
                            leagues=leagues,
                            sport=sport,
                            date=date,
                            markets=markets,
                            scrape_odds_history=scrape_odds_history,
                            target_bookmaker=target_bookmaker,
                            bookies_filter=bookies_filter_enum,
                            period=period_enum,
                            request_delay=request_delay,
                            concurrent_scraping_task=concurrency_tasks,
                            include_started=include_started,
                        )
                    )
            else:
                logger.info(f"""
                    Scraping upcoming matches for sport={sport}, date={date}, markets={markets},
                    scrape_odds_history={scrape_odds_history}, target_bookmaker={target_bookmaker},
                    bookies_filter={bookies_filter}, period={period}
                """)
                return await with_execution_metadata(
                    await retry_scrape(
                        scraper.scrape_upcoming,
                        sport=sport,
                        date=date,
                        league=None,
                        markets=markets,
                        scrape_odds_history=scrape_odds_history,
                        target_bookmaker=target_bookmaker,
                        bookies_filter=bookies_filter_enum,
                        period=period_enum,
                        request_delay=request_delay,
                        concurrent_scraping_task=concurrency_tasks,
                        include_started=include_started,
                    )
                )

        else:
            raise ValueError(f"Unknown command: {command}. Supported commands are 'upcoming-matches' and 'historic'.")

    except Exception as e:
        logger.error(f"An error occurred: {e}", exc_info=True)
        raise  # Re-raise so CLI can surface the actual error to user

    finally:
        cleanup_diagnostic = await cleanup_primary_scraper(phase="final_cleanup")
        if cleanup_diagnostic is not None and completed_result is not None:
            attach_cleanup_diagnostic(completed_result, cleanup_diagnostic)


async def _scrape_multiple_leagues(
    scraper, scrape_func, leagues: list[str], sport: str | None, **kwargs
) -> ScrapeResult:
    """
    Helper function to handle multi-league scraping with error handling and logging.

    Args:
        scraper: The scraper instance
        scrape_func: The function to call for each league (scrape_historic or scrape_upcoming)
        leagues: List of leagues to scrape
        sport: The sport being scraped
        **kwargs: Additional arguments to pass to the scrape function

    Returns:
        ScrapeResult: Merged results from all leagues with combined statistics.
    """
    combined_result = ScrapeResult()
    failed_leagues = []
    all_leagues_no_fixtures = bool(leagues)
    league_by_link: dict[str, str] = {}

    logger.info(f"Starting multi-league scraping for {len(leagues)} leagues: {leagues}")

    for i, league in enumerate(leagues, 1):
        try:
            logger.info(f"[{i}/{len(leagues)}] Processing league: {league}")

            league_result = await retry_scrape(scrape_func, sport=sport, league=league, **kwargs)
            _annotate_requested_league(league_result, league)
            if isinstance(league_result, ScrapeResult):
                attempted_links = [record.get("match_link") for record in league_result.success]
                attempted_links.extend(failure.url for failure in league_result.failed)
                attempted_links.extend(partial.url for partial in league_result.partial)
                for link in attempted_links:
                    if not isinstance(link, str) or not link:
                        continue
                    owner = league_by_link.setdefault(link, league)
                    if owner != league:
                        raise RequestedLeagueProvenanceError("Requested league provenance is invalid")

            if isinstance(league_result, ScrapeResult) and league_result.is_truthful_no_fixtures():
                logger.info(f"No fixtures discovered for league: {league}")
                continue

            all_leagues_no_fixtures = False
            if league_result is None:
                error_message = "League scraper returned no result"
                logger.warning(f"{error_message} for league: {league}")
                failed_leagues.append(league)
                _record_league_failure(combined_result, sport, league, error_message, ErrorType.UNKNOWN)
                continue

            if (
                isinstance(league_result, ScrapeResult)
                and not league_result.success
                and not league_result.failed
                and not league_result.partial
                and league_result.stats.total_urls == 0
                and league_result.stats.successful == 0
                and league_result.stats.failed == 0
                and league_result.stats.partial == 0
            ):
                error_message = "League scraper returned an unattested empty result"
                logger.warning(f"{error_message} for league: {league}")
                failed_leagues.append(league)
                _record_league_failure(combined_result, sport, league, error_message, ErrorType.UNKNOWN)
                continue

            if league_result and league_result.success:
                combined_result.merge(league_result)
                logger.info(
                    f"Successfully scraped {league_result.stats.successful} matches from league: {league} "
                    f"({league_result.stats.failed} failed)"
                )
            elif league_result:
                # Result exists but no successful matches
                combined_result.merge(league_result)
                logger.warning(f"No successful matches for league: {league} ({league_result.stats.failed} failed)")
            else:
                logger.warning(f"No data returned for league: {league}")

        except RequestedLeagueProvenanceError:
            raise
        except Exception as e:
            logger.error(f"Failed to scrape league '{league}': {e}")
            failed_leagues.append(league)
            all_leagues_no_fixtures = False
            error_message = str(e)
            _record_league_failure(combined_result, sport, league, error_message)
            continue

    if all_leagues_no_fixtures:
        combined_result.metadata["discovery_outcome"] = "no_fixtures"
    else:
        combined_result.metadata.pop("discovery_outcome", None)
    if league_by_link:
        combined_result.metadata["_discovered_match_links"] = list(league_by_link)
        combined_result.metadata["_requested_league_by_match_link"] = dict(league_by_link)

    successful_leagues = len(leagues) - len(failed_leagues)

    if failed_leagues:
        logger.warning(f"Failed to scrape {len(failed_leagues)} leagues: {failed_leagues}")

    logger.info(
        f"Multi-league scraping completed: {successful_leagues}/{len(leagues)} leagues successful, "
        f"{combined_result.stats.successful} total matches scraped, "
        f"{combined_result.stats.failed} failed ({combined_result.stats.success_rate:.1f}% success rate)"
    )

    return combined_result


def _annotate_requested_league(
    result: ScrapeResult | None,
    league: str | dict[str, str],
) -> None:
    if not isinstance(result, ScrapeResult):
        return
    for record in result.success:
        requested_league = league.get(str(record.get("match_link") or "")) if isinstance(league, dict) else league
        if requested_league is not None:
            record["requested_league_slug"] = requested_league
    for partial in result.partial:
        requested_league = league.get(partial.url) if isinstance(league, dict) else league
        if requested_league is not None:
            partial.data["requested_league_slug"] = requested_league


def _record_league_failure(
    result: ScrapeResult,
    sport: str | None,
    league: str,
    error_message: str,
    error_type: ErrorType | None = None,
) -> None:
    result.failed.append(
        FailedUrl(
            url=f"league://{sport or 'unknown'}/{league}",
            error_type=error_type or classify_error(error_message),
            error_message=error_message,
            attempts=1,
            is_retryable=is_retryable_error(error_message),
        )
    )
    result.stats.total_urls += 1
    result.stats.failed += 1


def _should_try_camoufox(result: ScrapeResult | None) -> bool:
    if not isinstance(result, ScrapeResult) or result.stats.total_urls == 0:
        return False
    anti_bot_markers = ("captcha", "cloudflare", "challenge", "anti-bot", "blocked")
    messages = " ".join(failure.error_message.lower() for failure in result.failed)
    anti_bot = any(marker in messages for marker in anti_bot_markers)
    navigation_failures = sum(1 for failure in result.failed if failure.error_type.value == "navigation")
    return anti_bot or navigation_failures >= 2


async def retry_scrape(scrape_func, *args, **kwargs) -> ScrapeResult | None:
    """
    Retry a scrape function with exponential backoff for transient errors.

    Uses the unified retry_with_backoff mechanism with operation-level retry config
    (larger delays suitable for full scraping operations).

    Args:
        scrape_func: The async scraping function to execute.
        *args: Positional arguments for the function.
        **kwargs: Keyword arguments for the function.

    Returns:
        ScrapeResult from the scrape function, or None if max retries exceeded.

    Raises:
        Exception: Re-raises non-retryable errors immediately.
    """
    config = RetryConfig(
        max_attempts=OPERATION_RETRY_MAX_ATTEMPTS,
        base_delay=OPERATION_RETRY_BASE_DELAY,
        max_delay=OPERATION_RETRY_MAX_DELAY,
    )

    retry_result = await retry_with_backoff(scrape_func, *args, config=config, **kwargs)

    if retry_result.success:
        return retry_result.result

    # Preserve existing contract: non-retryable errors are re-raised
    if retry_result.last_error and not is_retryable_error(retry_result.last_error):
        logger.error(f"Non-retryable error encountered: {retry_result.last_error}")
        raise Exception(retry_result.last_error)

    logger.error(f"Max retries exceeded after {retry_result.attempts} attempts.")
    return None
