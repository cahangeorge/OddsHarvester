import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
import json
import logging
import random
import re
import time
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bs4 import BeautifulSoup
from bs4.element import Tag

from oddsharvester.core.base_scraper import _extract_fragment_match_id, _is_offscreen_row, _parse_date_header
from oddsharvester.core.market_extraction.odds_parser import OddsParser
from oddsharvester.core.odds_portal_selectors import OddsPortalSelectors
from oddsharvester.core.oddsportal_xhr import (
    DECODER_REVISION,
    OddsPortalXHRDecodeError,
    OddsPortalXHRSchemaError,
    build_listing_xhr_url,
    build_market_xhr_url,
    decode_xhr_payload,
    event_data_url,
    event_payload_from_static_html,
    extract_page_bootstrap,
    listing_page_metadata,
    listing_rows,
    market_rows_from_payload,
    match_record_from_event_payload,
    parse_user_data_script,
    provider_names_from_payload,
)
from oddsharvester.core.scrape_result import ErrorType, FailedUrl, ScrapeResult, ScrapeStats
from oddsharvester.core.url_builder import URLBuilder
from oddsharvester.utils.command_enum import CommandEnum
from oddsharvester.utils.constants import (
    DEFAULT_REQUEST_DELAY_S,
    ODDSPORTAL_BASE_URL,
    REQUEST_DELAY_JITTER_FACTOR,
)
from oddsharvester.utils.proxy_manager import ProxyEntry, ProxyManager
from oddsharvester.utils.scraper_engine import ScraperEngine
from oddsharvester.utils.utils import clean_html_text

XHR_FOOTBALL_MARKETS = {
    "1x2",
    "btts",
    "double_chance",
    "dnb",
    "over_under_1_5",
    "over_under_2_5",
    "over_under_3_5",
    "asian_handicap_-0_5",
}
MAX_DECODED_CACHE_ENTRIES = 256
MAX_DECODED_CACHE_BYTES = 16 * 1024 * 1024
EGRESS_CONSECUTIVE_FAILURE_THRESHOLD = 3
DEFAULT_EGRESS_COOLDOWN_BASE_SECONDS = 15.0
DEFAULT_EGRESS_COOLDOWN_MAX_SECONDS = 300.0
MARKET_LABELS = {
    "1x2": ["1", "X", "2"],
    "btts": ["btts_yes", "btts_no"],
    "double_chance": ["1X", "12", "X2"],
    "dnb": ["dnb_team1", "dnb_team2"],
    "over_under_1_5": ["odds_over", "odds_under"],
    "over_under_2_5": ["odds_over", "odds_under"],
    "over_under_3_5": ["odds_over", "odds_under"],
    "asian_handicap_-0_5": ["team1_handicap", "team2_handicap"],
}
ANTI_BOT_MARKERS = (
    "cf-chl-",
    "cloudflare",
    "captcha",
    "checking your browser",
    "verify you are human",
    "access denied",
)
NO_FIXTURES_MARKERS = (
    "no matches found",
    "no events found",
    "no upcoming matches",
    "no games found",
)
MATCH_PATH_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
MATCH_FRAGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_:-]+(?:;[A-Za-z0-9_-]+)?$")


def _looks_like_anti_bot_page(html: str) -> bool:
    normalized = html.lower()
    return any(marker in normalized for marker in ANTI_BOT_MARKERS)


def _looks_like_no_fixtures_page(html: str) -> bool:
    normalized = html.lower()
    return any(marker in normalized for marker in NO_FIXTURES_MARKERS)


def _looks_like_html(value: str) -> bool:
    return value.lstrip().lower().startswith(("<!doctype html", "<html"))


def _trusted_match_url(base_url: str, href: str) -> str | None:
    candidate = urljoin(base_url, href)
    parsed = urlsplit(candidate)
    trusted_host = urlsplit(base_url).hostname
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not trusted_host
        or parsed.hostname != trusted_host
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
    ):
        return None
    segments = [segment for segment in parsed.path.split("/") if segment]
    if (
        len(segments) != 4
        or segments[0] != "football"
        or not all(MATCH_PATH_SEGMENT_PATTERN.fullmatch(segment) for segment in segments)
        or (parsed.fragment and not MATCH_FRAGMENT_PATTERN.fullmatch(parsed.fragment))
    ):
        return None
    return candidate


class ScraplingUnavailableError(RuntimeError):
    """Raised when the Scrapling fast path cannot safely satisfy a request."""


class RequestedLeagueProvenanceError(RuntimeError):
    """Raised when a match has invalid or conflicting requested-league ownership."""


class StaticListingRequiresBrowserError(ScraplingUnavailableError):
    """Raised when a listing needs browser hydration for trusted link discovery."""


class ScraplingProxyError(ScraplingUnavailableError):
    """Raised for an egress-specific network, rate-limit, or anti-bot response."""


@dataclass(frozen=True)
class _HTTPSessionLease:
    client: Any
    proxy_key: str


