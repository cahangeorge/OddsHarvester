from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime
import json
import math
import re
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit
import zlib

from bs4 import BeautifulSoup
from bs4.element import Tag
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from oddsharvester.utils.constants import ODDSPORTAL_BASE_URL
from oddsharvester.utils.utils import clean_html_text

DECODER_REVISION = "app-CxDlN6Pk-2026-07-31"
MAX_ENCODED_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_DECODED_PAYLOAD_BYTES = 8 * 1024 * 1024

# These values are shipped to every browser by OddsPortal's public application
# bundle. They are versioned with DECODER_REVISION so drift fails closed and
# falls back to the browser path instead of emitting corrupt records.
_PAYLOAD_PASSWORD = b"J*8sQ!p$7aD_fR2yW@gHn*3bVp#sAdLd_k"
_PAYLOAD_SALT = b"5b9a8f2c3e6d1a4b7c8e9d0f1a2b3c4d"
_PBKDF2_ITERATIONS = 1_000

MARKET_BETTING_TYPE_IDS = {
    "1x2": 1,
    "over_under_2_5": 2,
    "btts": 13,
}
MARKET_OUTCOME_LABELS = {
    "1x2": ("1", "X", "2"),
    "over_under_2_5": ("odds_over", "odds_under"),
    "btts": ("btts_yes", "btts_no"),
}
MARKET_HANDICAPS = {"over_under_2_5": "2.50"}

_JSON_PARSE_ARGUMENT = re.compile(r"""JSON\.parse\(("(?:\\.|[^"\\])*")\)""")


class OddsPortalXHRDecodeError(RuntimeError):
    """Raised when an encrypted OddsPortal XHR payload cannot be decoded safely."""


class OddsPortalXHRSchemaError(RuntimeError):
    """Raised when a decoded OddsPortal payload no longer matches the expected schema."""


