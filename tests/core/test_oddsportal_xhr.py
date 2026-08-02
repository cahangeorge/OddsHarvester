from __future__ import annotations

import base64
import gzip
import json

from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import pytest

from oddsharvester.core.oddsportal_xhr import (
    MAX_ENCODED_PAYLOAD_BYTES,
    OddsPortalXHRDecodeError,
    OddsPortalXHRSchemaError,
    build_listing_xhr_url,
    build_market_xhr_url,
    decode_xhr_payload,
    event_data_url,
    event_id_from_match_link,
    event_payload_from_static_html,
    extract_page_bootstrap,
    listing_page_metadata,
    listing_rows,
    market_rows_from_payload,
    match_record_from_event_payload,
    parse_user_data_script,
    provider_names_from_payload,
)

PASSWORD = b"J*8sQ!p$7aD_fR2yW@gHn*3bVp#sAdLd_k"
SALT = b"5b9a8f2c3e6d1a4b7c8e9d0f1a2b3c4d"


def _encode_payload(data: dict, *, compressed: bool = True) -> str:
    raw = json.dumps(data).encode()
    if compressed:
        raw = gzip.compress(raw)
    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=SALT,
        iterations=1_000,
    ).derive(PASSWORD)
    iv = bytes(range(16))
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(raw) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    envelope = f"{base64.b64encode(ciphertext).decode()}:{iv.hex()}".encode()
    return base64.b64encode(envelope).decode()


@pytest.mark.parametrize("compressed", [False, True])
def test_decode_xhr_payload_supports_plain_and_gzip_json(compressed):
    expected = {"d": {"rows": [{"id": "match"}]}}

    assert decode_xhr_payload(_encode_payload(expected, compressed=compressed)) == expected


@pytest.mark.parametrize("payload", ["", "not-base64", b"A" * (MAX_ENCODED_PAYLOAD_BYTES + 1)])
def test_decode_xhr_payload_rejects_invalid_envelopes(payload):
    with pytest.raises(OddsPortalXHRDecodeError):
        decode_xhr_payload(payload)


def test_decode_xhr_payload_rejects_non_ascii_text():
    with pytest.raises(OddsPortalXHRDecodeError, match="not ASCII"):
        decode_xhr_payload("<html>soft block \u2013 not encrypted</html>")


def test_decode_xhr_payload_fails_closed_on_schema_drift():
    with pytest.raises(OddsPortalXHRSchemaError, match="object at 'd'"):
        decode_xhr_payload(_encode_payload({"data": []}))


def test_extract_page_bootstrap_and_user_data():
    html = """
    <next-matches :odds-request='{"url":"/ajax-sport-country-tournament-archive_/1/LeagueId/"}'>
    </next-matches>
    <script src="/ajax-user-data/t/token/"></script>
    """
    request_url, user_data_url = extract_page_bootstrap(
        html,
        page_url="https://www.oddsportal.com/football/austria/bundesliga/results/",
    )
    user_data = {"bookiehash": "Xabc123", "geo": "RO", "locale": "en"}
    encoded = json.dumps(json.dumps(user_data))

    assert request_url == (
        "https://www.oddsportal.com/ajax-sport-country-tournament-archive_/1/LeagueId/"
    )
    assert user_data_url == "https://www.oddsportal.com/ajax-user-data/t/token/"
    assert parse_user_data_script(f"window.user = JSON.parse({encoded});") == user_data


def test_extract_page_bootstrap_rejects_external_user_data_script():
    html = """
    <next-matches :odds-request='{"url":"/ajax-listing/"}'></next-matches>
    <script src="https://evil.example/ajax-user-data/token"></script>
    """

    with pytest.raises(OddsPortalXHRSchemaError, match="user-data script host"):
        extract_page_bootstrap(html, page_url="https://www.oddsportal.com/football/austria/")


def test_build_listing_xhr_url_rejects_lookalike_host():
    with pytest.raises(OddsPortalXHRSchemaError, match="not trusted"):
        build_listing_xhr_url(
            request_base_url="https://eviloddsportal.com/ajax-listing/",
            bookiehash="Xabc123",
            page=1,
            timestamp_ms=1,
        )


def test_build_listing_xhr_url_and_listing_rows():
    url = build_listing_xhr_url(
        request_base_url="https://www.oddsportal.com/ajax-listing/",
        bookiehash="Xabc123",
        page=2,
        timestamp_ms=123,
    )
    payload = {
        "d": {
            "rows": [{"url": "/football/h2h/a/b/#Abc123"}],
            "total": 195,
            "onePage": 50,
            "page": 2,
            "pagination": {"pageCount": 4},
        }
    }
    rows, page_count = listing_rows(payload)

    assert url == "https://www.oddsportal.com/ajax-listing/Xabc123/2/0/?_=123"
    assert rows == [{"url": "/football/h2h/a/b/#Abc123"}]
    assert page_count == 4
    assert listing_page_metadata(payload) == (195, 50, 2)


def test_listing_rejects_pagination_that_would_truncate_total():
    with pytest.raises(OddsPortalXHRSchemaError, match="truncate"):
        listing_rows(
            {
                "d": {
                    "rows": [{"id": 1}],
                    "total": 195,
                    "onePage": 50,
                    "pagination": {"pageCount": 1},
                }
            }
        )


