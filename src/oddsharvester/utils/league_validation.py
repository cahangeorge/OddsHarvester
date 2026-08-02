"""Safe, lightweight validation for discovered football league URLs.

Discovery only proves a link appears in OddsPortal's catalog. A candidate is
considered reachable only when its rendered ``/results/`` page contains match
links; the backend can then inject that verified URL into one scraper process.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from oddsharvester.utils.constants import ODDSPORTAL_BASE_URL
from oddsharvester.utils.league_catalog import normalize_football_league_url

ANTI_BOT_MARKERS = ("captcha", "cloudflare", "access denied", "rate limit", "challenge")
EVENT_ROW_PATTERN = re.compile(r"eventRow", re.IGNORECASE)


@dataclass(frozen=True)
class FootballLeagueValidation:
    status: str
    detail: str
    match_count: int = 0
    season_alias: str | None = None
    historic_url: str | None = None


def football_results_url(source_url: str, season: str | None = None) -> str | None:
    """Build the current or exact historic Results URL for a direct football league URL."""
    candidate = normalize_football_league_url(source_url)
    if candidate is None:
        return None
    if season:
        return f"{candidate.url.rstrip('/')}-{season}/results/"
    return f"{candidate.url}results/"


def football_historic_results_urls(source_url: str, season: str, *, current_year: int | None = None) -> list[str]:
    """Return safe OddsPortal season conventions, range first then calendar years."""
    candidate = normalize_football_league_url(source_url)
    if candidate is None:
        return []
    current_year = current_year or datetime.now(UTC).year
    values = [season]
    urls: list[str] = []
    if re.fullmatch(r"\d{4}-\d{4}", season):
        start_year, end_year = season.split("-")
        if int(start_year) == current_year:
            urls.append(f"{candidate.url}results/")
        values.extend([start_year, end_year])
    urls.extend(f"{candidate.url.rstrip('/')}-{value}/results/" for value in values)
    return list(dict.fromkeys(urls))


def validate_football_results_page(
    source_url: str,
    final_url: str,
    html: str,
    *,
    season: str | None = None,
    attempted_url: str | None = None,
) -> FootballLeagueValidation:
    """Classify one rendered Results page and derive a historic alias when redirected."""
    expected_url = attempted_url or football_results_url(source_url, season)
    if expected_url is None:
        return FootballLeagueValidation("unavailable", "Candidate URL is not a direct OddsPortal football league URL.")

    normalized_html = html.lower()
    if any(marker in normalized_html for marker in ANTI_BOT_MARKERS):
        return FootballLeagueValidation(
            "validation_pending", "OddsPortal returned an anti-bot or rate-limit challenge."
        )

    expected_path = urlsplit(expected_url).path.rstrip("/")
    parsed_final = urlsplit(final_url)
    hostname = (parsed_final.hostname or "").lower()
    if (
        parsed_final.scheme != "https"
        or not (hostname == "oddsportal.com" or hostname.endswith(".oddsportal.com"))
        or parsed_final.port not in (None, 443)
        or parsed_final.username is not None
        or parsed_final.password is not None
        or parsed_final.query
        or parsed_final.fragment
    ):
        return FootballLeagueValidation("unavailable", "Results page redirected to an unsafe final URL.")
    actual_path = parsed_final.path.rstrip("/")
    season_alias = None
    if actual_path != expected_path:
        candidate = normalize_football_league_url(source_url)
        parts = [part for part in actual_path.split("/") if part]
        suffix = f"-{season}" if season else ""
        if (
            candidate is None
            or not season
            or len(parts) != 4
            or parts[:2] != ["football", candidate.country_slug]
            or parts[-1] != "results"
            or not parts[2].endswith(suffix)
        ):
            return FootballLeagueValidation(
                "unavailable", f"Results page redirected to unexpected path: {actual_path or '/'}"
            )
        season_alias = parts[2].removesuffix(suffix)

    soup = BeautifulSoup(html, "lxml")
    links = {
        anchor["href"]
        for row in soup.find_all(class_=EVENT_ROW_PATTERN)
        for anchor in row.find_all("a", href=True)
        if len(anchor["href"].strip("/").split("/")) > 3
    }
    if not links:
        return FootballLeagueValidation("unavailable", "Rendered results page contained no match links.")

    return FootballLeagueValidation(
        "available",
        "Results page passed; promoted to the runtime OddsHarvester league registry.",
        len(links),
        season_alias,
        final_url if season else None,
    )


def absolute_match_links(html: str) -> set[str]:
    """Expose validated match-link extraction for validation diagnostics."""
    soup = BeautifulSoup(html, "lxml")
    return {
        f"{ODDSPORTAL_BASE_URL}{href}"
        for row in soup.find_all(class_=EVENT_ROW_PATTERN)
        for anchor in row.find_all("a", href=True)
        if len((href := anchor["href"]).strip("/").split("/")) > 3
    }