def decode_xhr_payload(payload: str | bytes) -> dict[str, Any]:
    try:
        encoded = payload.encode("ascii") if isinstance(payload, str) else payload
    except UnicodeEncodeError as exc:
        raise OddsPortalXHRDecodeError("OddsPortal XHR payload is not ASCII") from exc
    if not encoded or len(encoded) > MAX_ENCODED_PAYLOAD_BYTES:
        raise OddsPortalXHRDecodeError("OddsPortal XHR payload size is invalid")
    try:
        envelope = base64.b64decode(encoded, validate=True).decode("ascii")
        ciphertext_b64, iv_hex = envelope.split(":", 1)
        ciphertext = base64.b64decode(ciphertext_b64, validate=True)
        iv = bytes.fromhex(iv_hex)
    except (UnicodeError, ValueError, binascii.Error) as exc:
        raise OddsPortalXHRDecodeError("OddsPortal XHR envelope is invalid") from exc
    if len(iv) != 16 or not ciphertext or len(ciphertext) % 16:
        raise OddsPortalXHRDecodeError("OddsPortal XHR cipher parameters are invalid")

    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_PAYLOAD_SALT,
        iterations=_PBKDF2_ITERATIONS,
    ).derive(_PAYLOAD_PASSWORD)
    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
        decoded = unpadder.update(padded) + unpadder.finalize()
    except ValueError as exc:
        raise OddsPortalXHRDecodeError(
            f"OddsPortal XHR decryption failed for decoder {DECODER_REVISION}"
        ) from exc

    if decoded.startswith(b"\x1f\x8b"):
        decoded = _bounded_gzip_decompress(decoded)
    if len(decoded) > MAX_DECODED_PAYLOAD_BYTES:
        raise OddsPortalXHRDecodeError("OddsPortal decoded XHR payload is too large")
    try:
        data = json.loads(decoded)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OddsPortalXHRDecodeError("OddsPortal decoded XHR payload is not JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("d"), dict):
        raise OddsPortalXHRSchemaError("OddsPortal decoded XHR payload does not contain an object at 'd'")
    return data


def extract_page_bootstrap(html: str, *, page_url: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "lxml")
    component = soup.find("next-matches")
    if not isinstance(component, Tag):
        raise OddsPortalXHRSchemaError("OddsPortal listing does not expose next-matches")
    raw_request = component.get(":odds-request")
    if not isinstance(raw_request, str):
        raise OddsPortalXHRSchemaError("OddsPortal listing does not expose odds-request")
    try:
        request = json.loads(raw_request)
    except json.JSONDecodeError as exc:
        raise OddsPortalXHRSchemaError("OddsPortal listing odds-request is invalid") from exc
    request_url = request.get("url")
    if not isinstance(request_url, str) or not request_url.startswith("/ajax-"):
        raise OddsPortalXHRSchemaError("OddsPortal listing odds-request URL is invalid")

    user_data_script = next(
        (
            script.get("src")
            for script in soup.find_all("script", src=True)
            if isinstance(script, Tag)
            and isinstance(script.get("src"), str)
            and "/ajax-user-data/" in script.get("src", "")
        ),
        None,
    )
    if not isinstance(user_data_script, str):
        raise OddsPortalXHRSchemaError("OddsPortal listing does not expose ajax-user-data")
    joined_request_url = urljoin(page_url, request_url)
    joined_user_data_url = urljoin(page_url, user_data_script)
    if not _is_trusted_oddsportal_url(urlsplit(joined_request_url)):
        raise OddsPortalXHRSchemaError("OddsPortal listing request host is not trusted")
    if (
        not _is_trusted_oddsportal_url(urlsplit(joined_user_data_url))
        or not urlsplit(joined_user_data_url).path.startswith("/ajax-user-data/")
    ):
        raise OddsPortalXHRSchemaError("OddsPortal user-data script host is not trusted")
    return joined_request_url, joined_user_data_url


def parse_user_data_script(script: str) -> dict[str, Any]:
    for match in _JSON_PARSE_ARGUMENT.finditer(script):
        try:
            embedded_json = json.loads(match.group(1))
            parsed = json.loads(embedded_json)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("bookiehash"), str):
            return parsed
    raise OddsPortalXHRSchemaError("OddsPortal ajax-user-data did not expose a bookiehash")


def build_listing_xhr_url(
    *,
    request_base_url: str,
    bookiehash: str,
    page: int,
    timestamp_ms: int,
) -> str:
    parsed = urlsplit(request_base_url)
    if not _is_trusted_oddsportal_url(parsed):
        raise OddsPortalXHRSchemaError("OddsPortal listing request host is not trusted")
    if not bookiehash.startswith("X") or not re.fullmatch(r"[A-Za-z0-9]+", bookiehash):
        raise OddsPortalXHRSchemaError("OddsPortal bookiehash is invalid")
    if page < 1:
        raise OddsPortalXHRSchemaError("OddsPortal listing page must be positive")
    base = request_base_url.rstrip("/")
    return f"{base}/{bookiehash}/{page}/0/?_={timestamp_ms}"


