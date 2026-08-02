#!/usr/bin/env python3
"""
Script to capture new fixtures from live scraping.

Usage:
    python -m tests.integration.helpers.capture \\
        --sport football \\
        --league premier-league \\
        --match-url "https://www.oddsportal.com/football/england/premier-league/leicester-brentford-xQ77QTN0" \\
        --markets "1x2" \\
        --period "full_time" \\
        --bookies-filter "all"

This will:
1. Run the scraper against the specified match
2. Save the output as a new fixture
3. Generate metadata.json
"""

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

# Project paths
SCRIPT_DIR = Path(__file__).parent
INTEGRATION_DIR = SCRIPT_DIR.parent
FIXTURES_DIR = INTEGRATION_DIR / "fixtures"
PROJECT_ROOT = INTEGRATION_DIR.parent.parent


def get_version() -> str:
    """Get OddsHarvester version."""
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from oddsharvester import __version__

        return __version__
    except ImportError:
        return "unknown"


def extract_match_id_from_url(url: str) -> str:
    """Extract match ID from OddsPortal URL."""
    path = urlparse(url).path.rstrip("/")
    last_segment = path.split("/")[-1]

    # Match ID is the last part after the last hyphen
    parts = last_segment.rsplit("-", 1)
    if len(parts) == 2 and len(parts[1]) >= 6:
        return parts[1]

    return last_segment


def build_fixture_filename(
    markets: list[str],
    period: str,
    bookies_filter: str,
) -> str:
    """Build fixture filename from parameters."""
    markets_str = "_".join(sorted(markets))
    return f"{markets_str}_{period}_{bookies_filter}.json"


def _alias_fragmented_redirect_targets(har_path: Path) -> None:
    """Add aliased entries for fragmented redirect targets so HAR replay can resolve them.

    OddsPortal H2H pages use URL fragments (`#match_id`) to select which match in the
    H2H series to display. Match URLs 301-redirect to `/h2h/<teams>/#match_id`. Playwright's
    `route_from_har` with `not_found="abort"` looks up the fragmented redirect target
    against HAR entries verbatim; since HAR records the bare URL (HTTP fragments never
    reach the wire), the fragmented lookup fails and the navigation aborts. We can't
    simply strip the fragment from the Location header — JS reads `location.hash` to
    pick the right match, so dropping it shows the wrong match.

    The fix: for each Location header with a fragment, duplicate the bare-URL entry as
    an alias at the fragmented URL. The redirect chain now resolves, and the browser's
    `location.hash` is preserved (Playwright sets it from the redirect target), so JS
    renders the intended match.
    """
    har = json.loads(har_path.read_text())
    entries = har.get("log", {}).get("entries", [])
    url_to_entry = {entry["request"]["url"]: entry for entry in entries if "request" in entry}

    fragmented_targets: set[str] = set()
    for entry in entries:
        for header in entry.get("response", {}).get("headers", []):
            if header.get("name", "").lower() == "location":
                value = header.get("value", "")
                if "#" in value:
                    fragmented_targets.add(value)

    new_entries = []
    for fragmented_url in fragmented_targets:
        bare_url = fragmented_url.split("#", 1)[0]
        if bare_url in url_to_entry and fragmented_url not in url_to_entry:
            alias = json.loads(json.dumps(url_to_entry[bare_url]))
            alias["request"]["url"] = fragmented_url
            new_entries.append(alias)

    if new_entries:
        entries.extend(new_entries)
        har_path.write_text(json.dumps(har))


