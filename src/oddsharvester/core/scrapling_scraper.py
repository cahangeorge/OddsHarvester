import asyncio
from datetime import UTC, date, datetime
import json
import logging
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from oddsharvester.core.base_scraper import _extract_fragment_match_id, _is_offscreen_row, _parse_date_header
from oddsharvester.core.market_extraction.odds_parser import OddsParser
from oddsharvester.core.odds_portal_selectors import OddsPortalSelectors
from oddsharvester.core.scrape_result import ErrorType, FailedUrl, ScrapeResult, ScrapeStats
from oddsharvester.core.url_builder import URLBuilder
from oddsharvester.utils.command_enum import CommandEnum
from oddsharvester.utils.constants import DEFAULT_REQUEST_DELAY_S, ODDSPORTAL_BASE_URL
from oddsharvester.utils.scraper_engine import ScraperEngine
from oddsharvester.utils.utils import clean_html_text

CORE_FOOTBALL_MARKETS = {"1x2", "btts", "over_under_2_5"}
MARKET_LABELS = {
    "1x2": ["1", "X", "2"],
    "btts": ["btts_yes", "btts_no"],
    "over_under_2_5": ["odds_over", "odds_under"],
}


class ScraplingUnavailableError(RuntimeError):
    """Raised when the Scrapling fast path cannot safely satisfy a request."""