def listing_rows(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    body = payload.get("d")
    rows = body.get("rows") if isinstance(body, dict) else None
    pagination = body.get("pagination") if isinstance(body, dict) else None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise OddsPortalXHRSchemaError("OddsPortal listing payload does not contain rows")
    page_count = pagination.get("pageCount", 1) if isinstance(pagination, dict) else 1
    if not isinstance(page_count, int) or page_count < 1:
        raise OddsPortalXHRSchemaError("OddsPortal listing pagination is invalid")
    total = body.get("total") if isinstance(body, dict) else None
    one_page = body.get("onePage") if isinstance(body, dict) else None
    if isinstance(total, int) and total > 0:
        if not isinstance(one_page, int) or one_page < 1:
            raise OddsPortalXHRSchemaError("OddsPortal listing page size is invalid")
        if page_count < math.ceil(total / one_page):
            raise OddsPortalXHRSchemaError("OddsPortal listing pagination would truncate rows")
    return rows, page_count


def listing_page_metadata(payload: dict[str, Any]) -> tuple[int, int, int]:
    body = payload.get("d")
    if not isinstance(body, dict):
        raise OddsPortalXHRSchemaError("OddsPortal listing payload body is invalid")
    total = body.get("total")
    one_page = body.get("onePage")
    current_page = body.get("page", 1)
    if not isinstance(total, int) or total < 0:
        raise OddsPortalXHRSchemaError("OddsPortal listing total is invalid")
    if not isinstance(current_page, int) or current_page < 1:
        raise OddsPortalXHRSchemaError("OddsPortal listing current page is invalid")
    if total == 0 and one_page is None:
        one_page = 1
    if not isinstance(one_page, int) or one_page < 1:
        raise OddsPortalXHRSchemaError("OddsPortal listing page size is invalid")
    return total, one_page, current_page


def event_data_url(match_link: str, *, base_url: str | None = None) -> str:
    event_id = event_id_from_match_link(match_link)
    parsed = urlsplit(match_link)
    if not _is_trusted_oddsportal_url(parsed):
        raise OddsPortalXHRSchemaError("OddsPortal match URL host is not trusted")
    origin = _trusted_origin(base_url or f"{parsed.scheme}://{parsed.netloc}")
    return f"{origin}{parsed.path}?eventId={quote(event_id)}"


def event_id_from_match_link(match_link: str) -> str:
    parsed = urlsplit(match_link)
    fragment = parsed.fragment
    event_id = fragment.split(":", 1)[0].split(";", 1)[0]
    if not event_id:
        segments = [segment for segment in parsed.path.split("/") if segment]
        if len(segments) == 4 and segments[0] == "football" and segments[1] != "h2h":
            event_id = segments[-1].rsplit("-", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,16}", event_id):
        raise OddsPortalXHRSchemaError("OddsPortal match link does not contain a valid event ID")
    return event_id


def build_market_xhr_url(
    event_payload: dict[str, Any],
    *,
    market: str,
    base_url: str | None = None,
    scope_id: int = 2,
    geo: str = "RO",
    locale: str = "en",
) -> str:
    body = event_payload.get("d")
    sport_data = body.get("sportData") if isinstance(body, dict) else None
    event_data = sport_data.get("eventData") if isinstance(sport_data, dict) else None
    if not isinstance(event_data, dict):
        raise OddsPortalXHRSchemaError("OddsPortal event payload does not contain eventData")
    betting_type_id = MARKET_BETTING_TYPE_IDS.get(market)
    if betting_type_id is None:
        raise OddsPortalXHRSchemaError(f"Unsupported OddsPortal XHR market: {market}")
    request_base = event_data.get("requestBasePreMatch")
    if not request_base and isinstance(sport_data, dict):
        request_base = sport_data.get("requestBasePreMatch")
    event_id = event_data.get("id")
    version_id = event_data.get("versionId")
    sport_id = event_data.get("sportId")
    event_hash = unquote(str(event_data.get("xhash") or ""))
    if (
        not isinstance(request_base, str)
        or not request_base.startswith("/match-event/")
        or not isinstance(event_id, str)
        or not isinstance(version_id, int)
        or not isinstance(sport_id, int)
        or not re.fullmatch(r"[A-Za-z0-9]+", event_hash)
    ):
        raise OddsPortalXHRSchemaError("OddsPortal event payload cannot build a market request")
    origin = _trusted_origin(base_url or ODDSPORTAL_BASE_URL)
    if not re.fullmatch(r"[A-Z]{2}", geo):
        raise OddsPortalXHRSchemaError("OddsPortal market geo is invalid")
    if not re.fullmatch(r"[a-z]{2}(?:-[A-Z]{2})?", locale):
        raise OddsPortalXHRSchemaError("OddsPortal market locale is invalid")
    return (
        f"{origin}{request_base}{version_id}-{sport_id}-{event_id}-"
        f"{betting_type_id}-{scope_id}-{event_hash}.dat?geo={geo}&lang={locale}"
    )


def match_record_from_event_payload(
    payload: dict[str, Any],
    *,
    match_link: str,
) -> dict[str, Any]:
    body = payload.get("d")
    sport_data = body.get("sportData") if isinstance(body, dict) else None
    event_data = sport_data.get("eventData") if isinstance(sport_data, dict) else None
    event_body = sport_data.get("eventBody") if isinstance(sport_data, dict) else None
    if not isinstance(event_data, dict) or not isinstance(event_body, dict):
        raise OddsPortalXHRSchemaError("OddsPortal event payload does not contain match details")
    expected_event_id = event_id_from_match_link(match_link)
    if event_data.get("id") != expected_event_id:
        raise OddsPortalXHRSchemaError("OddsPortal event payload belongs to another match")
    start_date = event_body.get("startDate")
    match_date = (
        datetime.fromtimestamp(start_date, tz=UTC).strftime("%Y-%m-%d %H:%M:%S %Z")
        if isinstance(start_date, int | float)
        else None
    )
    return {
        "scraped_date": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "match_date": match_date,
        "match_link": match_link,
        "home_team": event_data.get("home"),
        "away_team": event_data.get("away"),
        "league_name": event_data.get("tournamentName"),
        "home_score": str(event_body["homeResult"]) if event_body.get("homeResult") is not None else None,
        "away_score": str(event_body["awayResult"]) if event_body.get("awayResult") is not None else None,
        "partial_results": clean_html_text(event_body.get("partialresult")),
        "venue": _ascii_or_none(event_body.get("venue")),
        "venue_town": _ascii_or_none(event_body.get("venueTown")),
        "venue_country": event_body.get("venueCountry"),
    }


def event_payload_from_static_html(html: str, *, match_link: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    component = soup.find("div", id="react-event-header")
    raw_data = component.get("data") if isinstance(component, Tag) else None
    if not isinstance(raw_data, str):
        raise OddsPortalXHRSchemaError("OddsPortal static match page does not expose event bootstrap")
    try:
        sport_data = json.loads(raw_data)
    except json.JSONDecodeError as exc:
        raise OddsPortalXHRSchemaError("OddsPortal static match event bootstrap is invalid") from exc
    event_data = sport_data.get("eventData") if isinstance(sport_data, dict) else None
    if not isinstance(event_data, dict) or event_data.get("id") != event_id_from_match_link(match_link):
        raise OddsPortalXHRSchemaError("OddsPortal static match page exposes a different event")
    return {"d": {"sportData": sport_data}}


def provider_names_from_payload(payload: dict[str, Any]) -> dict[str, str]:
    body = payload.get("d")
    names = body.get("providersNames") if isinstance(body, dict) else None
    if not isinstance(names, dict):
        raise OddsPortalXHRSchemaError("OddsPortal provider payload does not contain providersNames")
    return {str(provider_id): str(name) for provider_id, name in names.items() if name}


def market_rows_from_payload(
    payload: dict[str, Any],
    *,
    market: str,
    provider_names: dict[str, str],
    target_bookmaker: str | None = None,
) -> list[dict[str, str]]:
    body = payload.get("d")
    oddsdata = body.get("oddsdata") if isinstance(body, dict) else None
    back = oddsdata.get("back") if isinstance(oddsdata, dict) else None
    if not isinstance(back, dict):
        raise OddsPortalXHRSchemaError("OddsPortal market payload does not contain back odds")
    expected_type = MARKET_BETTING_TYPE_IDS[market]
    expected_handicap = MARKET_HANDICAPS.get(market)
    labels = MARKET_OUTCOME_LABELS[market]
    selected: dict[str, Any] | None = None
    for candidate in back.values():
        if not isinstance(candidate, dict) or candidate.get("bettingTypeId") != expected_type:
            continue
        if candidate.get("scopeId") != 2:
            continue
        if expected_handicap is not None and _normalized_handicap(candidate.get("handicapValue")) != expected_handicap:
            continue
        selected = candidate
        break
    if selected is None:
        raise OddsPortalXHRSchemaError(f"OddsPortal market payload does not contain {market}")
    odds = selected.get("odds")
    active = selected.get("act", {})
    if not isinstance(odds, dict):
        raise OddsPortalXHRSchemaError(f"OddsPortal market payload has invalid odds for {market}")

    rows: list[dict[str, str]] = []
    for provider_id, values in odds.items():
        provider_key = str(provider_id)
        if isinstance(active, dict) and active.get(provider_key) is False:
            continue
        bookmaker_name = provider_names.get(provider_key)
        if bookmaker_name is None:
            raise OddsPortalXHRSchemaError(
                f"OddsPortal provider catalog does not contain provider ID {provider_key}"
            )
        if target_bookmaker and bookmaker_name.casefold() != target_bookmaker.casefold():
            continue
        normalized_values = _outcome_values(values, labels)
        if normalized_values is None:
            continue
        row = {"bookmaker_name": bookmaker_name, "period": "FullTime"}
        row.update(normalized_values)
        rows.append(row)
    if not rows:
        raise OddsPortalXHRSchemaError(f"OddsPortal market payload did not produce bookmaker rows for {market}")
    return rows


def _outcome_values(values: Any, labels: tuple[str, ...]) -> dict[str, str] | None:
    if isinstance(values, dict):
        ordered = [values.get(str(index)) for index in range(len(labels))]
    elif isinstance(values, list):
        ordered = values[: len(labels)]
    else:
        return None
    if len(ordered) != len(labels) or any(value is None for value in ordered):
        return None
    return {label: _format_decimal_odd(value) for label, value in zip(labels, ordered, strict=True)}


def _normalized_handicap(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return ""


def _ascii_or_none(value: str | None) -> str | None:
    if not value:
        return None
    return value.encode("ascii", "ignore").decode("ascii")


def _format_decimal_odd(value: Any) -> str:
    try:
        odd = float(value)
    except (TypeError, ValueError) as exc:
        raise OddsPortalXHRSchemaError("OddsPortal market contains a non-numeric odd") from exc
    if not math.isfinite(odd) or odd <= 1:
        raise OddsPortalXHRSchemaError("OddsPortal market contains an invalid decimal odd")
    return f"{odd:.2f}"


def _bounded_gzip_decompress(data: bytes) -> bytes:
    try:
        inflater = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
        decoded = inflater.decompress(data, MAX_DECODED_PAYLOAD_BYTES + 1)
        if inflater.unconsumed_tail or len(decoded) > MAX_DECODED_PAYLOAD_BYTES:
            raise OddsPortalXHRDecodeError("OddsPortal decoded XHR payload is too large")
        decoded += inflater.flush(MAX_DECODED_PAYLOAD_BYTES + 1 - len(decoded))
    except zlib.error as exc:
        raise OddsPortalXHRDecodeError("OddsPortal XHR gzip payload is invalid") from exc
    if not inflater.eof:
        raise OddsPortalXHRDecodeError("OddsPortal XHR gzip payload is incomplete")
    if len(decoded) > MAX_DECODED_PAYLOAD_BYTES:
        raise OddsPortalXHRDecodeError("OddsPortal decoded XHR payload is too large")
    return decoded


def _trusted_origin(url: str) -> str:
    parsed = urlsplit(url)
    if not _is_trusted_oddsportal_url(parsed):
        raise OddsPortalXHRSchemaError("OddsPortal base URL host is not trusted")
    return f"https://{parsed.netloc}".rstrip("/")


def _is_trusted_oddsportal_url(parsed: Any) -> bool:
    try:
        port = parsed.port
    except ValueError:
        return False
    host = parsed.hostname
    return (
        parsed.scheme == "https"
        and isinstance(host, str)
        and (host == "oddsportal.com" or host.endswith(".oddsportal.com"))
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
    )
