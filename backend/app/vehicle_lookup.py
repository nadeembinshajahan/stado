"""Anonymized Indian vehicle identification from a license plate.

ETHICS / SCOPE
--------------
This module surfaces only ANONYMIZED vehicle data. It is NOT a PII scraper:

  * The plate -> state / RTO decode is done LOCALLY from a built-in code map,
    so it always works offline.
  * Make/model/fuel/year/owner come from a PLUGGABLE provider. The default is a
    deterministic MOCK (seeded off the plate hash) returning plausible but
    FICTITIOUS data. A real REST API is used ONLY if the operator explicitly
    sets ``settings.vehicle_api_url`` (+ optional key).
  * Owner names are ALWAYS masked (e.g. "R**** S****") regardless of the
    source, via ``mask_name``. We never store or surface a raw owner name.

Nothing here scrapes Parivahan or hardcodes any real personal data.
"""
from __future__ import annotations

import hashlib
import logging
import re

import httpx

from .config import settings

log = logging.getLogger("gcs.vehicle")

# ── Indian state / UT code map (RTO prefix -> region) ────────────────────────
# Covers all states and union territories. Defence/diplomatic/CD codes included
# as best-effort. Decoded locally; no network needed.
STATE_CODES: dict[str, str] = {
    "AP": "Andhra Pradesh",
    "AR": "Arunachal Pradesh",
    "AS": "Assam",
    "BR": "Bihar",
    "CG": "Chhattisgarh",
    "CH": "Chandigarh",
    "DD": "Daman and Diu",
    "DL": "Delhi",
    "DN": "Dadra and Nagar Haveli",
    "GA": "Goa",
    "GJ": "Gujarat",
    "HP": "Himachal Pradesh",
    "HR": "Haryana",
    "JH": "Jharkhand",
    "JK": "Jammu and Kashmir",
    "KA": "Karnataka",
    "KL": "Kerala",
    "LA": "Ladakh",
    "LD": "Lakshadweep",
    "MH": "Maharashtra",
    "ML": "Meghalaya",
    "MN": "Manipur",
    "MP": "Madhya Pradesh",
    "MZ": "Mizoram",
    "NL": "Nagaland",
    "OD": "Odisha",
    "OR": "Odisha",  # legacy prefix for Odisha
    "PB": "Punjab",
    "PY": "Puducherry",
    "RJ": "Rajasthan",
    "SK": "Sikkim",
    "TN": "Tamil Nadu",
    "TR": "Tripura",
    "TS": "Telangana",
    "TG": "Telangana",  # alternate Telangana prefix
    "UK": "Uttarakhand",
    "UA": "Uttarakhand",  # legacy prefix for Uttarakhand
    "UP": "Uttar Pradesh",
    "WB": "West Bengal",
    "AN": "Andaman and Nicobar Islands",
    "CD": "Diplomatic Corps",
}

# Indian civilian plate: 2-letter state, 1-2 digit RTO, 0-3 letter series,
# 1-4 digit number. e.g. KA01AB1234, MH12A1234, DL1CAA9999.
_PLATE_RE = re.compile(r"^([A-Z]{2})(\d{1,2})([A-Z]{0,3})(\d{1,4})$")

# Mock provider value pools — fictitious, plausible, and deterministically
# chosen per plate so the same plate always yields the same record.
_MOCK_MAKERS = [
    "Maruti Suzuki Swift", "Hyundai Creta", "Tata Nexon", "Mahindra XUV700",
    "Honda City", "Toyota Innova Crysta", "Kia Seltos", "Renault Kwid",
    "Maruti Suzuki Baleno", "Hyundai Venue", "Tata Punch", "Volkswagen Virtus",
    "Skoda Slavia", "MG Hector", "Maruti Suzuki Brezza", "Toyota Fortuner",
]
_MOCK_FUELS = ["Petrol", "Diesel", "CNG", "Petrol+CNG", "Electric"]
_MOCK_CLASSES = [
    "LMV (Car)", "LMV (Car)", "LMV (Car)", "Motor Cab", "Goods Carrier",
]
# Fictitious owner names — only ever surfaced AFTER masking via mask_name().
_MOCK_OWNERS = [
    "Rahul Sharma", "Priya Nair", "Amit Verma", "Sneha Reddy", "Vikram Singh",
    "Anjali Mehta", "Karthik Iyer", "Deepa Krishnan", "Suresh Patel", "Neha Gupta",
]

# Simple in-process cache so the same plate is not re-looked-up.
_CACHE: dict[str, dict] = {}


def mask_name(name: str | None) -> str:
    """Mask a personal name, keeping only the first letter of each word.

    "Rahul Sharma" -> "R**** S*****". Empty / falsy input -> "REDACTED".
    """
    if not name or not str(name).strip():
        return "REDACTED"
    parts = str(name).strip().split()
    masked: list[str] = []
    for p in parts:
        if not p:
            continue
        # Keep the leading character; mask the remaining glyphs with '*'.
        rest = "*" * max(len(p) - 1, 1)
        masked.append(p[0].upper() + rest)
    return " ".join(masked) if masked else "REDACTED"


def _normalize(plate: str) -> str:
    """Strip whitespace/separators and uppercase. 'ka 01-ab 1234' -> 'KA01AB1234'."""
    return re.sub(r"[\s\-_.]+", "", str(plate or "")).upper()


