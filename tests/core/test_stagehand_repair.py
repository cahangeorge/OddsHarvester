import pytest

from oddsharvester.core.stagehand_repair import STAGEHAND_CANDIDATE_PAGE_LIMIT, StagehandRepairAdapter


@pytest.mark.asyncio
async def test_stagehand_repair_skips_without_configuration(monkeypatch):
    monkeypatch.delenv("OH_STAGEHAND_API_KEY", raising=False)
    monkeypatch.delenv("OH_STAGEHAND_MODEL", raising=False)
    outcome = await StagehandRepairAdapter().repair(
        representative_page="https://www.oddsportal.com/football/example/",
        candidate_pages=["a", "b", "c", "d"],
    )
    assert outcome.status == "repair_skipped"
    assert outcome.reason == "missing_stagehand_config"
    assert outcome.candidate_pages == STAGEHAND_CANDIDATE_PAGE_LIMIT
    assert outcome.to_dict()["persistent_activation"] is False


@pytest.mark.asyncio
async def test_stagehand_repair_observes_but_never_activates_candidate(monkeypatch):
    import sys
    from types import SimpleNamespace

    events = []

    class Sessions:
        async def start(self, *, model_name, browser):
            events.append(("start", model_name, browser["type"]))
            return SimpleNamespace(data=SimpleNamespace(session_id="session-1"))

        async def navigate(self, *, id, url):
            events.append(("navigate", id, url))

        async def extract(self, *, id, instruction, schema):
            assert id == "session-1"
            assert "candidate recipe" in instruction
            assert schema["required"] == ["listing_selector", "match_link_selector", "confidence"]
            events.append(("extract", id))
            result = {"listing_selector": ".eventRow", "match_link_selector": "a[href]", "confidence": 0.9}
            return SimpleNamespace(data=SimpleNamespace(result=result))

        async def end(self, *, id):
            events.append(("end", id))

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["server"] == "local"
            assert kwargs["local_ready_timeout_s"] == 120
            assert kwargs["timeout"] == 120
            assert kwargs["max_retries"] == 0
            self.sessions = Sessions()

        async def close(self):
            events.append(("close",))

    monkeypatch.setenv("OH_STAGEHAND_API_KEY", "test-key")
    monkeypatch.setenv("OH_STAGEHAND_MODEL", "openai/gpt-5-nano")
    monkeypatch.setitem(sys.modules, "stagehand", SimpleNamespace(AsyncStagehand=Client))
    outcome = await StagehandRepairAdapter().repair(
        representative_page="https://www.oddsportal.com/football/example/", candidate_pages=["a", "b", "c", "d"]
    )
    assert outcome.status == "repair_observed"
    assert outcome.reason == "candidate_not_activated"
    assert outcome.recipe["listing_selector"] == ".eventRow"
    assert events == [
        ("start", "openai/gpt-5-nano", "local"),
        ("navigate", "session-1", "https://www.oddsportal.com/football/example/"),
        ("extract", "session-1"),
        ("end", "session-1"),
        ("close",),
    ]
