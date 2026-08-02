from enum import Enum
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from oddsharvester.core.camoufox_manager import CamoufoxManager
from oddsharvester.utils.proxy_manager import ProxyManager


@pytest.mark.asyncio
async def test_camoufox_partial_initialization_is_cleaned_up(monkeypatch):
    browser = SimpleNamespace()
    lifecycle = []

    class FakeDefaultAddons(Enum):
        UBO = "ubo"

    class FakeCamoufox:
        def __init__(self, **kwargs):
            assert kwargs["headless"] is True
            assert kwargs["exclude_addons"] == [FakeDefaultAddons.UBO]

        async def __aenter__(self):
            lifecycle.append("enter")
            return browser

        async def __aexit__(self, *_args):
            lifecycle.append("exit")

    monkeypatch.setitem(sys.modules, "camoufox", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "camoufox.addons",
        SimpleNamespace(DefaultAddons=FakeDefaultAddons),
    )
    monkeypatch.setitem(sys.modules, "camoufox.async_api", SimpleNamespace(AsyncCamoufox=FakeCamoufox))
    manager = CamoufoxManager()
    monkeypatch.setattr(manager, "_create_context", AsyncMock(side_effect=RuntimeError("context failed")))

    with pytest.raises(RuntimeError, match="context failed"):
        await manager.initialize(headless=True)

    assert lifecycle == ["enter", "exit"]
    assert manager.browser is None
    assert manager.context is None
    assert manager.contexts == {}
    assert manager._camoufox is None


@pytest.mark.asyncio
async def test_camoufox_cleanup_is_idempotent():
    manager = CamoufoxManager()

    await manager.cleanup()
    await manager.cleanup()

    assert manager.browser is None
    assert manager.context is None
    assert manager.page is None


@pytest.mark.asyncio
async def test_camoufox_multi_proxy_contexts_reuse_pool_health(monkeypatch):
    lifecycle = []

    class FakeDefaultAddons(Enum):
        UBO = "ubo"

    class FakeCamoufox:
        def __init__(self, **kwargs):
            assert kwargs["proxy"] is None

        async def __aenter__(self):
            lifecycle.append("enter")
            return SimpleNamespace()

        async def __aexit__(self, *_args):
            lifecycle.append("exit")

    class FakeContext:
        def __init__(self, key):
            self.key = key

        async def new_page(self):
            return SimpleNamespace(context_key=self.key, close=AsyncMock())

        async def close(self):
            return None

    monkeypatch.setitem(sys.modules, "camoufox", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "camoufox.addons",
        SimpleNamespace(DefaultAddons=FakeDefaultAddons),
    )
    monkeypatch.setitem(sys.modules, "camoufox.async_api", SimpleNamespace(AsyncCamoufox=FakeCamoufox))

    proxy_manager = ProxyManager(
        proxy_urls=["http://a.example:8000", "http://b.example:8000"]
    )
    proxy_manager.blacklist_proxy(proxy_manager.entries[0].key)
    manager = CamoufoxManager()

    async def create_context(*, proxy, **_kwargs):
        return FakeContext(proxy["server"])

    monkeypatch.setattr(manager, "_create_context", create_context)
    await manager.initialize(
        headless=True,
        timezone_id="UTC",
        proxy_manager=proxy_manager,
    )

    page, proxy_key = await manager.new_rotated_page()

    assert set(manager.contexts) == {proxy_manager.entries[1].key}
    assert manager._default_key == proxy_manager.entries[1].key
    assert manager.page.context_key == proxy_manager.entries[1].key
    assert proxy_key == proxy_manager.entries[1].key
    assert page.context_key == proxy_key
    await manager.cleanup()
    assert lifecycle == ["enter", "exit"]
