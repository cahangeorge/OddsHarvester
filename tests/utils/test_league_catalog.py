import json
from pathlib import Path

import pytest

from oddsharvester.utils.league_catalog import (
    FootballLeagueCandidate,
    normalize_football_league_url,
    parse_football_catalog_html,
    parse_football_catalog_json,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "catalog"


def test_parse_football_catalog_html_returns_unique_direct_league_urls():
    candidates = parse_football_catalog_html((FIXTURES / "football_listing.html").read_text())

    assert candidates == [
        FootballLeagueCandidate("australia", "a-league", "https://www.oddsportal.com/football/australia/a-league/"),
        FootballLeagueCandidate("australia", "npl-nsw", "https://www.oddsportal.com/football/australia/npl-nsw/"),
    ]


def test_parse_football_catalog_json_walks_nested_listing_values():
    candidates = parse_football_catalog_json((FIXTURES / "football_listing.json").read_text())

    assert [(candidate.slug, candidate.url) for candidate in candidates] == [
        ("australia-a-league", "https://www.oddsportal.com/football/australia/a-league/"),
        ("australia-npl-victoria", "https://www.oddsportal.com/football/australia/npl-victoria/"),
        ("england-premier-league", "https://www.oddsportal.com/football/england/premier-league/"),
    ]


@pytest.mark.parametrize(
    ("url", "expected_slug"),
    [
        ("https://www.oddsportal.com/football/Australia/A-League/?page=2#overview", "australia-a-league"),
        ("https://de.oddsportal.com/football/australia/npl-nsw/", "australia-npl-nsw"),
    ],
)
def test_normalize_football_league_url_uses_path_slugs(url, expected_slug):
    candidate = normalize_football_league_url(url)

    assert candidate is not None
    assert candidate.slug == expected_slug


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/football/australia/a-league/",
        "https://notoddsportal.com/football/australia/a-league/",
        "https://www.oddsportal.com/football/australia/a-league/results/",
        "https://www.oddsportal.com/tennis/australia/australian-open/",
        "not a url",
    ],
)
def test_normalize_football_league_url_rejects_non_direct_catalog_urls(url):
    assert normalize_football_league_url(url) is None


def test_parse_football_catalog_json_returns_empty_for_invalid_json():
    assert parse_football_catalog_json("not-json") == []


def test_parse_football_catalog_json_accepts_decoded_payload():
    payload = json.loads((FIXTURES / "football_listing.json").read_text())

    assert [candidate.slug for candidate in parse_football_catalog_json(payload)] == [
        "australia-a-league",
        "australia-npl-victoria",
        "england-premier-league",
    ]
