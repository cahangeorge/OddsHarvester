"""Bounded, opt-in Stagehand v3 repair-only adapter."""

import asyncio
from dataclasses import dataclass
import json
import os
from typing import Any

STAGEHAND_TIMEOUT_SECONDS = 120
STAGEHAND_CANDIDATE_PAGE_LIMIT = 3
RECIPE_SCHEMA = {
    "type": "object",
    "required": ["listing_selector", "match_link_selector", "confidence"],
    "properties": {
        "listing_selector": {"type": "string"},
        "match_link_selector": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "additionalProperties": False,
}


@dataclass(frozen=True)
class RepairOutcome:
    status: str
    reason: str
    timeout_seconds: int = STAGEHAND_TIMEOUT_SECONDS
    candidate_pages: int = 0
    recipe: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "timeout_seconds": self.timeout_seconds,
            "candidate_pages": self.candidate_pages,
            "recipe": self.recipe,
            "persistent_activation": False,
        }


class StagehandRepairAdapter:
    """Run one local/BYOB repair observation; never persist or activate a recipe."""

    def configured(self) -> bool:
        return bool(os.environ.get("OH_STAGEHAND_API_KEY") and os.environ.get("OH_STAGEHAND_MODEL"))

    async def repair(self, *, representative_page: str | None, candidate_pages: list[str]) -> RepairOutcome:
        candidates = candidate_pages[:STAGEHAND_CANDIDATE_PAGE_LIMIT]
        if not self.configured():
            return RepairOutcome("repair_skipped", "missing_stagehand_config", candidate_pages=len(candidates))
        if not representative_page:
            return RepairOutcome("repair_skipped", "missing_representative_page", candidate_pages=len(candidates))
        try:
            recipe = await asyncio.wait_for(
                self._repair_async(representative_page), timeout=STAGEHAND_TIMEOUT_SECONDS
            )
        except ImportError:
            return RepairOutcome("repair_skipped", "stagehand_not_installed", candidate_pages=len(candidates))
        except TimeoutError:
            return RepairOutcome("repair_failed", "timeout", candidate_pages=len(candidates))
        except Exception as exc:
            return RepairOutcome("repair_failed", type(exc).__name__, candidate_pages=len(candidates))
        if not _valid_recipe(recipe):
            return RepairOutcome("repair_failed", "invalid_recipe", candidate_pages=len(candidates))
        return RepairOutcome(
            "repair_observed", "candidate_not_activated", candidate_pages=len(candidates), recipe=recipe
        )

    async def _repair_async(self, representative_page: str) -> dict[str, Any]:
        # Import only after explicit configuration so normal scraper use and tests
        # never require Node/browser/model assets.
        from stagehand import AsyncStagehand

        client = AsyncStagehand(
            server="local",
            model_api_key=os.environ["OH_STAGEHAND_API_KEY"],
            local_ready_timeout_s=STAGEHAND_TIMEOUT_SECONDS,
            timeout=STAGEHAND_TIMEOUT_SECONDS,
            max_retries=0,
        )
        session_id = None
        try:
            session = await client.sessions.start(
                model_name=os.environ["OH_STAGEHAND_MODEL"],
                browser={"type": "local", "launchOptions": {"headless": True}},
            )
            session_id = session.data.session_id
            await client.sessions.navigate(id=session_id, url=representative_page)
            observed = await client.sessions.extract(
                id=session_id,
                instruction="Return a JSON candidate recipe for the OddsPortal listing and match link selectors.",
                schema=RECIPE_SCHEMA,
            )
            payload = observed.data.result
            if hasattr(payload, "to_dict"):
                payload = payload.to_dict()
            return json.loads(payload) if isinstance(payload, str) else payload
        finally:
            if session_id is not None:
                await client.sessions.end(id=session_id)
            await client.close()


def _valid_recipe(recipe: Any) -> bool:
    return (
        isinstance(recipe, dict)
        and isinstance(recipe.get("listing_selector"), str)
        and isinstance(recipe.get("match_link_selector"), str)
        and isinstance(recipe.get("confidence"), (int, float))
    )
