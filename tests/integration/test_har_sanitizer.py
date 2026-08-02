import json
from types import SimpleNamespace

import pytest

from tests.integration.helpers import capture
from tests.integration.helpers.capture import assert_sanitized_har, sanitize_har


def test_sanitize_har_removes_headers_cookies_query_and_body_secrets(tmp_path):
    path = tmp_path / "capture.har"
    path.write_text(
        json.dumps(
            {
                "log": {
                    "entries": [
                        {
                            "request": {
                                "url": "https://www.oddsportal.com/path?token=secret&season=2025",
                                "headers": [
                                    {"name": "Cookie", "value": "session=secret"},
                                    {"name": "X-API-Key", "value": "secret"},
                                    {"name": "Accept", "value": "application/json"},
                                ],
                                "cookies": [{"name": "session", "value": "secret"}],
                                "postData": {
                                    "mimeType": "application/json",
                                    "text": '{"league":"epl","csrf_token":"secret"}',
                                },
                            },
                            "response": {
                                "headers": [{"name": "Set-Cookie", "value": "session=secret"}],
                                "content": {
                                    "mimeType": "text/html",
                                    "text": '<meta name="csrf-token" content="secret"><h1>Safe</h1>',
                                },
                            },
                        }
                    ]
                }
            }
        )
    )

    assert sanitize_har(path) is True

    scrubbed = path.read_text()
    assert "secret" not in scrubbed
    assert "token=" not in scrubbed
    assert "session=" not in scrubbed
    assert "season=2025" in scrubbed
    assert "[REDACTED]" in scrubbed
    assert_sanitized_har(path)


def test_failed_capture_deletes_raw_har(monkeypatch, tmp_path):
    raw_paths = []
    monkeypatch.setattr(capture, "FIXTURES_DIR", tmp_path)

    def fake_run(_cmd, **kwargs):
        raw_path = kwargs["env"]["ODDSHARVESTER_HAR_RECORD"]
        raw_paths.append(raw_path)
        with open(raw_path, "w", encoding="utf-8") as handle:
            handle.write('{"log":{"entries":[{"request":{"headers":[{"name":"Cookie","value":"secret"}]}}]}}')
        return SimpleNamespace(returncode=1, stdout="", stderr="failed")

    monkeypatch.setattr(capture.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Scraper failed"):
        capture.capture_fixture(
            sport="football",
            league="test",
            match_url="https://www.oddsportal.com/football/test/a-b-123456/",
            markets=["1x2"],
            capture_har=True,
        )

    assert len(raw_paths) == 1
    assert not capture.Path(raw_paths[0]).exists()
    assert not list(tmp_path.rglob("*.har"))
