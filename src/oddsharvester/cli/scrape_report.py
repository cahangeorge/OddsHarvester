"""Versioned machine-readable reports for CLI scrape runs."""

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

from oddsharvester.core.scrape_result import ScrapeResult
from oddsharvester.utils.scraper_engine import ScraperEngine

REPORT_SCHEMA_VERSION = "1.1"


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
    market_coverage = _market_coverage(result, source)
    warnings = _collect_warnings(result, market_coverage)
    if exception_type:
        warnings.append(f"Scrape execution raised {exception_type}.")

    status = _status(result, exception_type, market_coverage)
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "command": command,
        "status": status,
        "outcome": _outcome(result, status),
        "engines": {
            "requested": requested_engine,
            "used": _used_engines(result, requested_engine),
            "attempts": (result.metadata.get("engine_attempts", []) if result else []),
            "cache": (result.metadata.get("cache", {}) if result else {}),
            "repair": (
                result.metadata.get("repair", {"status": "repair_skipped", "reason": "not_requested"})
                if result
                else {"status": "repair_skipped", "reason": "not_requested"}
            ),
        },
        "cleanup": _cleanup(result),
        "markets": market_coverage,
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


def _status(result: ScrapeResult | None, exception_type: str | None, market_coverage: dict[str, Any]) -> str:
    if exception_type or not result:
        return "failed"
    cleanup_failed = _cleanup(result)["status"] == "failed"
    if result.is_truthful_no_fixtures():
        return "failed" if cleanup_failed else "success"
    if not result.success:
        return "failed"
    if cleanup_failed:
        return "partial"
    if market_coverage["status"] == "incomplete":
        return "partial"
    if result.failed or result.partial:
        return "partial"
    return "success"


def _outcome(result: ScrapeResult | None, status: str) -> str:
    if status == "success" and result and result.is_truthful_no_fixtures():
        return "no_fixtures"
    return status


def _cleanup(result: ScrapeResult | None) -> dict[str, str]:
    diagnostic = result.metadata.get("cleanup") if result else None
    if not isinstance(diagnostic, dict) or diagnostic.get("status") != "failed":
        return {"status": "success"}

    phase = diagnostic.get("phase")
    if phase not in {"final_cleanup", "pre_camoufox"}:
        phase = "unknown"
    error_type = diagnostic.get("error_type")
    if (
        not isinstance(error_type, str)
        or not error_type
        or len(error_type) > 80
        or not error_type.replace("_", "").isalnum()
    ):
        error_type = "UnknownCleanupError"
    return {"status": "failed", "phase": phase, "error_type": error_type}


def _market_coverage(result: ScrapeResult | None, source: dict[str, Any]) -> dict[str, Any]:
    requested = list(dict.fromkeys(str(market) for market in source.get("markets", []) if market))
    if not requested:
        return {"status": "not_requested", "requested": [], "complete_records": 0, "missing_by_market": {}}
    if result and result.is_truthful_no_fixtures():
        return {
            "status": "no_fixtures",
            "requested": requested,
            "complete_records": 0,
            "missing_by_market": {},
        }

    records = result.success if result else []
    missing_by_market = {
        market: sum(not bool(record.get(f"{market}_market")) for record in records) for market in requested
    }
    missing_by_market = {market: count for market, count in missing_by_market.items() if count}
    complete_records = sum(all(bool(record.get(f"{market}_market")) for market in requested) for record in records)
    return {
        "status": "complete" if records and not missing_by_market else "incomplete",
        "requested": requested,
        "complete_records": complete_records,
        "missing_by_market": missing_by_market,
    }


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


def _collect_warnings(result: ScrapeResult | None, market_coverage: dict[str, Any]) -> list[str]:
    if not result:
        return []
    warnings = [warning for partial in result.partial for warning in partial.warnings]
    cleanup = _cleanup(result)
    if cleanup["status"] == "failed":
        warnings.append(f"Cleanup failed during {cleanup['phase']} ({cleanup['error_type']}).")
    if market_coverage["status"] == "incomplete":
        missing = ", ".join(sorted(market_coverage["missing_by_market"])) or "all requested markets"
        warnings.append(f"Requested market coverage is incomplete: {missing}.")
    return list(dict.fromkeys(warnings))
