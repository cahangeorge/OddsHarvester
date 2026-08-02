#!/usr/bin/env python3
"""Run the optional Stagehand repair assistant outside the scrape hot path."""

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from oddsharvester.core.stagehand_repair import StagehandRepairAdapter


def _trusted_page(value: str) -> str:
    parsed = urlsplit(value)
    hostname = parsed.hostname.lower() if parsed.hostname else None
    if (
        parsed.scheme == "https"
        and hostname
        and (hostname == "oddsportal.com" or hostname.endswith(".oddsportal.com"))
        and parsed.username is None
        and parsed.password is None
        and parsed.port in {None, 443}
        and not parsed.query
        and not parsed.fragment
    ):
        return value
    raise argparse.ArgumentTypeError("page must be a credential-free HTTPS OddsPortal URL")


async def _run(page: str, output: Path) -> int:
    outcome = await StagehandRepairAdapter().repair(representative_page=page, candidate_pages=[page])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(outcome.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if outcome.status == "repair_observed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page", required=True, type=_trusted_page)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    return asyncio.run(_run(args.page, args.output))


if __name__ == "__main__":
    raise SystemExit(main())
