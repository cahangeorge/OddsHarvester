from oddsharvester.utils.league_validation import (
    football_historic_results_urls,
    football_results_url,
    validate_football_results_page,
)


def test_builds_results_url_only_for_direct_football_leagues():
    assert football_results_url("https://www.oddsportal.com/football/argentina/primera-c/") == (
        "https://www.oddsportal.com/football/argentina/primera-c/results/"
    )
    assert football_results_url("https://example.com/football/argentina/primera-c/") is None


def test_reachable_results_page_stays_pending_until_cli_registry_support_exists():
    result = validate_football_results_page(
        "https://www.oddsportal.com/football/argentina/primera-c/",
        "https://www.oddsportal.com/football/argentina/primera-c/results/",
        '<div class="eventRow"><a href="/football/argentina/primera-c/team-a-team-b-abc123/">Match</a></div>',
    )

    assert result.status == "available"
    assert result.match_count == 1


def test_missing_match_links_marks_candidate_unavailable():
    result = validate_football_results_page(
        "https://www.oddsportal.com/football/argentina/primera-c/",
        "https://www.oddsportal.com/football/argentina/primera-c/results/",
        "<html><body>No matches</body></html>",
    )

    assert result.status == "unavailable"


def test_historic_redirect_promotes_the_resolved_alias_for_that_exact_season():
    result = validate_football_results_page(
        "https://www.oddsportal.com/football/argentina/primera-c/",
        "https://www.oddsportal.com/football/argentina/primera-c-old-2023-2024/results/",
        '<div class="eventRow"><a href="/football/argentina/primera-c/team-a-team-b-abc123/">Match</a></div>',
        season="2023-2024",
    )

    assert result.status == "available"
    assert result.season_alias == "primera-c-old"


def test_historic_candidates_try_range_then_calendar_year_conventions():
    assert football_historic_results_urls(
        "https://www.oddsportal.com/football/argentina/primera-c/", "2025-2026", current_year=2026
    ) == [
        "https://www.oddsportal.com/football/argentina/primera-c-2025-2026/results/",
        "https://www.oddsportal.com/football/argentina/primera-c-2025/results/",
        "https://www.oddsportal.com/football/argentina/primera-c-2026/results/",
    ]


def test_current_season_tries_the_rolling_base_results_url_first():
    assert football_historic_results_urls(
        "https://www.oddsportal.com/football/argentina/primera-c/", "2026-2027", current_year=2026
    )[0] == "https://www.oddsportal.com/football/argentina/primera-c/results/"
