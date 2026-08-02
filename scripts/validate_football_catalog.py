"""Validate discovered football candidates in bounded, rate-limited batches."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from oddsharvester.core.browser.cookies import CookieDismisser
from oddsharvester.core.browser.scrolling import PageScroller
from oddsharvester.core.playwright_manager import PlaywrightManager
from oddsharvester.utils.league_validation import (
    football_historic_results_urls,
    football_results_url,
    validate_football_results_page,
)


async def validate_candidates(
    candidates: list[dict], *, timeout_ms: int, delay_seconds: float, season: str | None = None
) -> list[dict]:
    manager = PlaywrightManager()
    cookie_dismisser = CookieDismisser()
    scroller = PageScroller()
    await manager.initialize(headless=True)
    outcomes: list[dict] = []
    try:
        for candidate in candidates:
            slug = str(candidate.get("scrape_slug", ""))
            source_url = str(candidate.get("source_url", ""))
            results_urls = (
                football_historic_results_urls(source_url, season)
                if season
                else [football_results_url(source_url)]
            )
            results_urls = [url for url in results_urls if url]
            if not slug or not results_urls:
                outcomes.append(
                    {
                        "scrape_slug": slug,
                        "status": "unavailable",
                        "detail": "Invalid catalog candidate URL.",
                        "match_count": 0,
                    }
                )
                continue
            try:
                result = None
                for results_url in results_urls:
                    await manager.page.goto(results_url, wait_until="domcontentloaded", timeout=timeout_ms)
                    await cookie_dismisser.dismiss(manager.page)
                    await manager.page.wait_for_timeout(int(delay_seconds * 1000))
                    await scroller.scroll_until_loaded(
                        page=manager.page,
                        timeout=max(10, timeout_ms // 1000),
                        scroll_pause_time=1,
                        max_scroll_attempts=3,
                        content_check_selector="div[class*='eventRow']",
                    )
                    result = validate_football_results_page(
                        source_url,
                        manager.page.url,
                        await manager.page.content(),
                        season=season,
                        attempted_url=results_url,
                    )
                    if result.status in {"available", "validation_pending"}:
                        break
                assert result is not None
                outcomes.append(
                    {
                        "scrape_slug": slug,
                        "status": result.status,
                        "detail": result.detail,
                        "match_count": result.match_count,
                        "season_alias": result.season_alias,
                        "historic_url": result.historic_url,
                    }
                )
            except Exception as exc:
                outcomes.append(
                    {
                        "scrape_slug": slug,
                        "status": "validation_pending",
                        "detail": f"Validation could not complete: {type(exc).__name__}",
                        "match_count": 0,
                    }
                )
            await manager.page.wait_for_timeout(int(delay_seconds * 1000))
    finally:
        await manager.cleanup()
    return outcomes


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate discovered OddsPortal football league candidates.")
    parser.add_argument("--input", type=Path, required=True, help="JSON array of catalog candidate objects.")
    parser.add_argument("--output", type=Path, required=True, help="Where validation results JSON is written.")
    parser.add_argument("--timeout-ms", type=int, default=30_000)
    parser.add_argument("--delay-seconds", type=float, default=1.5)
    parser.add_argument("--season", default=None, help="Validate this exact historic season (YYYY or YYYY-YYYY).")
    args = parser.parse_args()

    candidates = json.loads(args.input.read_text())
    if not isinstance(candidates, list):
        raise SystemExit("--input must contain a JSON array")
    results = asyncio.run(
        validate_candidates(
            candidates,
            timeout_ms=max(1_000, args.timeout_ms),
            delay_seconds=max(0.0, args.delay_seconds),
            season=args.season,
        )
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"Validated {len(results)} football catalog candidates: {args.output}")


if __name__ == "__main__":
    main()
