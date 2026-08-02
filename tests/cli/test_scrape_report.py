"""Tests for the optional CLI scrape report."""

from datetime import UTC, datetime, timedelta
import json
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner
import pytest

from oddsharvester.cli.cli import cli
from oddsharvester.cli.scrape_report import write_scrape_report
from oddsharvester.core.scrape_result import ErrorType, FailedUrl, PartialResult, ScrapeResult, ScrapeStats


def _result() -> ScrapeResult:
    return ScrapeResult(
        success=[{"match": "A v B", "_scraper_engine": "scrapling-http"}],
        failed=[FailedUrl("https://example.test/failed", ErrorType.NAVIGATION, "timed out", attempts=2)],
        partial=[PartialResult("https://example.test/partial", {"match": "C v D"}, warnings=["Market btts missing"])],
        stats=ScrapeStats(total_urls=3, successful=1, failed=1, partial=1),
    )


def test_write_scrape_report_has_stable_versioned_shape(tmp_path):
    started_at = datetime(2026, 7, 12, 10, 0, tzinfo=UTC)
    output = tmp_path / "nested" / "report.json"

    write_scrape_report(
        str(output),
        command="upcoming",
        result=_result(),
        requested_engine="auto",
        source={"sport": "football"},
        locale="en-GB",
        timezone="Europe/London",
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=1.2345),
    )

    report = json.loads(output.read_text())
    assert list(report) == [
        "schema_version",
        "command",
        "status",
        "outcome",
        "engines",
        "source",
        "locale",
        "timezone",
        "stats",
        "failures",
        "warnings",
        "timing",
    ]
    assert report["schema_version"] == "1.1"
    assert report["status"] == "partial"
    assert report["outcome"] == "partial"
    assert report["engines"] == {
        "requested": "auto",
        "used": ["scrapling-http"],
        "attempts": [],
        "cache": {},
        "repair": {"status": "repair_skipped", "reason": "not_requested"},
    }
    assert report["stats"] == {
        "total_urls": 3,
        "successful": 1,
        "failed": 1,
        "partial": 1,
        "success_rate_pct": 33.3,
    }
    assert report["failures"][0]["error_type"] == "navigation"
    assert report["warnings"] == ["Market btts missing"]
    assert report["timing"]["duration_seconds"] == 1.234


def test_v11_no_fixtures_requires_explicit_discovery_attestation(tmp_path):
    started_at = datetime(2026, 7, 12, 10, 0, tzinfo=UTC)
    output = tmp_path / "report.json"
    result = ScrapeResult(
        stats=ScrapeStats(total_urls=0),
        metadata={"discovery_outcome": "no_fixtures"},
    )

    write_scrape_report(
        str(output),
        command="upcoming",
        result=result,
        requested_engine="auto",
        source={"sport": "football"},
        locale=None,
        timezone=None,
        started_at=started_at,
        finished_at=started_at,
    )

    report = json.loads(output.read_text())
    assert report["status"] == "success"
    assert report["outcome"] == "no_fixtures"


def test_v11_zero_urls_without_attestation_is_failed(tmp_path):
    started_at = datetime(2026, 7, 12, 10, 0, tzinfo=UTC)
    output = tmp_path / "report.json"

    write_scrape_report(
        str(output),
        command="upcoming",
        result=ScrapeResult(stats=ScrapeStats(total_urls=0)),
        requested_engine="auto",
        source={"sport": "football"},
        locale=None,
        timezone=None,
        started_at=started_at,
        finished_at=started_at,
    )

    report = json.loads(output.read_text())
    assert report["status"] == "failed"
    assert report["outcome"] == "failed"


@pytest.mark.parametrize(
    ("command", "args", "patch_target"),
    [
        (
            "upcoming",
            ["upcoming", "-s", "football", "-l", "england-premier-league"],
            "oddsharvester.cli.commands.upcoming.run_scraper",
        ),
        (
            "historic",
            ["historic", "-s", "football", "-l", "england-premier-league", "--season", "current"],
            "oddsharvester.cli.commands.historic.run_scraper",
        ),
    ],
)
def test_cli_report_is_additive_and_primary_output_stays_a_list(tmp_path, command, args, patch_target):
    primary_output = tmp_path / "matches"
    report_output = tmp_path / "report.json"
    result_data = _result()

    with (
        patch(patch_target, new_callable=AsyncMock, return_value=result_data),
        patch("oddsharvester.cli.commands.upcoming.store_data") as upcoming_store,
        patch("oddsharvester.cli.commands.historic.store_data") as historic_store,
    ):
        invocation = CliRunner().invoke(
            cli,
            [*args, "--output", str(primary_output), "--report-output", str(report_output), "--engine", "auto"],
        )

    assert invocation.exit_code == 0, invocation.output
    store = upcoming_store if command == "upcoming" else historic_store
    assert store.call_args.kwargs["data"] == result_data.success
    assert isinstance(store.call_args.kwargs["data"], list)
    report = json.loads(report_output.read_text())
    assert report["command"] == command
    assert report["source"]["sport"] == "football"
    assert report["engines"] == {
        "requested": "auto",
        "used": ["scrapling-http"],
        "attempts": [],
        "cache": {},
        "repair": {"status": "repair_skipped", "reason": "not_requested"},
    }


def test_cli_writes_failed_report_when_scraper_raises(tmp_path):
    report_output = tmp_path / "failure.json"

    with patch(
        "oddsharvester.cli.commands.upcoming.run_scraper",
        new_callable=AsyncMock,
        side_effect=RuntimeError("proxy-user:proxy-pass"),
    ):
        invocation = CliRunner().invoke(
            cli,
            [
                "upcoming",
                "-s",
                "football",
                "-l",
                "england-premier-league",
                "--report-output",
                str(report_output),
            ],
        )

    assert invocation.exit_code == 1
    report = json.loads(report_output.read_text())
    assert report["status"] == "failed"
    assert report["warnings"] == ["Scrape execution raised RuntimeError."]
    assert "proxy-pass" not in report_output.read_text()