def capture_fixture(
    sport: str,
    league: str,
    match_url: str,
    markets: list[str],
    period: str = "full_time",
    bookies_filter: str = "all",
    output_format: str = "json",
    headless: bool = True,
    timeout: int = 300,
    season: str = "current",
    capture_har: bool = False,
) -> Path:
    """
    Capture a new fixture from live scraping.

    Returns the path to the created fixture file.
    """
    match_id = extract_match_id_from_url(match_url)

    # Determine match directory name (use last URL segment)
    url_path = urlparse(match_url).path.rstrip("/")
    match_dir_name = url_path.split("/")[-1]

    # Create output directory
    output_dir = FIXTURES_DIR / sport / league / match_dir_name
    output_dir.mkdir(parents=True, exist_ok=True)

    # Build command
    markets_str = ",".join(markets)
    fixture_filename = build_fixture_filename(markets, period, bookies_filter)
    output_path = output_dir / fixture_filename

    cmd = [
        "uv",
        "run",
        "oddsharvester",
        "historic",
        "--sport",
        sport,
        "--match-link",
        match_url,
        "--market",
        markets_str,
        "--format",
        output_format,
        "--bookies-filter",
        bookies_filter,
        "--season",
        season,
        "--output",
        str(output_path.with_suffix("")),  # Extension added automatically
    ]

    if period:
        cmd.extend(["--period", period])

    if headless:
        cmd.append("--headless")

    har_path = output_path.with_suffix(".har")

    # Run scraper
    print(f"Running scraper for {match_url}...")
    print(f"Command: {' '.join(cmd)}")
    print()

    env = os.environ.copy()
    raw_har_dir: Path | None = None
    raw_har_path: Path | None = None
    if capture_har:
        raw_har_dir = Path(tempfile.mkdtemp(prefix="oddsharvester-har-"))
        raw_har_path = raw_har_dir / "capture.har"
        env["ODDSHARVESTER_HAR_RECORD"] = str(raw_har_path)
        print(f"Recording HAR to: {har_path}")

    try:
        result = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=timeout, env=env
        )

        if result.returncode != 0:
            print(f"Scraper failed with exit code {result.returncode}")
            print(f"STDOUT:\n{result.stdout}")
            print(f"STDERR:\n{result.stderr}")
            raise RuntimeError("Scraper failed")

        print("Scraper succeeded!")
        if result.stdout:
            print(f"Output: {result.stdout.strip()}")

        if not output_path.exists():
            raise RuntimeError(f"Output file not created: {output_path}")

        if capture_har:
            if raw_har_path is None or not raw_har_path.exists():
                raise RuntimeError("HAR file not created")
            _alias_fragmented_redirect_targets(raw_har_path)
            sanitize_har(raw_har_path)
            assert_sanitized_har(raw_har_path)
            raw_har_path.replace(har_path)
    finally:
        if raw_har_path is not None:
            raw_har_path.unlink(missing_ok=True)
        if raw_har_dir is not None:
            raw_har_dir.rmdir()

    # Load scraped data to extract metadata
    with open(output_path) as f:
        scraped_data = json.load(f)

    match_data = scraped_data[0] if isinstance(scraped_data, list) and scraped_data else scraped_data

    # Update or create metadata.json
    metadata_path = output_dir / "metadata.json"
    if metadata_path.exists():
        with open(metadata_path) as f:
            metadata = json.load(f)
    else:
        metadata = {
            "match_id": match_id,
            "match_url": match_url,
            "sport": sport,
            "league": league,
            "home_team": match_data.get("home_team", ""),
            "away_team": match_data.get("away_team", ""),
            "final_score": {
                "home": match_data.get("home_score", ""),
                "away": match_data.get("away_score", ""),
            },
            "match_date": match_data.get("match_date", ""),
            "notes": "",
        }

    # Update metadata with this fixture
    metadata["captured_at"] = datetime.now(UTC).isoformat()
    metadata["oddsharvester_version"] = get_version()

    if "available_fixtures" not in metadata:
        metadata["available_fixtures"] = []

    if fixture_filename not in metadata["available_fixtures"]:
        metadata["available_fixtures"].append(fixture_filename)
        metadata["available_fixtures"].sort()

    with open(metadata_path, "w") as f:
        json.dump(metadata, f, indent=4)

    print()
    print(f"Fixture created: {output_path}")
    print(f"Metadata updated: {metadata_path}")

    return output_path


