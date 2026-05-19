from oddsharvester.core.odds_portal_selectors import OddsPortalSelectors


def test_match_details_testid_constants_exist():
    assert OddsPortalSelectors.MATCH_DETAILS_GAME_TIME_TESTID == "game-time-item"
    assert OddsPortalSelectors.MATCH_DETAILS_GAME_HOST_TESTID == "game-host"
    assert OddsPortalSelectors.MATCH_DETAILS_GAME_GUEST_TESTID == "game-guest"
    assert OddsPortalSelectors.MATCH_DETAILS_BREADCRUMBS_TESTID == "breadcrumbs-line"
    assert OddsPortalSelectors.MATCH_DETAILS_BREADCRUMB_LEAGUE_TESTID == "3"
