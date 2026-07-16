"""
League season aliases for leagues that changed sponsor names.

Some leagues on OddsPortal change their URL slug when sponsors change.
For example, Czech Republic's top league was "fortuna-liga" until 2023-2024,
then became "chance-liga" from 2024-2025 onwards.

This module provides a mapping to resolve the correct URL slug for a given season.
"""

import json
import os
import re
from urllib.parse import urlsplit

from .sport_market_constants import Sport

# League Season Aliases
# Format: canonical_league_key -> {max_year: url_slug}
# - canonical_league_key: The league key as defined in SPORTS_LEAGUES_URLS_MAPPING
# - max_year: The LAST season start year that uses this alias
# - url_slug: The URL slug to use for seasons up to and including max_year
#
# Seasons after max_year use the canonical (default) slug from SPORTS_LEAGUES_URLS_MAPPING
LEAGUE_SEASON_ALIASES: dict[Sport, dict[str, dict[int, str]]] = {
    Sport.FOOTBALL: {
        # Czech Republic: fortuna-liga until 2023-2024, then chance-liga
        "czech-republic-chance-liga": {
            2023: "fortuna-liga",
        },
        # Slovakia: fortuna-liga until 2023-2024, then nike-liga
        "slovakia-nike-liga": {
            2023: "fortuna-liga",
        },
        # Hungary: otp-bank-liga until 2023-2024, then nb-i
        "hungary-nb-i": {
            2023: "otp-bank-liga",
        },
        # Brazil: serie-a until 2023, then serie-a-betano from 2024
        "brazil-serie-a": {
            2023: "serie-a",
        },
        # South Africa: premier-league until 2023-2024, then betway-premiership
        "south-africa-premiership": {
            2023: "premier-league",
        },
        # Bulgaria: parva-liga until 2024-2025, then efbet-league from 2025-2026
        # (also a-pfg until 2015-2016, but parva-liga alias covers the more recent range)
        "bulgaria-parva-liga": {
            2024: "parva-liga",
        },
        # World Cup: current page is year-branded as world-championship-2026,
        # while older tournament pages use world-cup-YYYY.
        "world-cup": {
            2022: "world-cup",
        },
    },
}


def runtime_football_season_alias(raw: str | None, league: str, season: str | None) -> str | None:
    """Read an exact season alias that was verified immediately before a scrape."""
    if not raw or not season:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    by_league = payload.get(league)
    alias = by_league.get(season) if isinstance(by_league, dict) else None
    return alias if isinstance(alias, str) and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", alias) else None


def runtime_football_historic_url(raw: str | None, league: str, season: str | None) -> str | None:
    """Read an exact Results URL that was rendered and validated for this job."""
    if not raw or not season:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    by_league = payload.get(league)
    url = by_league.get(season) if isinstance(by_league, dict) else None
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"oddsportal.com", "www.oddsportal.com"}
        or len(parts) != 4
        or parts[0] != "football"
        or parts[-1] != "results"
    ):
        return None
    return url


def get_league_slug_for_season(sport: Sport, league: str, season: str | None) -> str | None:
    """
    Get the aliased URL slug for a league if it differs from the canonical one for the given season.

    Some leagues change URL slugs due to sponsor changes (e.g., Czech fortuna-liga -> chance-liga).
    This function returns the correct slug for the given season, or None if no alias applies.

    Args:
        sport: The sport enum.
        league: The canonical league key (as defined in SPORTS_LEAGUES_URLS_MAPPING).
        season: The season string (e.g., "2023-2024" or "2023" or None for current).

    Returns:
        The aliased URL slug to use, or None if no alias applies for this league/season.
    """
    if sport is Sport.FOOTBALL:
        runtime_alias = runtime_football_season_alias(
            os.environ.get("ODDSHARVESTER_RUNTIME_FOOTBALL_SEASON_ALIASES"), league, season
        )
        if runtime_alias:
            return runtime_alias

    if sport not in LEAGUE_SEASON_ALIASES or league not in LEAGUE_SEASON_ALIASES[sport]:
        return None

    if not season:
        return None

    if re.match(r"^\d{4}-\d{4}$", season):
        start_year = int(season.split("-")[0])
    elif re.match(r"^\d{4}$", season):
        start_year = int(season)
    else:
        return None

    aliases = LEAGUE_SEASON_ALIASES[sport][league]
    for max_year, alias_slug in sorted(aliases.items()):
        if start_year <= max_year:
            return alias_slug

    return None