def main():
    parser = argparse.ArgumentParser(
        description="Capture new fixtures for integration testing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Football - basic 1x2 market
  python -m tests.integration.helpers.capture \\
      --sport football \\
      --league premier-league \\
      --match-url "https://www.oddsportal.com/football/england/premier-league/leicester-brentford-xQ77QTN0" \\
      --markets "1x2"

  # Basketball - with period
  python -m tests.integration.helpers.capture \\
      --sport basketball \\
      --league nba \\
      --match-url "https://www.oddsportal.com/basketball/usa/nba/los-angeles-lakers-boston-celtics-0fwUQJEk/" \\
      --markets "home_away" \\
      --period "1st_half"

  # Tennis - multiple markets
  python -m tests.integration.helpers.capture \\
      --sport tennis \\
      --league australian-open \\
      --match-url "https://www.oddsportal.com/tennis/australia/atp-australian-open-2024/..." \\
      --markets "match_winner,over_under_sets_2_5"
        """,
    )

    parser.add_argument("--sport", required=True, help="Sport (e.g., football, basketball, tennis)")
    parser.add_argument(
        "--league", required=True, help="League slug for fixture organization (e.g., premier-league, nba)"
    )
    parser.add_argument("--match-url", required=True, help="Full OddsPortal match URL")
    parser.add_argument("--markets", required=True, help="Comma-separated markets (e.g., 1x2,btts)")
    parser.add_argument("--period", default="full_time", help="Period (default: full_time)")
    parser.add_argument("--bookies-filter", default="all", choices=["all", "classic", "crypto"], help="Bookies filter")
    parser.add_argument("--no-headless", action="store_true", help="Run browser with GUI (for debugging)")
    parser.add_argument("--timeout", type=int, default=300, help="Timeout in seconds (default: 300)")
    parser.add_argument("--season", default="current", help="Season (default: current, e.g., 2024-2025)")
    parser.add_argument(
        "--capture-har",
        action="store_true",
        help="Record a HAR file (snapshot.har) alongside the JSON fixture.",
    )

    args = parser.parse_args()

    markets = [m.strip() for m in args.markets.split(",")]

    try:
        capture_fixture(
            sport=args.sport,
            league=args.league,
            match_url=args.match_url,
            markets=markets,
            period=args.period,
            bookies_filter=args.bookies_filter,
            headless=not args.no_headless,
            timeout=args.timeout,
            season=args.season,
            capture_har=args.capture_har,
        )
        print()
        print("Done!")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


def sanitize_har(har_path: Path) -> bool:
    """Remove credential-bearing headers and token-like HAR fields before persistence."""
    har = json.loads(har_path.read_text())
    changed = False
    sensitive_names = {
        "authorization",
        "cookie",
        "cookies",
        "password",
        "proxy-authorization",
        "set-cookie",
    }
    sensitive_markers = ("api-key", "apikey", "auth", "csrf", "jwt", "password", "secret", "session", "token")

    def is_sensitive_name(name: object) -> bool:
        normalized = str(name or "").strip().lower()
        return normalized in sensitive_names or any(marker in normalized for marker in sensitive_markers)

    def scrub_text(text: str, mime_type: str) -> str:
        nonlocal changed
        replacement = text
        if "json" in mime_type:
            try:
                payload = json.loads(text)
            except (TypeError, ValueError):
                payload = None
            if payload is not None:
                before = json.dumps(payload, sort_keys=True)
                scrub(payload)
                after = json.dumps(payload, sort_keys=True)
                if before != after:
                    replacement = json.dumps(payload)
        elif "x-www-form-urlencoded" in mime_type:
            pairs = parse_qsl(text, keep_blank_values=True)
            retained = [(key, value) for key, value in pairs if not is_sensitive_name(key)]
            if retained != pairs:
                replacement = urlencode(retained)
                changed = True

        redacted = re.sub(
            r'(?i)(name=["\'](?:csrf[-_]?token|auth[-_]?token)["\'][^>]*content=["\'])[^"\']*(["\'])',
            r"\1[REDACTED]\2",
            replacement,
        )
        if redacted != replacement:
            changed = True
        return redacted

    def scrub(value):
        nonlocal changed
        if isinstance(value, dict):
            for key in list(value):
                if is_sensitive_name(key):
                    value.pop(key)
                    changed = True
                else:
                    scrub(value[key])
            url = value.get("url")
            if isinstance(url, str):
                parsed = urlsplit(url)
                query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
                retained = [(key, item) for key, item in query_pairs if not is_sensitive_name(key)]
                if retained != query_pairs:
                    value["url"] = urlunsplit(
                        (parsed.scheme, parsed.netloc, parsed.path, urlencode(retained), parsed.fragment)
                    )
                    changed = True
            text = value.get("text")
            if isinstance(text, str):
                mime_type = str(value.get("mimeType") or value.get("mime_type") or "")
                value["text"] = scrub_text(text, mime_type)
        elif isinstance(value, list):
            retained = []
            for item in value:
                if isinstance(item, dict) and is_sensitive_name(item.get("name")):
                    changed = True
                    continue
                scrub(item)
                retained.append(item)
            value[:] = retained

    scrub(har)
    if changed:
        har_path.write_text(json.dumps(har, separators=(",", ":")) + "\n")
    return changed


def assert_sanitized_har(har_path: Path) -> None:
    """Reject a HAR if credential-bearing fields remain after sanitization."""
    har = json.loads(har_path.read_text())
    sensitive_markers = ("api-key", "apikey", "auth", "cookie", "csrf", "jwt", "password", "secret", "session", "token")

    def is_sensitive_name(value: object) -> bool:
        normalized = str(value or "").strip().lower().replace("_", "-")
        return any(marker in normalized for marker in sensitive_markers)

    for entry in har.get("log", {}).get("entries", []):
        for message in (entry.get("request", {}), entry.get("response", {})):
            if message.get("cookies"):
                raise ValueError("HAR residue scan found cookies")
            for header in message.get("headers", []):
                if is_sensitive_name(header.get("name")):
                    raise ValueError("HAR residue scan found a sensitive header")
        request_url = str(entry.get("request", {}).get("url") or "")
        for key, _value in parse_qsl(urlsplit(request_url).query, keep_blank_values=True):
            if is_sensitive_name(key):
                raise ValueError("HAR residue scan found a sensitive query parameter")


if __name__ == "__main__":
    main()
