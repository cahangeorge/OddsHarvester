import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from oddsharvester.core.exceptions import AllProxiesExhaustedError
from oddsharvester.core.playwright_manager import PlaywrightManager
from oddsharvester.utils.proxy_manager import ProxyManager


@pytest.fixture
def mock_playwright():
    """Mock async_playwright with browser/context/page chain."""
    with patch("oddsharvester.core.playwright_manager.async_playwright") as mock_ap:
        playwright = AsyncMock()
        browser = AsyncMock()
        context = AsyncMock()
        page = AsyncMock()

        mock_ap.return_value.start = AsyncMock(return_value=playwright)
        playwright.chromium.launch = AsyncMock(return_value=browser)
        browser.new_context = AsyncMock(return_value=context)
        context.new_page = AsyncMock(return_value=page)
        context.add_init_script = AsyncMock()
        context.route_from_har = AsyncMock()
        page.evaluate = AsyncMock(return_value="UTC")

        yield {"playwright": playwright, "browser": browser, "context": context, "page": page}


@pytest.mark.asyncio
async def test_route_from_har_called_when_env_var_set(mock_playwright, monkeypatch, tmp_path):
    har_path = tmp_path / "snapshot.har"
    har_path.write_text("{}")
    monkeypatch.setenv("ODDSHARVESTER_HAR_REPLAY", str(har_path))

    pm = PlaywrightManager()
    await pm.initialize(headless=True)

    mock_playwright["context"].route_from_har.assert_awaited_once_with(
        har_path,
        url="**oddsportal.com/**",
        not_found="abort",
    )


@pytest.mark.asyncio
async def test_route_from_har_not_called_when_env_var_unset(mock_playwright, monkeypatch):
    monkeypatch.delenv("ODDSHARVESTER_HAR_REPLAY", raising=False)

    pm = PlaywrightManager()
    await pm.initialize(headless=True)

    mock_playwright["context"].route_from_har.assert_not_called()


@pytest.mark.asyncio
async def test_record_har_kwargs_when_record_env_var_set(mock_playwright, monkeypatch, tmp_path):
    har_path = tmp_path / "snapshot.har"
    monkeypatch.setenv("ODDSHARVESTER_HAR_RECORD", str(har_path))

    pm = PlaywrightManager()
    await pm.initialize(headless=True)

    mock_playwright["browser"].new_context.assert_awaited_once()
    call_kwargs = mock_playwright["browser"].new_context.await_args.kwargs
    assert call_kwargs["record_har_path"] == har_path
    assert call_kwargs["record_har_mode"] == "full"
    assert call_kwargs["record_har_url_filter"] == "**oddsportal.com/**"


@pytest.mark.asyncio
async def test_record_har_kwargs_absent_when_env_var_unset(mock_playwright, monkeypatch):
    monkeypatch.delenv("ODDSHARVESTER_HAR_RECORD", raising=False)

    pm = PlaywrightManager()
    await pm.initialize(headless=True)

    call_kwargs = mock_playwright["browser"].new_context.await_args.kwargs
    assert "record_har_path" not in call_kwargs
    assert "record_har_mode" not in call_kwargs
    assert "record_har_url_filter" not in call_kwargs


@pytest.mark.asyncio
async def test_resolves_system_timezone_when_none_requested(mock_playwright):
    """With no explicit timezone, the effective browser timezone is captured."""
    mock_playwright["page"].evaluate = AsyncMock(return_value="Europe/Paris")

    pm = PlaywrightManager()
    await pm.initialize(headless=True)

    assert pm.timezone_id == "Europe/Paris"
    mock_playwright["page"].evaluate.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_timezone_is_not_overridden(mock_playwright):
    """An explicit timezone_id is kept as-is and not re-resolved from the page."""
    pm = PlaywrightManager()
    await pm.initialize(headless=True, timezone_id="Asia/Tokyo")

    assert pm.timezone_id == "Asia/Tokyo"
    mock_playwright["page"].evaluate.assert_not_called()


@pytest.mark.asyncio
async def test_timezone_resolution_failure_falls_back_to_utc(mock_playwright):
    """If the timezone probe raises, fall back to UTC rather than crash."""
    mock_playwright["page"].evaluate = AsyncMock(side_effect=RuntimeError("probe failed"))

    pm = PlaywrightManager()
    await pm.initialize(headless=True)

    assert pm.timezone_id == "UTC"


@pytest.mark.asyncio
async def test_single_context_when_no_proxy_manager(mock_playwright):
    pm = PlaywrightManager()
    await pm.initialize(headless=True)
    mock_playwright["browser"].new_context.assert_awaited_once()
    assert list(pm.contexts.keys()) == ["direct"]
    assert pm.non_default_context_keys() == []


