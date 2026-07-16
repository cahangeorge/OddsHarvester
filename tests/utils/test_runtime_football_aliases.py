import json

from oddsharvester.utils.league_aliases import runtime_football_historic_url, runtime_football_season_alias


def test_runtime_football_alias_requires_an_exact_validated_season():
    raw = json.dumps({"argentina-primera-c": {"2023-2024": "primera-c-old"}})

    assert runtime_football_season_alias(raw, "argentina-primera-c", "2023-2024") == "primera-c-old"
    assert runtime_football_season_alias(raw, "argentina-primera-c", "2022-2023") is None


def test_runtime_historic_url_requires_exact_season_and_safe_results_url():
    url = "https://www.oddsportal.com/football/argentina/primera-c-2025/results/"
    raw = json.dumps({"argentina-primera-c": {"2025-2026": url}})

    assert runtime_football_historic_url(raw, "argentina-primera-c", "2025-2026") == url
    assert runtime_football_historic_url(raw, "argentina-primera-c", "2024-2025") is None
