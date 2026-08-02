import logging
import os

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from oddsharvester.core.browser.market_navigation import MarketTabNavigator
from oddsharvester.core.browser.scrolling import PageScroller
from oddsharvester.core.odds_portal_selectors import OddsPortalSelectors
from oddsharvester.utils.constants import DEFAULT_MARKET_TIMEOUT_MS, MARKET_SWITCH_WAIT_TIME_MS, SCROLL_PAUSE_TIME_MS


class NavigationManager:
    """Handles browser navigation for market extraction."""

    def __init__(self, tab_navigator: MarketTabNavigator, scroller: PageScroller):
        """Initialize NavigationManager."""
        self.logger = logging.getLogger(self.__class__.__name__)
        self.tab_navigator = tab_navigator
        self.scroller = scroller
        self.fast_ready_waits = os.environ.get("ODDSHARVESTER_PIPELINE_V2") == "1"

    async def navigate_to_market_tab(self, page: Page, market_tab_name: str) -> bool:
        """Navigate to a specific market tab."""
        return await self.tab_navigator.navigate_to_tab(
            page=page, market_tab_name=market_tab_name, timeout=DEFAULT_MARKET_TIMEOUT_MS
        )

    async def is_market_active(self, page: Page, market_name: str) -> bool:
        """Return whether trusted URL or active-tab state already identifies the market."""
        target_code = OddsPortalSelectors.MARKET_TAB_CODES.get(market_name)
        if target_code and OddsPortalSelectors.market_code_from_url(page.url) == target_code:
            return True
        active_tab = await page.query_selector("li.active, li[class*='active'], .active")
        if not active_tab:
            return False
        tab_text = await active_tab.text_content()
        return bool(tab_text and market_name.lower() in tab_text.lower())

    async def wait_for_market_switch(
        self,
        page: Page,
        market_name: str,
        max_attempts: int = 3,
        *,
        already_active_before_navigation: bool = False,
    ) -> bool:
        """
        Wait for the market switch to complete and verify the correct market is active.

        Args:
            page (Page): The Playwright page instance.
            market_name (str): The name of the market that should be active.
            max_attempts (int): Maximum number of verification attempts.

        Returns:
            bool: True if the market switch is confirmed, False otherwise.
        """
        self.logger.info(f"Waiting for market switch to complete for: {market_name}")

        if self.fast_ready_waits and already_active_before_navigation:
            self.logger.info(f"Market was already active before navigation: {market_name}")
            return True

        for attempt in range(max_attempts):
            try:
                # Wait for the market switch animation to complete
                await page.wait_for_timeout(MARKET_SWITCH_WAIT_TIME_MS)

                if await self.is_market_active(page, market_name):
                    self.logger.info(f"Market switch confirmed: {market_name} is active")
                    return True

            except Exception as e:
                self.logger.warning(f"Market switch verification attempt {attempt + 1} failed: {e}")

        self.logger.warning(f"Market switch verification failed after {max_attempts} attempts")
        return False

    async def select_specific_market(self, page: Page, specific_market: str, main_market: str | None = None) -> bool:
        """Select a specific submarket within the main market.

        On localized mirrors the submarket label prefix is translated, so match
        on the language-independent tail (gotchas §7).
        """
        text = OddsPortalSelectors.submarket_match_text(specific_market, main_market)
        return await self.scroller.scroll_until_visible_and_click_parent(
            page=page,
            selector=OddsPortalSelectors.SUB_MARKET_SELECTOR,
            text=text,
        )

    async def close_specific_market(self, page: Page, specific_market: str, main_market: str | None = None) -> bool:
        """Close a specific submarket after scraping."""
        self.logger.info(f"Closing sub-market: {specific_market}")
        text = OddsPortalSelectors.submarket_match_text(specific_market, main_market)
        return await self.scroller.scroll_until_visible_and_click_parent(
            page=page,
            selector=OddsPortalSelectors.SUB_MARKET_SELECTOR,
            text=text,
        )

    async def wait_for_page_load(self, page: Page, *, allow_existing_rows: bool = False) -> None:
        """Wait for page content to load."""
        if not self.fast_ready_waits or not allow_existing_rows:
            await page.wait_for_timeout(SCROLL_PAUSE_TIME_MS)
            return
        try:
            await page.wait_for_selector(
                OddsPortalSelectors.BOOKMAKER_ROW_CSS,
                state="attached",
                timeout=SCROLL_PAUSE_TIME_MS,
            )
            self.logger.info("Odds content ready via bookmaker row")
        except PlaywrightTimeoutError:
            self.logger.info("Odds readiness condition timed out; continuing after the bounded wait")