@pytest.mark.asyncio
async def test_one_context_per_proxy_when_multi(mock_playwright):
    proxy_manager = ProxyManager(proxy_urls=["http://a.example.com:1", "http://b.example.com:2"])
    pm = PlaywrightManager()
    await pm.initialize(headless=True, proxy_manager=proxy_manager)
    # Two contexts created; browser launched with the per-context sentinel.
    assert mock_playwright["browser"].new_context.await_count == 2
    assert set(pm.contexts.keys()) == {"http://a.example.com:1", "http://b.example.com:2"}
    launch_kwargs = mock_playwright["playwright"].chromium.launch.await_args.kwargs
    assert launch_kwargs["proxy"] == {"server": "per-context"}
    assert len(pm.non_default_context_keys()) == 1


@pytest.mark.asyncio
async def test_initial_context_skips_preblacklisted_proxy(mock_playwright):
    proxy_manager = ProxyManager(proxy_urls=["http://a.example.com:1", "http://b.example.com:2"])
    proxy_manager.blacklist_proxy(proxy_manager.entries[0].key)
    pm = PlaywrightManager()

    await pm.initialize(headless=True, proxy_manager=proxy_manager)

    assert list(pm.contexts) == [proxy_manager.entries[1].key]
    assert pm._default_key == proxy_manager.entries[1].key
    assert pm.context is pm.contexts[proxy_manager.entries[1].key]


@pytest.mark.asyncio
async def test_initialization_fails_when_all_proxies_are_preblacklisted(mock_playwright):
    proxy_manager = ProxyManager(proxy_urls=["http://a.example.com:1", "http://b.example.com:2"])
    for entry in proxy_manager.entries:
        proxy_manager.blacklist_proxy(entry.key)
    pm = PlaywrightManager()

    with pytest.raises(AllProxiesExhaustedError, match="initialize Playwright"):
        await pm.initialize(headless=True, proxy_manager=proxy_manager)

    mock_playwright["playwright"].chromium.launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_rotated_page_reports_key(mock_playwright):
    proxy_manager = ProxyManager(proxy_urls=["http://a.example.com:1", "http://b.example.com:2"])
    pm = PlaywrightManager()
    await pm.initialize(headless=True, proxy_manager=proxy_manager)
    _page, key = await pm.new_rotated_page()
    assert key in {"http://a.example.com:1", "http://b.example.com:2"}


@pytest.mark.asyncio
async def test_new_rotated_page_raises_when_exhausted(mock_playwright):
    proxy_manager = ProxyManager(proxy_urls=["http://a.example.com:1", "http://b.example.com:2"])
    pm = PlaywrightManager()
    await pm.initialize(headless=True, proxy_manager=proxy_manager)
    for key in ["http://a.example.com:1", "http://b.example.com:2"]:
        for _ in range(3):
            pm.report_page_result(key, is_proxy_failure=True)
    with pytest.raises(AllProxiesExhaustedError):
        await pm.new_rotated_page()


@pytest.mark.asyncio
async def test_resource_blocking_is_enabled_only_outside_har_replay(mock_playwright, monkeypatch):
    monkeypatch.delenv("ODDSHARVESTER_HAR_REPLAY", raising=False)
    manager = PlaywrightManager()
    await manager.initialize(headless=True)
    mock_playwright["context"].route.assert_awaited_once()


@pytest.mark.asyncio
async def test_resource_blocking_preserves_har_replay(mock_playwright, monkeypatch, tmp_path):
    har_path = tmp_path / "snapshot.har"
    har_path.write_text("{}")
    monkeypatch.setenv("ODDSHARVESTER_HAR_REPLAY", str(har_path))
    manager = PlaywrightManager()
    await manager.initialize(headless=True)
    mock_playwright["context"].route.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_attempts_all_resources_and_repeated_call_does_not_close_twice(mock_playwright):
    manager = PlaywrightManager()
    await manager.initialize(headless=True)
    first_error = RuntimeError("page close failed")
    mock_playwright["page"].close.side_effect = first_error
    mock_playwright["context"].close.side_effect = RuntimeError("context close failed")

    with pytest.raises(RuntimeError) as exc_info:
        await manager.cleanup()
    with pytest.raises(RuntimeError) as repeated_exc_info:
        await manager.cleanup()

    assert exc_info.value is first_error
    assert repeated_exc_info.value is first_error
    mock_playwright["page"].close.assert_awaited_once()
    mock_playwright["context"].close.assert_awaited_once()
    mock_playwright["browser"].close.assert_awaited_once()
    mock_playwright["playwright"].stop.assert_awaited_once()
    assert manager.page is None
    assert manager.context is None
    assert manager.contexts == {}
    assert manager.browser is None
    assert manager.playwright is None
    assert manager.timezone_id is None
    assert manager._default_key is None
    assert manager._proxy_manager is None


