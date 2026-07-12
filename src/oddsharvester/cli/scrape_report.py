"""Versioned machine-readable reports for CLI scrape runs."""

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

from oddsharvester.core.scrape_result import ScrapeResult
from oddsharvester.utils.scraper_engine import ScraperEngine

REPORT_SCHEMA_VERSION = "1.0"


def write_scrape_report(
    output_path: str,
    *,
    command: str,
    result: ScrapeResult | None,
    requested_engine: str,
    source: dict[str, Any],
    locale: str | None,
    timezone: str | None,
    started_at: datetime,
    finished_at: datetime,
    exception_type: str | None = None,
) -> None:
    """Write a metadata report without changing the primary scraped-data output."""
    failures = [failure.to_dict() for failure in result.failed] if result else []
    warnings = _collect_warnings(result)
    if exception_type:
        warnings.append(f"Scrape execution raised {exception_type}.")

    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "command": command,
        "status": _status(result, exception_type),
        "engines": {
            "requested": requested_engine,
            "used": _used_engines(result, requested_engine),
        },
        "source": source,
        "locale": locale,
        "timezone": timezone,
        "stats": _stats(result),
        "failures": failures,
        "warnings": warnings,
        "timing": {
            "started_at": started_at.astimezone(UTC).isoformat(),
            "finished_at": finished_at.astimezone(UTC).isoformat(),
            "duration_seconds": round(max(0.0, (finished_at - started_at).total_seconds()), 3),
        },
    }

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _status(result: ScrapeResult | None, exception_type: str | None) -> str:
    if exception_type or not result or not result.success:
        return "failed"
    if result.failed or result.partial:
        return "partial"
    return "success"


def _used_engines(result: ScrapeResult | None, requested_engine: str) -> list[str]:
    observed = {
        engine
        for record in (result.success if result else [])
        if isinstance((engine := record.get("_scraper_engine")), str) and engine
    }
    if observed:
        return sorted(observed)
    if result and result.success:
        return [ScraperEngine.PLAYWRIGHT.value if requested_engine == ScraperEngine.AUTO.value else requested_engine]
    return []


def _stats(result: ScrapeResult | None) -> dict[str, int | float]:
    if not result:
        return {
            "total_urls": 0,
            "successful": 0,
            "failed": 0,
            "partial": 0,
            "success_rate_pct": 0.0,
        }
    return {
        "total_urls": result.stats.total_urls,
        "successful": result.stats.successful,
        "failed": result.stats.failed,
        "partial": result.stats.partial,
        "success_rate_pct": round(result.stats.success_rate, 1),
    }


def _collect_warnings(result: ScrapeResult | None) -> list[str]:
    if not result:
        return []
    warnings = [warning for partial in result.partial for warning in partial.warnings]
    return list(dict.fromkeys(warnings))
