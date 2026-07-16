import json

from oddsharvester.utils.sport_league_constants import runtime_football_league_urls


def test_runtime_mapping_accepts_only_matching_canonical_football_urls():
    payload = json.dumps(
        {
            "argentina-primera-c": "https://www.oddsportal.com/football/argentina/primera-c/",
            "wrong-slug": "https://www.oddsportal.com/football/argentina/primera-c/",
            "not-football": "https://www.oddsportal.com/tennis/argentina/primera-c/",
        }
    )

    assert runtime_football_league_urls(payload) == {
        "argentina-primera-c": "https://www.oddsportal.com/football/argentina/primera-c/"
    }