@pytest.mark.asyncio
async def test_concurrent_cleanup_callers_share_error_and_close_resources_once(mock_playwright):
    manager = PlaywrightManager()
    await manager.initialize(headless=True)
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    first_error = RuntimeError("page close failed")

    async def fail_page_close():
        close_started.set()
        await allow_close.wait()
        raise first_error

    mock_playwright["page"].close.side_effect = fail_page_close
    first_waiter = asyncio.create_task(manager.cleanup())
    await close_started.wait()
    second_waiter = asyncio.create_task(manager.cleanup())
    allow_close.set()

    results = await asyncio.gather(first_waiter, second_waiter, return_exceptions=True)

    assert results[0] is first_error
    assert results[1] is first_error
    mock_playwright["page"].close.assert_awaited_once()
    mock_playwright["context"].close.assert_awaited_once()
    mock_playwright["browser"].close.assert_awaited_once()
    mock_playwright["playwright"].stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_initialize_waits_for_running_cleanup_before_new_lifecycle(mock_playwright):
    manager = PlaywrightManager()
    await manager.initialize(headless=True)
    close_started = asyncio.Event()
    allow_close = asyncio.Event()

    async def delayed_page_close():
        close_started.set()
        await allow_close.wait()

    mock_playwright["page"].close.side_effect = delayed_page_close
    cleanup_waiter = asyncio.create_task(manager.cleanup())
    await close_started.wait()
    next_initialize = asyncio.create_task(manager.initialize(headless=True))
    await asyncio.sleep(0)

    mock_playwright["playwright"].chromium.launch.assert_awaited_once()
    allow_close.set()
    await cleanup_waiter
    await next_initialize

    assert mock_playwright["playwright"].chromium.launch.await_count == 2
    await manager.cleanup()


@pytest.mark.asyncio
async def test_concurrent_reinitializes_after_cleanup_allocate_one_lifecycle(mock_playwright):
    manager = PlaywrightManager()
    await manager.initialize(headless=True)
    close_started = asyncio.Event()
    allow_close = asyncio.Event()

    async def delayed_page_close():
        close_started.set()
        await allow_close.wait()

    mock_playwright["page"].close.side_effect = delayed_page_close
    cleanup_waiter = asyncio.create_task(manager.cleanup())
    await close_started.wait()

    reinitialize_started = asyncio.Event()
    allow_reinitialize = asyncio.Event()

    async def delayed_browser_launch(**_kwargs):
        reinitialize_started.set()
        await allow_reinitialize.wait()
        return mock_playwright["browser"]

    mock_playwright["playwright"].chromium.launch.side_effect = delayed_browser_launch
    first_reinitialize = asyncio.create_task(manager.initialize(headless=True))
    await asyncio.sleep(0)
    second_reinitialize = asyncio.create_task(manager.initialize(headless=True))
    await asyncio.sleep(0)

    assert not first_reinitialize.done()
    assert not second_reinitialize.done()
    mock_playwright["playwright"].chromium.launch.assert_awaited_once()

    allow_close.set()
    await cleanup_waiter
    await reinitialize_started.wait()

    with pytest.raises(RuntimeError, match="initialization is already in progress"):
        await second_reinitialize
    assert mock_playwright["playwright"].chromium.launch.await_count == 2

    allow_reinitialize.set()
    await first_reinitialize
    await manager.cleanup()


@pytest.mark.asyncio
async def test_cancelled_cleanup_waiter_does_not_cancel_shared_cleanup(mock_playwright):
    manager = PlaywrightManager()
    await manager.initialize(headless=True)
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    cleanup_error = RuntimeError("page close failed")

    async def fail_page_close():
        close_started.set()
        await allow_close.wait()
        raise cleanup_error

    mock_playwright["page"].close.side_effect = fail_page_close
    cancelled_waiter = asyncio.create_task(manager.cleanup())
    await close_started.wait()
    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter

    allow_close.set()
    with pytest.raises(RuntimeError) as later_exc_info:
        await manager.cleanup()

    assert later_exc_info.value is cleanup_error
    mock_playwright["page"].close.assert_awaited_once()
    mock_playwright["context"].close.assert_awaited_once()
    mock_playwright["browser"].close.assert_awaited_once()
    mock_playwright["playwright"].stop.assert_awaited_once()
