"""Fail unless every OddsHarvester canary report describes a clean scrape."""

import argparse
import json
from pathlib import Path

EXPECTED_SCHEMA_VERSION = "1.0"


def verify_report(path: Path) -> None:
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        raise ValueError(f"{path}: unsupported report schema {report.get('schema_version')!r}")
    if report.get("status") != "success":
        raise ValueError(f"{path}: canary status is {report.get('status')!r}")

    stats = report.get("stats", {})
    if stats.get("successful", 0) < 1:
        raise ValueError(f"{path}: canary returned no successful matches")
    if stats.get("failed", 0) or stats.get("partial", 0):
        raise ValueError(f"{path}: canary reported failed or partial matches")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("reports", type=Path, nargs="+")
    args = parser.parse_args()
    for report in args.reports:
        verify_report(report)
        print(f"Healthy scrape report: {report}")


if __name__ == "__main__":
    main()