class ScraplingOddsPortalScraper:
    """Small, conservative Scrapling adapter for OddsPortal core-football flows."""

    def __init__(
        self,
        *,
        engine: str,
        base_url: str | None = None,
        locale: str | None = None,
        timezone_id: str | None = None,
        geo: str = "RO",
        proxy: dict[str, str] | None = None,
        proxy_manager: ProxyManager | None = None,
        concurrency_tasks: int = 12,
        request_delay: float = DEFAULT_REQUEST_DELAY_S,
        egress_cooldown_base: float = DEFAULT_EGRESS_COOLDOWN_BASE_SECONDS,
        egress_cooldown_max: float = DEFAULT_EGRESS_COOLDOWN_MAX_SECONDS,
    ) -> None:
        self.engine = engine
        self.base_url = base_url
        self.locale = locale
        self.timezone_id = timezone_id
        self.geo = geo.upper()
        self.proxy = proxy
        self.proxy_manager = proxy_manager
        self.concurrency_tasks = concurrency_tasks
        self.request_delay = request_delay
        self.egress_cooldown_base = max(0.0, egress_cooldown_base)
        self.egress_cooldown_max = max(self.egress_cooldown_base, egress_cooldown_max)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.odds_parser = OddsParser()
        self._http_sessions: list[Any] = []
        self._http_session_pools: dict[str, asyncio.Queue[_HTTPSessionLease]] = {}
        self._proxy_entries: list[ProxyEntry] = []
        self._proxy_cursor = 0
        self._pace_locks: dict[str, asyncio.Lock] = {}
        self._next_request_at: dict[str, float] = {}
        self._egress_failures: dict[str, int] = {}
        self._egress_backoff_level: dict[str, int] = {}
        self._egress_cooldown_until: dict[str, float] = {}
        self._egress_half_open_inflight: set[str] = set()
        self._stealth_session = None
        self._stealth_proxy_key: str | None = None
        self._decoded_cache: OrderedDict[tuple[str, str, str], tuple[dict[str, Any], int]] = OrderedDict()
        self._decoded_cache_bytes = 0
        self._cache_hits = 0
        self._cache_misses = 0
        self._provider_names: dict[tuple[str, str], dict[str, str]] = {}
        self._provider_lock = asyncio.Lock()
        if self.engine == ScraperEngine.SCRAPLING_STEALTH.value:
            self.concurrency_tasks = min(self.concurrency_tasks, 3)

    async def scrape(
        self,
        *,
        command: CommandEnum,
        match_links: list[str] | None = None,
        sport: str | None = None,
        date_value: str | None = None,
        leagues: list[str] | None = None,
        season: str | None = None,
        markets: list[str] | None = None,
        max_pages: int | None = None,
        target_bookmaker: str | None = None,
        scrape_odds_history: bool = False,
        period: str | None = None,
        bookies_filter: str = "all",
        preview_submarkets_only: bool = False,
        include_started: bool = False,
        requested_league_by_match_link: dict[str, str] | None = None,
    ) -> ScrapeResult:
        self._validate_supported_request(
            sport=sport,
            markets=markets,
            scrape_odds_history=scrape_odds_history,
            period=period,
            bookies_filter=bookies_filter,
            preview_submarkets_only=preview_submarkets_only,
        )
        try:
            await self._open_session()
            return await self._scrape_open_session(
                command=command,
                match_links=match_links,
                sport=sport,
                date_value=date_value,
                leagues=leagues,
                season=season,
                markets=markets,
                max_pages=max_pages,
                target_bookmaker=target_bookmaker,
                include_started=include_started,
                requested_league_by_match_link=requested_league_by_match_link,
            )
        finally:
            await self.aclose()

    async def _scrape_open_session(
        self,
        *,
        command: CommandEnum,
        match_links: list[str] | None,
        sport: str | None,
        date_value: str | None,
        leagues: list[str] | None,
        season: str | None,
        markets: list[str] | None,
        max_pages: int | None,
        target_bookmaker: str | None,
        include_started: bool,
        requested_league_by_match_link: dict[str, str] | None = None,
    ) -> ScrapeResult:
        if match_links:
            links = match_links
            league_by_link = {
                link: requested_league_by_match_link[link]
                for link in links
                if requested_league_by_match_link and link in requested_league_by_match_link
            }
        else:
            links, league_by_link = await self._collect_links(
                command=command,
                sport=sport or "football",
                date_value=date_value,
                leagues=leagues,
                season=season,
                max_pages=max_pages,
                include_started=include_started,
            )
        if not links:
            return ScrapeResult(
                stats=ScrapeStats(total_urls=0),
                metadata={
                    "cache": {
                        "status": "memory",
                        "hits": self._cache_hits,
                        "misses": self._cache_misses,
                    },
                    "xhr_decoder_revision": DECODER_REVISION,
                    "discovery_outcome": "no_fixtures",
                    "egress": self._egress_metadata(),
                },
            )

        semaphore = asyncio.Semaphore(max(self.concurrency_tasks, 1))
        result = ScrapeResult(
            stats=ScrapeStats(total_urls=len(links)),
            metadata={
                "cache": {"status": "memory", "hits": 0, "misses": 0},
                "xhr_decoder_revision": DECODER_REVISION,
                "_discovered_match_links": list(links),
                "_requested_league_by_match_link": league_by_link,
            },
        )

        async def scrape_one(index: int, link: str) -> tuple[dict[str, Any] | None, FailedUrl | None]:
            async with semaphore:
                try:
                    record = await self._scrape_match_xhr_with_failover(
                        match_link=link,
                        markets=markets or ["1x2"],
                        target_bookmaker=target_bookmaker,
                    )
                    requested_league = league_by_link.get(link)
                    if requested_league is not None:
                        record["requested_league_slug"] = requested_league
                    return record, None
                except (
                    OddsPortalXHRDecodeError,
                    OddsPortalXHRSchemaError,
                ) as exc:
                    return None, FailedUrl(url=link, error_type=ErrorType.UNKNOWN, error_message=str(exc))
                except ScraplingProxyError as exc:
                    return None, FailedUrl(
                        url=link,
                        error_type=ErrorType.RATE_LIMITED,
                        error_message=str(exc),
                    )
                except ScraplingUnavailableError as exc:
                    return None, FailedUrl(
                        url=link,
                        error_type=ErrorType.NAVIGATION,
                        error_message=str(exc),
                    )

        rows = await asyncio.gather(*(scrape_one(index, link) for index, link in enumerate(links)))
        for data, failed in rows:
            if data is not None:
                result.success.append(data)
                result.stats.successful += 1
            elif failed is not None:
                result.failed.append(failed)
                result.stats.failed += 1
        result.metadata["cache"] = {
            "status": "memory",
            "hits": self._cache_hits,
            "misses": self._cache_misses,
        }
        result.metadata["egress"] = self._egress_metadata()
        return result

    def _validate_supported_request(
        self,
        *,
        sport: str | None,
        markets: list[str] | None,
        scrape_odds_history: bool,
        period: str | None,
        bookies_filter: str,
        preview_submarkets_only: bool,
    ) -> None:
        requested_markets = set(markets or ["1x2"])
        if not re.fullmatch(r"[A-Z]{2}", self.geo):
            raise ScraplingUnavailableError("Scrapling XHR geo must be a two-letter country code")
        if sport != "football":
            raise ScraplingUnavailableError("Scrapling fast path v1 supports football only")
        if scrape_odds_history:
            raise ScraplingUnavailableError("Scrapling fast path v1 does not support odds_history")
        if period not in (None, "full_time"):
            raise ScraplingUnavailableError("Scrapling fast path v1 supports full-time odds only")
        if bookies_filter != "all":
            raise ScraplingUnavailableError("Scrapling fast path v1 supports the all-bookies filter only")
        if preview_submarkets_only:
            raise ScraplingUnavailableError("Scrapling fast path v1 does not support preview-only markets")
        if not requested_markets.issubset(XHR_FOOTBALL_MARKETS):
            raise ScraplingUnavailableError(
                f"Scrapling XHR path supports only these football markets: {sorted(XHR_FOOTBALL_MARKETS)}"
            )

    async def _collect_links(
        self,
        *,
        command: CommandEnum,
        sport: str,
        date_value: str | None,
        leagues: list[str] | None,
        season: str | None,
        max_pages: int | None,
        include_started: bool = False,
    ) -> tuple[list[str], dict[str, str]]:
        target_leagues = leagues or [None]
        listing_semaphore = asyncio.Semaphore(max(1, min(self.concurrency_tasks, len(target_leagues))))

        async def collect_for_league(league: str | None) -> list[str]:
            async with listing_semaphore:
                if command == CommandEnum.HISTORIC:
                    if not league:
                        raise ScraplingUnavailableError("Historic Scrapling scraping requires a league")
                    base = URLBuilder.get_historic_matches_url(
                        sport=sport, league=league, season=season, base_url=self.base_url
                    )
                    date_filter = None
                else:
                    if not date_value and not league:
                        raise ScraplingUnavailableError("Upcoming Scrapling scraping requires a date or league")
                    base = URLBuilder.get_upcoming_matches_url(
                        sport=sport, date=date_value or "", league=league, base_url=self.base_url
                    )
                    date_filter = _parse_yyyymmdd(date_value) if league and date_value else None

                try:
                    return await self._collect_listing_xhr(
                        page_url=base,
                        date_filter=date_filter,
                        max_pages=max_pages,
                        skip_started=command == CommandEnum.UPCOMING_MATCHES and not include_started,
                    )
                except (OddsPortalXHRDecodeError, OddsPortalXHRSchemaError) as exc:
                    raise StaticListingRequiresBrowserError(
                        f"OddsPortal listing XHR contract changed for {base}"
                    ) from exc

        batches = await asyncio.gather(*(collect_for_league(league) for league in target_leagues))
        collected: list[str] = []
        league_by_link: dict[str, str] = {}
        owners: dict[str, str | None] = {}
        ambiguous: set[str] = set()
        for league, discovered in zip(target_leagues, batches, strict=True):
            for link in discovered:
                if link not in owners:
                    owners[link] = league
                    collected.append(link)
                    if league is not None:
                        league_by_link[link] = league
                elif owners[link] != league:
                    ambiguous.add(link)
        if ambiguous:
            raise RequestedLeagueProvenanceError("Requested league provenance is invalid")
        return collected, league_by_link

    async def _collect_listing_xhr(
        self,
        *,
        page_url: str,
        date_filter: date | None,
        max_pages: int | None,
        skip_started: bool = False,
    ) -> list[str]:
        attempts = (
            2
            if self.engine == ScraperEngine.SCRAPLING_HTTP.value
            and self.proxy_manager
            and self.proxy_manager.is_multi_proxy()
            else 1
        )
        tried_proxy_keys: set[str] = set()
        last_error: ScraplingProxyError | None = None
        for _ in range(attempts):
            try:
                async with self._lease_http_session(exclude_proxy_keys=tried_proxy_keys) as lease:
                    if lease is not None:
                        tried_proxy_keys.add(lease.proxy_key)
                    return await self._collect_listing_xhr_with_lease(
                        page_url=page_url,
                        date_filter=date_filter,
                        max_pages=max_pages,
                        skip_started=skip_started,
                        lease=lease,
                    )
            except ScraplingProxyError as exc:
                last_error = exc
        raise last_error or ScraplingUnavailableError("No healthy Scrapling listing session is available")

    async def _collect_listing_xhr_with_lease(
        self,
        *,
        page_url: str,
        date_filter: date | None,
        max_pages: int | None,
        skip_started: bool,
        lease: _HTTPSessionLease | None,
    ) -> list[str]:
        html = await self._fetch_text_with_lease(page_url, lease)
        if _looks_like_anti_bot_page(html):
            raise ScraplingProxyError(f"Anti-bot response detected for {page_url}")
        request_base, user_data_url = extract_page_bootstrap(html, page_url=page_url)
        user_data = parse_user_data_script(await self._fetch_text_with_lease(user_data_url, lease))
        first_url = build_listing_xhr_url(
            request_base_url=request_base,
            bookiehash=user_data["bookiehash"],
            page=1,
            timestamp_ms=int(time.time() * 1000),
        )
        first_payload = await self._fetch_decoded(first_url, lease)
        rows, page_count = listing_rows(first_payload)
        total, one_page, current_page = listing_page_metadata(first_payload)
        if current_page != 1:
            raise OddsPortalXHRSchemaError("OddsPortal listing first response is not page 1")
        if not rows and total != 0:
            raise OddsPortalXHRSchemaError("OddsPortal empty listing payload does not attest total=0")
        page_limit = min(page_count, max_pages) if max_pages else page_count
        all_rows = list(rows)
        for page in range(2, page_limit + 1):
            next_page_url = build_listing_xhr_url(
                request_base_url=request_base,
                bookiehash=user_data["bookiehash"],
                page=page,
                timestamp_ms=int(time.time() * 1000),
            )
            payload = await self._fetch_decoded(next_page_url, lease)
            page_rows, _ = listing_rows(payload)
            page_total, page_size, current_page = listing_page_metadata(payload)
            if page_total != total or page_size != one_page or current_page != page:
                raise OddsPortalXHRSchemaError("OddsPortal listing pagination response is inconsistent")
            all_rows.extend(page_rows)

        identities = [str(row.get("id") or row.get("url") or "") for row in all_rows]
        if any(not identity for identity in identities) or len(set(identities)) != len(identities):
            raise OddsPortalXHRSchemaError("OddsPortal listing pagination contains duplicate rows")
        expected_rows = min(total, one_page * page_limit)
        if len(all_rows) != expected_rows:
            raise OddsPortalXHRSchemaError(
                f"OddsPortal listing returned {len(all_rows)} rows, expected {expected_rows}"
            )

        links: list[str] = []
        trusted_row_count = 0
        for row in all_rows:
            if date_filter is not None:
                row_date = _listing_row_date(row, self.timezone_id)
                if row_date is None:
                    raise OddsPortalXHRSchemaError("OddsPortal listing row does not expose a parseable start timestamp")
                if row_date != date_filter:
                    continue
            href = row.get("url")
            if not isinstance(href, str):
                continue
            trusted = _trusted_match_url(self.base_url or ODDSPORTAL_BASE_URL, href)
            if trusted:
                trusted_row_count += 1
                if skip_started and _listing_row_has_started(row):
                    continue
                links.append(trusted)
        if not links and all_rows and date_filter is None and trusted_row_count == 0:
            raise OddsPortalXHRSchemaError("OddsPortal listing rows do not expose trusted match URLs")
        return links

    async def _scrape_match_xhr_with_failover(
        self,
        *,
        match_link: str,
        markets: list[str],
        target_bookmaker: str | None,
    ) -> dict[str, Any]:
        attempts = (
            2
            if self.engine == ScraperEngine.SCRAPLING_HTTP.value
            and self.proxy_manager
            and self.proxy_manager.is_multi_proxy()
            else 1
        )
        last_error: ScraplingProxyError | None = None
        tried_proxy_keys: set[str] = set()
        for _ in range(attempts):
            try:
                async with self._lease_http_session(exclude_proxy_keys=tried_proxy_keys) as lease:
                    if lease is not None:
                        tried_proxy_keys.add(lease.proxy_key)
                    return await self._scrape_match_xhr(
                        match_link=match_link,
                        markets=markets,
                        target_bookmaker=target_bookmaker,
                        lease=lease,
                    )
            except ScraplingProxyError as exc:
                last_error = exc
        raise last_error or ScraplingUnavailableError("No healthy Scrapling HTTP session is available")

    async def _scrape_match_xhr(
        self,
        *,
        match_link: str,
        markets: list[str],
        target_bookmaker: str | None,
        lease: _HTTPSessionLease | None,
    ) -> dict[str, Any]:
        try:
            event_payload = await self._fetch_decoded(
                event_data_url(match_link, base_url=self.base_url),
                lease,
            )
        except ScraplingProxyError as xhr_error:
            parsed = urlsplit(match_link)
            static_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
            try:
                static_html = await self._fetch_text_with_lease(
                    static_url,
                    lease,
                    report_success=False,
                )
                event_payload = event_payload_from_static_html(static_html, match_link=match_link)
            except OddsPortalXHRSchemaError as fallback_error:
                if lease is not None:
                    self._report_proxy_result(lease.proxy_key, is_proxy_failure=True)
                raise xhr_error from fallback_error
        record = match_record_from_event_payload(event_payload, match_link=match_link)
        providers = await self._get_provider_names(lease)
        locale = self.locale if self.locale and re.fullmatch(r"[a-z]{2}(?:-[A-Z]{2})?", self.locale) else "en"
        for market in markets:
            market_url = build_market_xhr_url(
                event_payload,
                market=market,
                base_url=self.base_url,
                geo=self.geo,
                locale=locale,
            )
            market_payload = await self._fetch_decoded(market_url, lease)
            record[f"{market}_market"] = market_rows_from_payload(
                market_payload,
                market=market,
                provider_names=providers,
                target_bookmaker=target_bookmaker,
            )
        record["_scraper_engine"] = self.engine
        record["_xhr_decoder_revision"] = DECODER_REVISION
        return record

    async def _get_provider_names(self, lease: _HTTPSessionLease | None) -> dict[str, str]:
        provider_key = (self._egress_cache_key(lease), self.geo)
        cached = self._provider_names.get(provider_key)
        if cached is not None:
            self._cache_hits += 1
            return cached
        async with self._provider_lock:
            cached = self._provider_names.get(provider_key)
            if cached is None:
                origin = (self.base_url or ODDSPORTAL_BASE_URL).rstrip("/")
                payload = await self._fetch_decoded(
                    f"{origin}/ajax-providers-bonus-data/0/?logged=false",
                    lease,
                )
                cached = provider_names_from_payload(payload)
                self._provider_names[provider_key] = cached
        return cached

    async def _fetch_decoded(
        self,
        url: str,
        lease: _HTTPSessionLease | None,
    ) -> dict[str, Any]:
        cache_key = (self._egress_cache_key(lease), self.geo, url)
        cached_entry = self._decoded_cache.get(cache_key)
        if cached_entry is not None:
            self._cache_hits += 1
            self._decoded_cache.move_to_end(cache_key)
            return cached_entry[0]
        self._cache_misses += 1
        raw_payload = await self._fetch_text_with_lease(url, lease, report_success=False)
        if raw_payload.lstrip().lower().startswith(("<!doctype html", "<html")):
            if lease is not None:
                self._report_proxy_result(lease.proxy_key, is_proxy_failure=True)
            elif self._stealth_proxy_key is not None:
                self._report_proxy_result(self._stealth_proxy_key, is_proxy_failure=True)
            raise ScraplingProxyError(f"OddsPortal returned an HTML soft block for XHR endpoint {url}")
        if lease is not None:
            self._report_proxy_result(lease.proxy_key, is_proxy_failure=False)
        elif self._stealth_proxy_key is not None:
            self._report_proxy_result(self._stealth_proxy_key, is_proxy_failure=False)
        payload = decode_xhr_payload(raw_payload)
        payload_size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        self._decoded_cache[cache_key] = (payload, payload_size)
        self._decoded_cache_bytes += payload_size
        self._decoded_cache.move_to_end(cache_key)
        while (
            len(self._decoded_cache) > MAX_DECODED_CACHE_ENTRIES or self._decoded_cache_bytes > MAX_DECODED_CACHE_BYTES
        ):
            _, (_, evicted_size) = self._decoded_cache.popitem(last=False)
            self._decoded_cache_bytes -= evicted_size
        return payload

    def _egress_cache_key(self, lease: _HTTPSessionLease | None) -> str:
        if lease is not None:
            return lease.proxy_key
        return self._stealth_proxy_key or "direct"

    def _extract_match_links_from_html(self, html: str, date_filter: date | None = None) -> list[str]:
        soup = BeautifulSoup(html, "lxml")
        event_rows = soup.find_all(class_=re.compile(OddsPortalSelectors.EVENT_ROW_CLASS_PATTERN))
        links: list[str] = []
        current_row_date: date | None = None
        for row in event_rows:
            if not isinstance(row, Tag) or _is_offscreen_row(row):
                continue
            if date_filter is not None:
                header_el = row.find(attrs={"data-testid": "date-header"})
                if isinstance(header_el, Tag):
                    current_row_date = _parse_date_header(header_el.get_text(" ", strip=True), self.timezone_id)
                if current_row_date is not None and current_row_date != date_filter:
                    continue
            for link in row.find_all("a", href=True):
                if not isinstance(link, Tag):
                    continue
                href = link.get("href")
                if not isinstance(href, str):
                    continue
                trusted_url = _trusted_match_url(self.base_url or ODDSPORTAL_BASE_URL, href)
                if trusted_url is not None:
                    links.append(trusted_url)
        return links

    def _parse_match_record(
        self,
        *,
        html: str,
        match_link: str,
        markets: list[str],
        target_bookmaker: str | None,
    ) -> dict[str, Any]:
        record = self._parse_match_details(html=html, match_link=match_link)
        if not record:
            raise ScraplingUnavailableError("Scrapling could not parse match details")

        for market in markets:
            labels = MARKET_LABELS.get(market)
            if not labels:
                raise ScraplingUnavailableError(f"Unsupported Scrapling market: {market}")
            rows = self.odds_parser.parse_market_odds(
                html_content=html,
                period="FullTime",
                odds_labels=labels,
                target_bookmaker=target_bookmaker,
            )
            if not rows:
                raise ScraplingUnavailableError(f"Scrapling could not parse market rows for {market}")
            record[f"{market}_market"] = rows

        record["_scraper_engine"] = self.engine
        return record

    def _parse_match_details(self, *, html: str, match_link: str) -> dict[str, Any] | None:
        soup = BeautifulSoup(html, "html.parser")
        event_header_div = soup.find("div", id="react-event-header")
        if not isinstance(event_header_div, Tag):
            return None
        data_attribute = event_header_div.get("data")
        if not isinstance(data_attribute, str) or not data_attribute:
            return None
        try:
            json_data = json.loads(data_attribute)
        except (TypeError, json.JSONDecodeError):
            return None

        event_body = json_data.get("eventBody", {})
        event_data = json_data.get("eventData", {})
        fragment_match_id = _extract_fragment_match_id(match_link)
        event_id = str(event_data.get("id") or "")
        if fragment_match_id and event_id and fragment_match_id != event_id:
            raise ScraplingUnavailableError("Scrapling received stale OddsPortal event payload")
        match_date = (
            datetime.fromtimestamp(event_body["startDate"], tz=UTC).strftime("%Y-%m-%d %H:%M:%S %Z")
            if event_body.get("startDate")
            else None
        )

        return {
            "scraped_date": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S %Z"),
            "match_date": match_date,
            "match_link": match_link,
            "home_team": event_data.get("home"),
            "away_team": event_data.get("away"),
            "league_name": event_data.get("tournamentName"),
            "home_score": str(event_body["homeResult"]) if event_body.get("homeResult") is not None else None,
            "away_score": str(event_body["awayResult"]) if event_body.get("awayResult") is not None else None,
            "partial_results": clean_html_text(event_body.get("partialresult")),
            "venue": _ascii_or_none(event_body.get("venue")),
            "venue_town": _ascii_or_none(event_body.get("venueTown")),
            "venue_country": event_body.get("venueCountry"),
        }

    async def _open_session(self) -> None:
        if self.engine == ScraperEngine.SCRAPLING_STEALTH.value:
            try:
                from scrapling.fetchers import AsyncStealthySession
            except ImportError as exc:
                raise ScraplingUnavailableError("Scrapling fetchers are not installed") from exc
            kwargs: dict[str, Any] = {"headless": True, "network_idle": True, "max_pages": 1}
            if self.proxy_manager is not None:
                entry = self.proxy_manager.next_proxy()
                if entry is None:
                    raise ScraplingProxyError("All configured proxies are unhealthy")
                self._stealth_proxy_key = entry.key
                proxy_url = _scrapling_proxy_url(entry.config)
                if proxy_url:
                    kwargs["proxy"] = proxy_url
            elif self.proxy:
                self._stealth_proxy_key = self.proxy.get("server", "direct")
                proxy_url = _scrapling_proxy_url(self.proxy)
                if proxy_url:
                    kwargs["proxy"] = proxy_url
            else:
                self._stealth_proxy_key = "direct"
            session = AsyncStealthySession(**kwargs)
            await session.__aenter__()
            self._stealth_session = session
            return
        try:
            from scrapling.fetchers import FetcherSession
        except ImportError as exc:
            raise ScraplingUnavailableError("Scrapling is not installed; install scrapling[fetchers]") from exc
        if self.proxy_manager is not None:
            self._proxy_entries = list(self.proxy_manager.entries)
        else:
            self._proxy_entries = [
                ProxyEntry(
                    key=self.proxy.get("server", "direct") if self.proxy else "direct",
                    config=self.proxy,
                )
            ]
        session_count = max(1, self.concurrency_tasks, len(self._proxy_entries))
        for entry in self._proxy_entries:
            self._http_session_pools[entry.key] = asyncio.Queue()
            self._pace_locks[entry.key] = asyncio.Lock()
        for index in range(session_count):
            entry = self._proxy_entries[index % len(self._proxy_entries)]
            kwargs: dict[str, Any] = {
                "impersonate": "chrome",
                "timeout": 15,
                "retries": 1,
            }
            proxy_url = _scrapling_proxy_url(entry.config)
            if proxy_url:
                kwargs["proxy"] = proxy_url
            session_manager = FetcherSession(**kwargs)
            session = session_manager.__enter__()
            self._http_sessions.append(session_manager)
            self._http_session_pools[entry.key].put_nowait(_HTTPSessionLease(client=session, proxy_key=entry.key))

    async def aclose(self) -> None:
        stealth_session = self._stealth_session
        http_sessions = self._http_sessions
        self._stealth_session = None
        self._stealth_proxy_key = None
        self._http_sessions = []
        self._http_session_pools = {}
        self._proxy_entries = []
        self._pace_locks = {}
        self._next_request_at = {}
        self._egress_failures = {}
        self._egress_backoff_level = {}
        self._egress_cooldown_until = {}
        self._egress_half_open_inflight = set()
        if stealth_session is not None:
            try:
                await stealth_session.__aexit__(None, None, None)
            except Exception:
                self.logger.warning("Failed to close Scrapling stealth session", exc_info=True)
        for session in http_sessions:
            try:
                session.__exit__(None, None, None)
            except Exception:
                self.logger.warning("Failed to close Scrapling HTTP session", exc_info=True)

    async def _fetch_text(self, url: str) -> str:
        if self._stealth_session is not None:
            response = await self._stealth_session.fetch(url)
            return _response_text(response)
        async with self._lease_http_session() as lease:
            return await self._fetch_text_with_lease(url, lease)

    @asynccontextmanager
    async def _lease_http_session(self, *, exclude_proxy_keys: set[str] | None = None):
        if self._stealth_session is not None:
            yield None
            return
        entry = self._next_proxy_entry(exclude_proxy_keys=exclude_proxy_keys)
        if entry is None:
            raise ScraplingProxyError("All configured proxies are unhealthy")
        pool = self._http_session_pools.get(entry.key)
        if pool is None:
            raise ScraplingUnavailableError("Scrapling session is not open")
        lease = await pool.get()
        try:
            yield lease
        finally:
            pool.put_nowait(lease)

    def _next_proxy_entry(self, *, exclude_proxy_keys: set[str] | None = None) -> ProxyEntry | None:
        if self.proxy_manager is not None:
            return self.proxy_manager.next_proxy(exclude_keys=exclude_proxy_keys)
        if not self._proxy_entries:
            return None
        excluded = exclude_proxy_keys or set()
        for _ in range(len(self._proxy_entries)):
            entry = self._proxy_entries[self._proxy_cursor % len(self._proxy_entries)]
            self._proxy_cursor += 1
            if entry.key not in excluded:
                return entry
        return None

    async def _fetch_text_with_lease(
        self,
        url: str,
        lease: _HTTPSessionLease | None,
        *,
        report_success: bool = True,
    ) -> str:
        if lease is None:
            if self._stealth_session is None:
                raise ScraplingUnavailableError("Scrapling session is not open")
            stealth_key = self._stealth_proxy_key or "direct"
            half_open_probe = await self._pace(stealth_key)
            if self._is_egress_unhealthy(
                stealth_key,
                allow_half_open_probe=half_open_probe,
            ) or (self.proxy_manager is not None and self.proxy_manager.is_blacklisted(stealth_key)):
                raise ScraplingProxyError("Stealth proxy is unhealthy; falling back to the browser engine")
            try:
                response = await self._stealth_session.fetch(url)
            except Exception as exc:
                self._report_proxy_result(stealth_key, is_proxy_failure=True)
                raise ScraplingProxyError(f"Stealth proxy request failed for {url}") from exc
            text = _response_text(response)
            if _looks_like_html(text) and _looks_like_anti_bot_page(text):
                self._report_proxy_result(stealth_key, is_proxy_failure=True)
                raise ScraplingProxyError(f"Anti-bot response detected for {url}")
            if report_success:
                self._report_proxy_result(stealth_key, is_proxy_failure=False)
            return text

        half_open_probe = await self._pace(lease.proxy_key)
        if self._is_egress_unhealthy(
            lease.proxy_key,
            allow_half_open_probe=half_open_probe,
        ):
            raise ScraplingProxyError(f"Egress circuit breaker is open for {lease.proxy_key}")
        try:
            response = await asyncio.to_thread(lease.client.get, url)
        except Exception as exc:
            self._report_proxy_result(lease.proxy_key, is_proxy_failure=True)
            raise ScraplingProxyError(f"Proxy request failed for {url}") from exc
        status = _response_status(response)
        text = _response_text(response)
        if status in {403, 407, 429} or (_looks_like_html(text) and _looks_like_anti_bot_page(text)):
            self._report_proxy_result(lease.proxy_key, is_proxy_failure=True)
            raise ScraplingProxyError(f"Proxy was blocked or rate-limited for {url} (HTTP {status})")
        if status is not None and status >= 400:
            self._report_proxy_result(lease.proxy_key, is_proxy_failure=False)
            raise ScraplingUnavailableError(f"OddsPortal request failed for {url} (HTTP {status})")
        if report_success:
            self._report_proxy_result(lease.proxy_key, is_proxy_failure=False)
        return text

    async def _pace(self, proxy_key: str) -> bool:
        half_open_probe = False
        lock = self._pace_locks.setdefault(proxy_key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            wait_until = max(
                self._next_request_at.get(proxy_key, now),
                self._egress_cooldown_until.get(proxy_key, now),
            )
            wait_for = wait_until - now
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            now = time.monotonic()
            if (
                self._egress_failures.get(proxy_key, 0) >= EGRESS_CONSECUTIVE_FAILURE_THRESHOLD
                and self._egress_cooldown_until.get(proxy_key, 0) <= now
                and proxy_key not in self._egress_half_open_inflight
            ):
                # Reserve exactly one half-open request. Other concurrent
                # callers remain behind the open circuit until it reports.
                self._egress_half_open_inflight.add(proxy_key)
                self._egress_failures[proxy_key] = EGRESS_CONSECUTIVE_FAILURE_THRESHOLD - 1
                half_open_probe = True
            jitter = self.request_delay * REQUEST_DELAY_JITTER_FACTOR * random.random()  # noqa: S311
            self._next_request_at[proxy_key] = now + self.request_delay + jitter
        return half_open_probe

    def _report_proxy_result(self, proxy_key: str, *, is_proxy_failure: bool) -> None:
        self._egress_half_open_inflight.discard(proxy_key)
        if is_proxy_failure:
            self._egress_failures[proxy_key] = self._egress_failures.get(proxy_key, 0) + 1
            level = min(self._egress_backoff_level.get(proxy_key, 0) + 1, 16)
            self._egress_backoff_level[proxy_key] = level
            cooldown = min(
                self.egress_cooldown_base * (2 ** (level - 1)),
                self.egress_cooldown_max,
            )
            self._egress_cooldown_until[proxy_key] = max(
                self._egress_cooldown_until.get(proxy_key, 0),
                time.monotonic() + cooldown,
            )
        else:
            self._egress_failures[proxy_key] = 0
            self._egress_backoff_level[proxy_key] = 0
            self._egress_cooldown_until.pop(proxy_key, None)
        if self.proxy_manager is not None:
            self.proxy_manager.report_result(proxy_key, is_proxy_failure=is_proxy_failure)

    def _is_egress_unhealthy(
        self,
        proxy_key: str,
        *,
        allow_half_open_probe: bool = False,
    ) -> bool:
        return self._egress_failures.get(proxy_key, 0) >= EGRESS_CONSECUTIVE_FAILURE_THRESHOLD or (
            proxy_key in self._egress_half_open_inflight and not allow_half_open_probe
        )

    def _egress_metadata(self) -> dict[str, Any]:
        now = time.monotonic()
        cooldown_remaining = max(
            (max(0.0, deadline - now) for deadline in self._egress_cooldown_until.values()),
            default=0.0,
        )
        return {
            "mode": "multi_proxy"
            if self.proxy_manager is not None and self.proxy_manager.is_multi_proxy()
            else "direct_or_single",
            "max_consecutive_failures": max(self._egress_failures.values(), default=0),
            "cooldown_remaining_seconds": round(cooldown_remaining, 3),
            "open_circuits": sum(
                failures >= EGRESS_CONSECUTIVE_FAILURE_THRESHOLD for failures in self._egress_failures.values()
            ),
        }


async def run_scrapling_scraper(**kwargs) -> ScrapeResult:
    engine = kwargs.pop("engine")
    scraper = ScraplingOddsPortalScraper(engine=engine, **kwargs.pop("scraper_options"))
    return await scraper.scrape(**kwargs)


def _response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    rendered_text: str | None = None
    if callable(text):
        rendered_text = _coerce_response_text(text())
        if rendered_text:
            return rendered_text
    elif text is not None:
        rendered_text = _coerce_response_text(text)
        if rendered_text:
            return rendered_text
    body = getattr(response, "body", None)
    if body is not None:
        return _coerce_response_text(body)
    if rendered_text is not None:
        return rendered_text
    if isinstance(response, str):
        return response
    raise ScraplingUnavailableError("Scrapling response did not expose text content")


def _response_status(response: Any) -> int | None:
    for attribute in ("status", "status_code"):
        value = getattr(response, attribute, None)
        if isinstance(value, int):
            return value
    return None


def _coerce_response_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _parse_yyyymmdd(value: str | None) -> date | None:
    if not value:
        return None
    for date_format in ("%Y%m%d", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue
    return None


def _listing_row_date(row: dict[str, Any], timezone_id: str | None) -> date | None:
    timestamp = next(
        (
            row.get(key)
            for key in ("date-start-timestamp", "startTimestamp", "startDate", "date")
            if row.get(key) is not None
        ),
        None,
    )
    if not isinstance(timestamp, str | int | float):
        return None
    try:
        numeric = float(timestamp)
    except (TypeError, ValueError):
        return None
    if numeric > 10_000_000_000:
        numeric /= 1_000
    timezone = UTC
    if timezone_id:
        try:
            timezone = ZoneInfo(timezone_id)
        except ZoneInfoNotFoundError:
            timezone = UTC
    try:
        return datetime.fromtimestamp(numeric, tz=timezone).date()
    except (OSError, OverflowError, ValueError):
        return None


def _listing_row_has_started(row: dict[str, Any]) -> bool:
    status_id = row.get("status-id")
    if status_id in {2, 3}:
        return True
    stage = str(row.get("event-stage-name") or "").casefold()
    return stage in {
        "finished",
        "in progress",
        "live",
        "after extra time",
        "after penalties",
    }


def _scrapling_proxy_url(config: dict[str, str] | None) -> str | None:
    if not config or not config.get("server"):
        return None
    server = config["server"]
    username = config.get("username")
    password = config.get("password")
    if not username or password is None:
        return server
    parsed = urlsplit(server)
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    credentials = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    return urlunsplit((parsed.scheme, f"{credentials}{host}{port}", parsed.path, parsed.query, parsed.fragment))


def _ascii_or_none(value: str | None) -> str | None:
    if not value:
        return None
    return value.encode("ascii", "ignore").decode("ascii")
