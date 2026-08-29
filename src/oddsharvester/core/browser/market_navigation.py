"""See module docstring in core/browser/__init__.py."""

import logging

from playwright.async_api import Page

from oddsharvester.core.odds_portal_selectors import OddsPortalSelectors
from oddsharvester.utils.constants import (
    DEFAULT_MARKET_TIMEOUT_MS,
    DROPDOWN_WAIT_MS,
    MARKET_TAB_TIMEOUT_MS,
    TAB_SWITCH_WAIT_MS,
)


class MarketTabNavigator:
    """Navigate to a market tab on the odds page, with fallback to the 'More' dropdown."""

    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)

    async def navigate_to_tab(self, page: Page, market_tab_name: str, timeout: int = MARKET_TAB_TIMEOUT_MS) -> bool:
        """Navigate to a specific market tab by its name.

        First tries visible tabs, then the "More" dropdown. Verifies the tab becomes active.
        Returns True on success, False otherwise.
        """
        self.logger.info(f"Attempting to navigate to market tab: {market_tab_name}")

        market_found = False
        current_nav = await page.query_selector("div[data-testid='sports-nav']")
        selectors = (
            OddsPortalSelectors.MARKET_TAB_SELECTORS[:2]
            if current_nav
            else OddsPortalSelectors.MARKET_TAB_SELECTORS[2:]
        )
        for selector in selectors:
            if await self._wait_and_click(page=page, selector=selector, text=market_tab_name, timeout=timeout):
                market_found = True
                break

        if market_found:
            if await self._verify_tab_is_active(page, market_tab_name):
                self.logger.info(f"Successfully navigated to {market_tab_name} tab (directly visible).")
                return True
            else:
                self.logger.warning(f"Tab {market_tab_name} was clicked but is not active.")

        self.logger.info(f"Market '{market_tab_name}' not found in visible tabs. Checking 'More' dropdown...")
        if await self._click_more_if_market_hidden(page, market_tab_name, timeout):
            if await self._verify_tab_is_active(page, market_tab_name):
                self.logger.info(f"Successfully navigated to {market_tab_name} tab (via 'More' dropdown).")
                return True
            else:
                self.logger.warning(f"Tab {market_tab_name} was clicked but is not active.")

        # Localized-mirror fallback: match the URL-fragment market code (gotchas §7).
        target_code = OddsPortalSelectors.MARKET_TAB_CODES.get(market_tab_name)
        if target_code and await self._navigate_by_code(page, target_code):
            self.logger.info(f"Successfully navigated to {market_tab_name} tab (via market-code fallback).")
            return True

        self.logger.error(
            f"Failed to find or click the {market_tab_name} tab (searched visible tabs, 'More' dropdown, "
            f"and market-code fallback)."
        )
        return False

    async def _navigate_by_code(self, page: Page, target_code: str) -> bool:
        """Click each tab and match the URL-fragment market code (localized mirrors)."""
        try:
            accepted_codes = OddsPortalSelectors.accepted_market_codes(target_code)
            await self._open_more_dropdown(page)
            elements = await page.query_selector_all(OddsPortalSelectors.MARKET_TAB_ITEM_SELECTOR)
            labels: list[str] = []
            for element in elements:
                text = (await element.text_content() or "").strip()
                if text and text not in labels:
                    labels.append(text)

            if not labels:
                return False

            self.logger.info(f"Market-code fallback: scanning {len(labels)} tabs for code '{target_code}'.")
            for label in labels:
                await self._open_more_dropdown(page)
                if not await self._click_by_text(page, OddsPortalSelectors.MARKET_TAB_ITEM_SELECTOR, label):
                    continue
                await page.wait_for_timeout(TAB_SWITCH_WAIT_MS)
                observed_code = OddsPortalSelectors.market_code_from_url(page.url)
                if observed_code and observed_code.casefold() in accepted_codes:
                    self.logger.info(f"Market-code fallback matched tab '{label}' -> code '{target_code}'.")
                    return True

            self.logger.warning(f"Market-code fallback found no tab yielding code '{target_code}'.")
            return False
        except Exception as e:
            self.logger.error(f"Error during market-code fallback for '{target_code}': {e}")
            return False

    async def _open_more_dropdown(self, page: Page) -> bool:
        """Expand the current or legacy market overflow without relying on translated text."""
        try:
            for expanded_selector in OddsPortalSelectors.MORE_EXPANDED_SELECTORS:
                if await page.query_selector(expanded_selector):
                    return True
            for selector in OddsPortalSelectors.MORE_BUTTON_SELECTORS:
                more = await page.query_selector(selector)
                if not more:
                    continue
                await more.click(timeout=DEFAULT_MARKET_TIMEOUT_MS)
                await page.wait_for_timeout(DROPDOWN_WAIT_MS)
                return True
            return False
        except Exception as e:
            self.logger.debug(f"Could not open 'More' dropdown: {e}")
            return False

    async def _wait_and_click(
        self, page: Page, selector: str, text: str | None = None, timeout: float = DEFAULT_MARKET_TIMEOUT_MS
    ) -> bool:
        try:
            # Overflow tabs stay attached while hidden. Waiting for attachment
            # avoids a full timeout on the hidden active tab; _click_by_text
            # remains responsible for selecting a visible matching element.
            await page.wait_for_selector(selector=selector, timeout=timeout, state="attached")
            if text:
                return await self._click_by_text(page=page, selector=selector, text=text)
            else:
                element = await page.query_selector(selector)
                await element.click()
                return True
        except Exception as e:
            self.logger.error(f"Error waiting for or clicking selector '{selector}': {e}")
            return False

    async def _click_by_text(self, page: Page, selector: str, text: str) -> bool:
        try:
            elements = await page.query_selector_all(selector)
            for element in elements:
                element_text = await element.text_content()
                if element_text and text in element_text:
                    is_visible = getattr(element, "is_visible", None)
                    if callable(is_visible) and not await is_visible():
                        continue
                    await element.click()
                    return True
            self.logger.info(f"Element with text '{text}' not found.")
            return False
        except Exception as e:
            self.logger.error(f"Error clicking element with text '{text}': {e}")
            return False

    async def _click_more_if_market_hidden(
        self, page: Page, market_tab_name: str, timeout: int = MARKET_TAB_TIMEOUT_MS
    ) -> bool:
        del timeout
        try:
            if not market_tab_name or not await self._open_more_dropdown(page):
                self.logger.warning("Could not find or click 'More' button")
                return False

            if await self._click_by_text(page, OddsPortalSelectors.MARKET_TAB_ITEM_SELECTOR, market_tab_name):
                return True

            dropdown_selectors = OddsPortalSelectors.get_dropdown_selectors_for_market(market_tab_name)
            for selector in dropdown_selectors:
                try:
                    dropdown_element = await page.query_selector(selector)
                    if dropdown_element:
                        text = await dropdown_element.text_content()
                        if text and market_tab_name.lower() in text.lower():
                            self.logger.info(f"Found '{market_tab_name}' in dropdown. Clicking...")
                            await dropdown_element.click()
                            return True
                except Exception as e:
                    self.logger.debug(
                        f"Exception while searching for market '{market_tab_name}' in dropdown with selector "
                        f"'{selector}': {e}"
                    )
                    continue

            self.logger.info("Debugging dropdown content:")
            dropdown_items = await page.query_selector_all(OddsPortalSelectors.DROPDOWN_DEBUG_ELEMENTS)
            for item in dropdown_items[:10]:
                try:
                    text = await item.text_content()
                    if text and text.strip():
                        self.logger.info(f"  Dropdown item: '{text.strip()}'")
                except Exception as e:
                    self.logger.debug(f"Exception while logging dropdown item: {e}")
                    continue

            return False

        except Exception as e:
            self.logger.error(f"Error in _click_more_if_market_hidden: {e}")
            return False

    async def _verify_tab_is_active(self, page: Page, market_tab_name: str) -> bool:
        try:
            if not market_tab_name:
                return False
            await page.wait_for_timeout(TAB_SWITCH_WAIT_MS)
            target_code = OddsPortalSelectors.MARKET_TAB_CODES.get(market_tab_name)
            if target_code and OddsPortalSelectors.market_code_matches(market_tab_name, page.url):
                return True
            active_selectors = [
                "div[data-testid='sports-nav'] button[data-testid='sports-nav-active-tab']",
                "li.odds-item.active",
                "ul.odds-tabs > li.active",
            ]

            for selector in active_selectors:
                try:
                    active_element = await page.query_selector(selector)
                    if active_element:
                        text = await active_element.text_content()
                        if text and market_tab_name.lower() in text.lower():
                            self.logger.info(f"Tab '{market_tab_name}' is confirmed active")
                            return True
                except Exception as e:
                    self.logger.debug(f"Exception checking active selector '{selector}': {e}")
                    continue

            self.logger.warning(f"Tab '{market_tab_name}' is not confirmed as active")
            return False

        except Exception as e:
            self.logger.error(f"Error verifying tab is active: {e}")
            return False