def _event_payload() -> dict:
    return {
        "d": {
            "sportData": {
                "eventData": {
                    "id": "Abc123",
                    "home": "Austria",
                    "away": "Argentina",
                    "tournamentName": "Friendly",
                    "requestBasePreMatch": "/match-event/",
                    "versionId": 1,
                    "sportId": 1,
                    "xhash": "hash123",
                },
                "eventBody": {
                    "startDate": 1_800_000_000,
                    "homeResult": 2,
                    "awayResult": 1,
                    "partialresult": "1:0",
                },
            }
        }
    }


def test_event_and_market_urls_are_bound_to_trusted_event():
    match_link = "https://www.oddsportal.com/football/h2h/austria/argentina/#Abc123"

    assert event_data_url(match_link) == (
        "https://www.oddsportal.com/football/h2h/austria/argentina/?eventId=Abc123"
    )
    assert build_market_xhr_url(
        _event_payload(),
        market="over_under_2_5",
        geo="AT",
        locale="de-AT",
    ) == "https://www.oddsportal.com/match-event/1-1-Abc123-2-2-hash123.dat?geo=AT&lang=de-AT"
    assert match_record_from_event_payload(_event_payload(), match_link=match_link)["home_team"] == "Austria"


def test_event_url_rejects_untrusted_host():
    with pytest.raises(OddsPortalXHRSchemaError, match="not trusted"):
        event_data_url("https://eviloddsportal.com/football/h2h/a/b/#Abc123")


def test_event_id_supports_canonical_match_slug_without_fragment():
    link = "https://www.oddsportal.com/football/england/premier-league/leicester-brentford-xQ77QTN0/"

    assert event_id_from_match_link(link) == "xQ77QTN0"


def test_static_event_bootstrap_is_accepted_only_for_requested_event():
    match_link = "https://www.oddsportal.com/football/h2h/austria/argentina/#Abc123"
    data = _event_payload()["d"]["sportData"]
    data["requestBasePreMatch"] = "/match-event/"
    html = f"<div id='react-event-header' data='{json.dumps(data)}'></div>"

    assert event_payload_from_static_html(html, match_link=match_link)["d"]["sportData"]["eventData"][
        "id"
    ] == "Abc123"
    with pytest.raises(OddsPortalXHRSchemaError, match="different event"):
        event_payload_from_static_html(
            html,
            match_link="https://www.oddsportal.com/football/h2h/austria/argentina/#Other1",
        )


def test_provider_mapping_and_market_parity():
    providers = provider_names_from_payload({"d": {"providersNames": {"14": "Pinnacle", "22": "Betano"}}})
    payloads = {
        "1x2": {
            "d": {
                "oddsdata": {
                    "back": {
                        "main": {
                            "bettingTypeId": 1,
                            "scopeId": 2,
                            "odds": {"14": {"0": 2.0, "1": 3.25, "2": 4}},
                            "act": {"14": True},
                        }
                    }
                }
            }
        },
        "over_under_2_5": {
            "d": {
                "oddsdata": {
                    "back": {
                        "total": {
                            "bettingTypeId": 2,
                            "scopeId": 2,
                            "handicapValue": 2.5,
                            "odds": {"14": [1.91, 2.02]},
                            "act": {"14": True},
                        }
                    }
                }
            }
        },
        "btts": {
            "d": {
                "oddsdata": {
                    "back": {
                        "main": {
                            "bettingTypeId": 13,
                            "scopeId": 2,
                            "odds": {"14": [1.8, 2.1]},
                            "act": {"14": True},
                        }
                    }
                }
            }
        },
    }

    assert market_rows_from_payload(
        payloads["1x2"],
        market="1x2",
        provider_names=providers,
        target_bookmaker="pinnacle",
    ) == [
        {
            "bookmaker_name": "Pinnacle",
            "period": "FullTime",
            "1": "2.00",
            "X": "3.25",
            "2": "4.00",
        }
    ]
    assert market_rows_from_payload(
        payloads["over_under_2_5"], market="over_under_2_5", provider_names=providers
    )[0]["odds_under"] == "2.02"
    assert market_rows_from_payload(payloads["btts"], market="btts", provider_names=providers)[0][
        "btts_yes"
    ] == "1.80"


def test_market_rows_reject_non_full_time_scope_and_invalid_decimal_odds():
    providers = {"14": "Pinnacle"}
    for invalid_scope in (3, None):
        wrong_scope = {
            "d": {
                "oddsdata": {
                    "back": {
                        "first_half": {
                            "bettingTypeId": 1,
                            "odds": {"14": [2.0, 3.0, 4.0]},
                        }
                    }
                }
            }
        }
        if invalid_scope is not None:
            wrong_scope["d"]["oddsdata"]["back"]["first_half"]["scopeId"] = invalid_scope
        with pytest.raises(OddsPortalXHRSchemaError, match="does not contain 1x2"):
            market_rows_from_payload(wrong_scope, market="1x2", provider_names=providers)

    for invalid_odd in (float("nan"), float("inf"), 1.0, 0):
        invalid_payload = {
            "d": {
                "oddsdata": {
                    "back": {
                        "main": {
                            "bettingTypeId": 13,
                            "scopeId": 2,
                            "odds": {"14": [invalid_odd, 2.0]},
                        }
                    }
                }
            }
        }
        with pytest.raises(OddsPortalXHRSchemaError, match="invalid decimal odd"):
            market_rows_from_payload(
                invalid_payload,
                market="btts",
                provider_names=providers,
            )
