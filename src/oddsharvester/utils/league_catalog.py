"""Pure helpers for discovering football league candidates from listing payloads.

The returned candidates are not scraper configuration.  OddsPortal can expose
localized or stale listing links, so callers must validate a candidate's
``/results/`` page before promoting it to a supported league.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import json
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

CANONICAL_ODDSPORTAL_URL = "https://www.oddsportal.com"
_FOOTBALL_PATH_SEGMENTS = 3
_STANDINGS_PATH_SEGMENTS = 4


@dataclass(frozen=True, order=True)
class FootballLeagueCandidate:
    """A football country/league URL discovered from an OddsPortal listing."""

    country_slug: str
    league_slug: str
    url: str

    @property
    def slug(self) -> str:
        """Stable platform identifier derived from the canonical URL path."""
        return f"{self.country_slug}-{self.league_slug}"


def normalize_football_league_url(url: str) -> FootballLeagueCandidate | None:
    """Return a canonical football candidate for an OddsPortal league URL.

    Direct ``/football/<country>/<league>/`` paths and the canonical standings
    listing variant ``/football/<country>/<league>/standings/`` are accepted.
    Result, match, and non-OddsPortal links are deliberately ignored rather
    than guessed from their text or query parameters.
    """
    parsed = urlparse(urljoin(f"{CANONICAL_ODDSPORTAL_URL}/", url))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    hostname = parsed.hostname.lower()
    if hostname != "oddsportal.com" and not hostname.endswith(".oddsportal.com"):
        return None

    segments = [segment.lower() for segment in parsed.path.split("/") if segment]
    is_direct_league = len(segments) == _FOOTBALL_PATH_SEGMENTS
    is_standings_league = (
        len(segments) == _STANDINGS_PATH_SEGMENTS
        and segments[-1] == "standings"
    )
    if not (is_direct_league or is_standings_league) or segments[0] != "football":
        return None

    _, country_slug, league_slug = segments[:_FOOTBALL_PATH_SEGMENTS]
    if not country_slug or not league_slug:
        return None

    return FootballLeagueCandidate(
        country_slug=country_slug,
        league_slug=league_slug,
        url=f"{CANONICAL_ODDSPORTAL_URL}/football/{country_slug}/{league_slug}/",
    )


def parse_football_catalog_html(html: str) -> list[FootballLeagueCandidate]:
    """Extract unique football league candidates from an OddsPortal HTML listing."""
    soup = BeautifulSoup(html, "html.parser")
    return _unique_candidates(anchor.get("href") for anchor in soup.find_all("a", href=True))


def parse_football_catalog_json(
    payload: str | Mapping[str, object] | Sequence[object],
) -> list[FootballLeagueCandidate]:
    """Extract unique football league candidates from a JSON listing payload.

    The listing API has changed shape over time, so this intentionally walks
    nested values and considers only string values that normalize as direct
    football league URLs.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return []

    return _unique_candidates(_iter_string_values(payload))


def _unique_candidates(urls: Iterable[str | None]) -> list[FootballLeagueCandidate]:
    candidates = {
        candidate for url in urls if isinstance(url, str) if (candidate := normalize_football_league_url(url))
    }
    return sorted(candidates)


def _iter_string_values(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for nested_value in value.values():
            yield from _iter_string_values(nested_value)
    elif isinstance(value, Sequence):
        for nested_value in value:
            yield from _iter_string_values(nested_value)