def decode_plate(plate: str) -> dict:
    """Decode the offline parts of an Indian plate.

    Returns ``{plate, valid, state, rto_code}``. Works offline, always; never
    raises. ``state`` is the decoded region (or None) and ``rto_code`` is the
    "<STATE><RTO>" prefix (e.g. "KA01") when valid.
    """
    norm = _normalize(plate)
    m = _PLATE_RE.match(norm)
    if not m:
        return {"plate": norm, "valid": False, "state": None, "rto_code": None}
    state_prefix, rto_num = m.group(1), m.group(2)
    state = STATE_CODES.get(state_prefix)
    return {
        "plate": norm,
        "valid": True,
        "state": state,
        "rto_code": f"{state_prefix}{rto_num}",
    }


def _plate_seed(plate: str) -> int:
    """Stable integer seed derived from the plate (deterministic across runs)."""
    digest = hashlib.sha256(plate.encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def _mock_details(plate: str) -> dict:
    """Deterministic, FICTITIOUS anonymized record seeded off the plate."""
    seed = _plate_seed(plate)
    maker = _MOCK_MAKERS[seed % len(_MOCK_MAKERS)]
    fuel = _MOCK_FUELS[(seed >> 4) % len(_MOCK_FUELS)]
    vclass = _MOCK_CLASSES[(seed >> 8) % len(_MOCK_CLASSES)]
    owner_raw = _MOCK_OWNERS[(seed >> 12) % len(_MOCK_OWNERS)]
    reg_year = 2008 + (seed >> 16) % 17  # 2008..2024
    return {
        "maker_model": maker,
        "fuel": fuel,
        "reg_year": str(reg_year),
        "vehicle_class": vclass,
        "owner": mask_name(owner_raw),
        "source": "mock",
    }


def _map_api_response(data: dict) -> dict:
    """Tolerantly map a real provider's JSON to our field names.

    Providers vary wildly; we accept a few common key spellings for each field
    and always mask the owner. Missing fields become None (except the masked
    owner, which falls back to REDACTED).
    """
    if not isinstance(data, dict):
        data = {}
    # Some APIs nest the record under "data" / "result"; unwrap one level.
    for wrapper in ("data", "result", "response", "vehicle"):
        inner = data.get(wrapper)
        if isinstance(inner, dict):
            data = inner
            break

    def pick(*keys: str):
        for k in keys:
            v = data.get(k)
            if v not in (None, "", []):
                return v
        return None

    owner_raw = pick("owner", "owner_name", "ownerName", "name", "registered_owner")
    return {
        "maker_model": pick(
            "maker_model", "makerModel", "model", "vehicle", "maker_description",
            "make_model", "brand_model",
        ),
        "fuel": pick("fuel", "fuel_type", "fuelType", "fuel_descr"),
        "reg_year": _as_str(
            pick("reg_year", "regYear", "year", "manufacturing_year", "mfg_year",
                 "registration_date")
        ),
        "vehicle_class": pick(
            "vehicle_class", "vehicleClass", "class", "vehicle_category", "category"
        ),
        "owner": mask_name(owner_raw),
        "source": "api",
    }


def _as_str(v) -> str | None:
    if v in (None, ""):
        return None
    return str(v)


async def _fetch_api(plate: str) -> dict | None:
    """GET the operator-configured provider for ``plate``. Returns mapped fields
    or None on any failure. The key (if set) is sent both as a header and as a
    query param so it works with most simple REST providers."""
    url = settings.vehicle_api_url
    if not url:
        return None
    key = settings.vehicle_api_key
    params = {"plate": plate, "registration_number": plate, "vehicleNumber": plate}
    headers = {"Accept": "application/json"}
    if key:
        params["key"] = key
        params["api_key"] = key
        headers["Authorization"] = f"Bearer {key}"
        headers["x-api-key"] = key
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(url, params=params, headers=headers)
            r.raise_for_status()
            return _map_api_response(r.json())
    except Exception as exc:  # noqa: BLE001
        log.warning("vehicle API lookup failed for %s: %s", plate, exc)
        return None


async def lookup(plate: str) -> dict:
    """Return the offline decode merged with anonymized vehicle details.

    Uses the configured REST provider if ``settings.vehicle_api_url`` is set,
    otherwise a deterministic MOCK. The owner is ALWAYS masked. Results are
    cached by normalized plate. Never raises — on any failure returns at least
    the offline decode.
    """
    base = decode_plate(plate)
    norm = base["plate"]
    if not base["valid"]:
        return base
    if norm in _CACHE:
        return _CACHE[norm]

    details: dict | None = None
    if settings.vehicle_api_url:
        details = await _fetch_api(norm)
    if details is None:
        details = _mock_details(norm)

    # Defensive: never let an upstream record leak an unmasked owner.
    if "owner" in details:
        owner_val = details["owner"]
        # If a mapper somehow returned a raw name, mask again (idempotent for
        # already-masked values like "R**** S*****").
        if owner_val and "*" not in str(owner_val) and owner_val != "REDACTED":
            details["owner"] = mask_name(owner_val)

    merged = {**base, **details}
    _CACHE[norm] = merged
    return merged
