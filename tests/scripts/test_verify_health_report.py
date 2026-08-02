"""Tests for the scheduled-health report gate."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "verify_health_report.py"
SPEC = importlib.util.spec_from_file_location("verify_health_report", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _write_report(path, *, status="success", successful=1, failed=0, partial=0):
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "status": status,
                "stats": {"successful": successful, "failed": failed, "partial": partial},
            }
        )
    )


def test_verify_report_accepts_clean_success(tmp_path):
    report = tmp_path / "report.json"
    _write_report(report)

    MODULE.verify_report(report)


@pytest.mark.parametrize(
    "values",
    [
        {"status": "failed"},
        {"successful": 0},
        {"failed": 1},
        {"partial": 1},
    ],
)
def test_verify_report_rejects_unhealthy_canary(tmp_path, values):
    report = tmp_path / "report.json"
    _write_report(report, **values)

    with pytest.raises(ValueError):
        MODULE.verify_report(report)


def test_health_workflow_does_not_mask_canary_failures():
    workflow = (Path(__file__).parents[2] / ".github" / "workflows" / "scraper_health_check.yml").read_text()

    assert "exit 0" not in workflow
    assert "timeout-minutes:" in workflow
    assert workflow.count("--report-output") == 2
    assert "verify_health_report.py" in workflow
