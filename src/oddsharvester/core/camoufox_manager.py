"""Optional Camoufox browser manager with the PlaywrightManager interface."""

from typing import Any

from oddsharvester.core.exceptions import AllProxiesExhaustedError
from oddsharvester.core.playwright_manager import PlaywrightManager


class CamoufoxUnavailableError(RuntimeError):
    """Raised only when the explicitly selected optional Camoufox engine is unavailable."""


class CamoufoxManager(PlaywrightManager):
    """Use Camoufox lazily while retaining the regular scraper lifecycle contract."""

    def __init__(self) -> None:
        super().__init__()
        self._camoufox: Any | None = None

    async def initialize(
        self,
        headless: bool,
        user_agent: str | None = None,
        locale: str | None = None,
        timezone_id: str | None = None,
        proxy_manager: Any | None = None,
    ) -> None:
        try:
            from camoufox.addons import DefaultAddons
            from camoufox.async_api import AsyncCamoufox
        except ImportError as exc:  # Optional dependency; never import during ordinary test collection.
            raise CamoufoxUnavailableError("Camoufox is not installed; install oddsharvester[camoufox]") from exc

        try:
            self.timezone_id = timezone_id
            self._proxy_manager = proxy_manager
            healthy_entries = (
                [entry for entry in proxy_manager.entries if not entry.blacklisted]
                if proxy_manager
                else []
            )
            if proxy_manager and not healthy_entries:
                raise AllProxiesExhaustedError(
                    "All proxies are blacklisted; cannot initialize Camoufox."
                )
            # Camoufox cannot use PlaywrightManager's synthetic "per-context"
            # launch proxy. In multi-proxy mode launch directly, then bind each
            # real proxy to its own browser context.
            launch_proxy = (
                proxy_manager.launch_proxy()
                if proxy_manager and not proxy_manager.is_multi_proxy()
                else None
            )
            camoufox = AsyncCamoufox(
                headless=headless,
                proxy=launch_proxy,
                exclude_addons=[DefaultAddons.UBO],
            )
            self._camoufox = camoufox
            self.browser = await camoufox.__aenter__()
            if proxy_manager and proxy_manager.is_multi_proxy():
                context_specs = [(entry.key, entry.config) for entry in healthy_entries]
            elif proxy_manager:
                context_specs = [(healthy_entries[0].key, None)]
            else:
                context_specs = [("direct", None)]
            self._default_key = context_specs[0][0]
            for index, (key, context_proxy) in enumerate(context_specs):
                self.contexts[key] = await self._create_context(
                    proxy=context_proxy,
                    user_agent=user_agent,
                    locale=locale,
                    timezone_id=timezone_id,
                    enable_har=(index == 0),
                )
            self.context = self.contexts[self._default_key]
            self.page = await self.context.new_page()
            if self.timezone_id is None:
                self.timezone_id = await self.page.evaluate("() => Intl.DateTimeFormat().resolvedOptions().timeZone")
        except Exception:
            await self.cleanup()
            raise

    async def cleanup(self) -> None:
        try:
            if self.page:
                await self.page.close()
            for context in list(self.contexts.values()):
                await context.close()
        finally:
            try:
                if self._camoufox:
                    await self._camoufox.__aexit__(None, None, None)
            finally:
                self.page = None
                self.context = None
                self.contexts = {}
                self.browser = None
                self._camoufox = None
                self._default_key = None
                self._proxy_manager = None
