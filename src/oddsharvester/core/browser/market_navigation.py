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
        for selector in OddsPortalSelectors.MARKET_TAB_SELECTORS:
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

        self.logger.error(
            f"Failed to find or click the {market_tab_name} tab (searched visible tabs and 'More' dropdown)."
        )
        return False

    async def _wait_and_click(
        self, page: Page, selector: str, text: str | None = None, timeout: float = DEFAULT_MARKET_TIMEOUT_MS
    ) -> bool:
        try:
            await page.wait_for_selector(selector=selector, timeout=timeout)
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
        try:
            more_clicked = False
            for selector in OddsPortalSelectors.MORE_BUTTON_SELECTORS:
                try:
                    more_element = await page.query_selector(selector)
                    if more_element:
                        text = await more_element.text_content()
                        if text and ("more" in text.lower() or "..." in text):
                            self.logger.info(f"Clicking 'More' button: '{text.strip()}'")
                            await more_element.click()
                            more_clicked = True
                            break
                except Exception as e:
                    self.logger.debug(f"Exception while searching for 'More' button with selector '{selector}': {e}")
                    continue

            if not more_clicked:
                self.logger.warning("Could not find or click 'More' button")
                return False

            await page.wait_for_timeout(DROPDOWN_WAIT_MS)

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
            await page.wait_for_timeout(TAB_SWITCH_WAIT_MS)
            active_selectors = ["li.active", "li[class*='active']", ".active", "[class*='active']"]

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

            page_content = await page.content()
            if market_tab_name and market_tab_name.lower() in page_content.lower():
                self.logger.info(f"Market '{market_tab_name}' found in page content")
                return True

            self.logger.warning(f"Tab '{market_tab_name}' is not confirmed as active")
            return False

        except Exception as e:
            self.logger.error(f"Error verifying tab is active: {e}")
            return False