class ScraplingOddsPortalScraper:
    """Small, conservative Scrapling adapter for OddsPortal core-football flows."""

    def __init__(
        self,
        *,
        engine: str,
        base_url: str | None = None,
        locale: str | None = None,
        timezone_id: str | None = None,
        proxy: dict[str, str] | None = None,
        concurrency_tasks: int = 3,
        request_delay: float = DEFAULT_REQUEST_DELAY_S,
    ) -> None:
        self.engine = engine
        self.base_url = base_url
        self.locale = locale
        self.timezone_id = timezone_id
        self.proxy = proxy
        self.concurrency_tasks = concurrency_tasks
        self.request_delay = request_delay
        self.logger = logging.getLogger(self.__class__.__name__)
        self.odds_parser = OddsParser()

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
    ) -> ScrapeResult:
        self._validate_supported_request(
            sport=sport,
            markets=markets,
            scrape_odds_history=scrape_odds_history,
        )

        links = match_links or await self._collect_links(
            command=command,
            sport=sport or "football",
            date_value=date_value,
            leagues=leagues,
            season=season,
            max_pages=max_pages,
        )
        if not links:
            return ScrapeResult(stats=ScrapeStats(total_urls=0))

        semaphore = asyncio.Semaphore(max(self.concurrency_tasks, 1))
        result = ScrapeResult(stats=ScrapeStats(total_urls=len(links)))

        async def scrape_one(index: int, link: str) -> tuple[dict[str, Any] | None, FailedUrl | None]:
            async with semaphore:
                if index > 0 and self.request_delay > 0:
                    await asyncio.sleep(self.request_delay)
                try:
                    html = await self._fetch_text(link)
                    record = self._parse_match_record(
                        html=html,
                        match_link=link,
                        markets=markets or ["1x2"],
                        target_bookmaker=target_bookmaker,
                    )
                    return record, None
                except Exception as exc:
                    return None, FailedUrl(url=link, error_type=ErrorType.UNKNOWN, error_message=str(exc))

        rows = await asyncio.gather(*(scrape_one(index, link) for index, link in enumerate(links)))
        for data, failed in rows:
            if data is not None:
                result.success.append(data)
                result.stats.successful += 1
            elif failed is not None:
                result.failed.append(failed)
                result.stats.failed += 1
        return result

    def _validate_supported_request(
        self,
        *,
        sport: str | None,
        markets: list[str] | None,
        scrape_odds_history: bool,
    ) -> None:
        requested_markets = set(markets or ["1x2"])
        if sport != "football":
            raise ScraplingUnavailableError("Scrapling fast path v1 supports football only")
        if scrape_odds_history:
            raise ScraplingUnavailableError("Scrapling fast path v1 does not support odds_history")
        if not requested_markets.issubset(CORE_FOOTBALL_MARKETS):
            raise ScraplingUnavailableError(
                f"Scrapling fast path v1 supports only core markets: {sorted(CORE_FOOTBALL_MARKETS)}"
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
    ) -> list[str]:
        collected: list[str] = []
        seen: set[str] = set()
        target_leagues = leagues or [None]

        for league in target_leagues:
            if command == CommandEnum.HISTORIC:
                if not league:
                    raise ScraplingUnavailableError("Historic Scrapling scraping requires a league")
                base = URLBuilder.get_historic_matches_url(
                    sport=sport, league=league, season=season, base_url=self.base_url
                )
                page_numbers = range(1, max(max_pages or 1, 1) + 1)
                urls = [base if page == 1 else f"{base}#/page/{page}" for page in page_numbers]
                date_filter = None
            else:
                if not date_value and not league:
                    raise ScraplingUnavailableError("Upcoming Scrapling scraping requires a date or league")
                base = URLBuilder.get_upcoming_matches_url(
                    sport=sport, date=date_value or "", league=league, base_url=self.base_url
                )
                urls = [base]
                date_filter = _parse_yyyymmdd(date_value) if league and date_value else None

            for url in urls:
                html = await self._fetch_text(url)
                for link in self._extract_match_links_from_html(html, date_filter=date_filter):
                    if link not in seen:
                        seen.add(link)
                        collected.append(link)
        return collected

    def _extract_match_links_from_html(self, html: str, date_filter: date | None = None) -> list[str]:
        soup = BeautifulSoup(html, "lxml")
        event_rows = soup.find_all(class_=re.compile(OddsPortalSelectors.EVENT_ROW_CLASS_PATTERN))
        links: list[str] = []
        current_row_date: date | None = None
        for row in event_rows:
            if _is_offscreen_row(row):
                continue
            if date_filter is not None:
                header_el = row.find(attrs={"data-testid": "date-header"})
                if header_el is not None:
                    current_row_date = _parse_date_header(header_el.get_text(" ", strip=True), self.timezone_id)
                if current_row_date is not None and current_row_date != date_filter:
                    continue
            for link in row.find_all("a", href=True):
                href = str(link["href"])
                if len(href.strip("/").split("/")) <= 3:
                    continue
                links.append(urljoin(self.base_url or ODDSPORTAL_BASE_URL, href))
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
        if not event_header_div:
            return None
        data_attribute = event_header_div.get("data")
        if not data_attribute:
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

    async def _fetch_text(self, url: str) -> str:
        if self.engine == ScraperEngine.SCRAPLING_STEALTH.value:
            return await self._fetch_stealth_text(url)
        return await asyncio.to_thread(self._fetch_http_text, url)

    def _fetch_http_text(self, url: str) -> str:
        try:
            from scrapling.fetchers import FetcherSession
        except ImportError as exc:
            raise ScraplingUnavailableError("Scrapling is not installed; install scrapling[fetchers]") from exc

        kwargs: dict[str, Any] = {"http3": True}
        if self.proxy and self.proxy.get("server"):
            kwargs["proxy"] = self.proxy["server"]
        with FetcherSession(**kwargs) as session:
            response = session.get(url, impersonate="chrome")
            return _response_text(response)

    async def _fetch_stealth_text(self, url: str) -> str:
        try:
            from scrapling.fetchers import AsyncStealthySession
        except ImportError as exc:
            raise ScraplingUnavailableError("Scrapling fetchers are not installed") from exc

        async with AsyncStealthySession(headless=True, network_idle=True, max_pages=1) as session:
            response = await session.fetch(url)
            return _response_text(response)


async def run_scrapling_scraper(**kwargs) -> ScrapeResult:
    engine = kwargs.pop("engine")
    scraper = ScraplingOddsPortalScraper(engine=engine, **kwargs.pop("scraper_options"))
    return await scraper.scrape(**kwargs)


def _response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if callable(text):
        return text()
    if isinstance(text, str):
        return text
    body = getattr(response, "body", None)
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    if isinstance(response, str):
        return response
    raise ScraplingUnavailableError("Scrapling response did not expose text content")


def _parse_yyyymmdd(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def _ascii_or_none(value: str | None) -> str | None:
    if not value:
        return None
    return value.encode("ascii", "ignore").decode("ascii")
