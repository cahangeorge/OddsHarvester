"""Discover the live OddsPortal football catalog as validation-pending candidates.

This deliberately does not mark a league available: an HTTP 200 and an empty
off-season page do not prove a league URL is scrapeable.  Send the JSON output
to Bet's catalog refresh API after a separate results-page validation pass.
"""

import argparse
import asyncio
import json
from pathlib import Path

from oddsharvester.core.playwright_manager import PlaywrightManager
from oddsharvester.utils.league_catalog import parse_football_catalog_html

CATALOG_URL = "https://www.oddsportal.com/football/"


async def discover() -> list[dict[str, str]]:
    manager = PlaywrightManager()
    await manager.initialize(headless=True)
    try:
        await manager.page.goto(CATALOG_URL, wait_until="domcontentloaded", timeout=60_000)
        await manager.page.wait_for_timeout(2_000)
        return [
            {
                "scrape_slug": item.slug,
                "country_slug": item.country_slug,
                "country_name": item.country_slug.replace("-", " ").title(),
                "league_name": item.league_slug.replace("-", " ").title(),
                "source_url": item.url,
                "status": "validation_pending",
            }
            for item in parse_football_catalog_html(await manager.page.content())
        ]
    finally:
        await manager.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = {"source": "oddsharvester-live-discovery", "complete_snapshot": True, "leagues": asyncio.run(discover())}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"Discovered {len(payload['leagues'])} football league candidates: {args.output}")


if __name__ == "__main__":
    main()
