"""
Map Apify / LinkedIn CSV export columns to lead fields for dashboard import.

Apify actors use inconsistent headers (url vs profileUrl vs linkedin_url, etc.).
Extend _ALIASES below when your export's first row differs — paste headers from the CSV.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any

import sqlite3

from core.ids import make_lead_id, normalize_linkedin_url
from core.repository import insert_lead_csv_import

# logical_field -> tuple of lowercase header aliases (first match wins per row)
_ALIASES: dict[str, tuple[str, ...]] = {
    "linkedin_url": (
        "linkedin_url",
        "linkedinurl",
        "linkedin",
        "url",
        "profileurl",
        "profile_url",
        "linkedinprofileurl",
        "linkedin_profile_url",
        "publicidentifier",
        "link",
    ),
    "lead_id": ("lead_id", "leadid"),
    "apollo_person_id": (
        "apollo_person_id",
        "apollopersonid",
        "apollo_id",
        "memberid",
        "member_id",
        "profileid",
        "profile_id",
        "linkedinmemberid",
    ),
    "email": ("email", "emailaddress", "email_address", "workemail"),
    "first_name": ("first_name", "firstname", "givenname", "given_name"),
    "last_name": ("last_name", "lastname", "surname", "familyname"),
    "full_name": ("full_name", "fullname", "name", "displayname"),
    "company_name": (
        "company_name",
        "companyname",
        "company",
        "organizationname",
        "organization_name",
        "employer",
    ),
    "title": (
        "title",
        "currentjobtitle",
        "jobtitle",
        "headline",
        "position",
        "occupation",
    ),
    "industry": ("industry", "companyindustry", "sector"),
    "recent_activity": (
        "recent_activity",
        "recentactivity",
        "activity_snippet",
        "activitysnippet",
        "linkedin_activity",
        "linkedinactivity",
    ),
    "location": ("location", "address", "city", "country", "geo", "region"),
    "years_experience": (
        "years_experience",
        "yearsexperience",
        "years_of_experience",
        "experienceyears",
    ),
}


def _norm_key(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").strip().lower())


def _pick(row: dict[str, str], logical: str) -> str:
    aliases = _ALIASES.get(logical, ())
    by_norm: dict[str, str] = {}
    for k, v in row.items():
        nk = _norm_key(k)
        if nk:
            by_norm[nk] = v
    for a in aliases:
        na = _norm_key(a)
        if na in by_norm:
            return str(by_norm[na] or "").strip()
    return ""


def _valid_import_lead_id(value: str) -> bool:
    s = (value or "").strip()
    return bool(re.fullmatch(r"[a-fA-F0-9]{24}", s))


def _parse_years(raw: str) -> int:
    s = (raw or "").strip()
    if not s:
        return 0
    try:
        return int(float(re.sub(r"[^\d.]", "", s.split(".")[0])))
    except (ValueError, TypeError):
        return 0


def map_apify_csv_row(row: dict[str, str]) -> dict[str, Any] | None:
    """
    Map one CSV row (header -> cell) to fields for insert_lead_csv_import.
    Returns None if linkedin_url cannot be resolved after normalization.
    """
    li_raw = _pick(row, "linkedin_url")
    li = normalize_linkedin_url(li_raw)
    if not li:
        return None

    apollo = _pick(row, "apollo_person_id")
    explicit_lid = _pick(row, "lead_id")
    if _valid_import_lead_id(explicit_lid):
        lead_id = explicit_lid
    else:
        lead_id = make_lead_id(apollo, li)

    fn = _pick(row, "first_name") or None
    ln = _pick(row, "last_name") or None
    full = _pick(row, "full_name") or None
    if not full and (fn or ln):
        full = " ".join(x for x in (fn or "", ln or "") if x).strip() or None

    return {
        "lead_id": lead_id,
        "linkedin_url": li,
        "apollo_person_id": apollo,
        "email": _pick(row, "email") or None,
        "first_name": fn,
        "last_name": ln,
        "full_name": full,
        "company_name": _pick(row, "company_name") or None,
        "title": _pick(row, "title") or None,
        "industry": _pick(row, "industry") or None,
        "location": _pick(row, "location") or None,
        "years_experience": _parse_years(_pick(row, "years_experience")),
        "recent_activity": _pick(row, "recent_activity") or None,
    }


def decode_csv_rows(data: bytes) -> list[dict[str, str]]:
    """Decode UTF-8 (with BOM strip) and return list of row dicts."""
    text = ""
    for enc in ("utf-8-sig", "utf-8"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        text = data.decode("utf-8", errors="replace")
    f = io.StringIO(text)
    reader = csv.DictReader(f)
    rows: list[dict[str, str]] = []
    for raw in reader:
        row = {str(k or ""): str(v or "") for k, v in raw.items()}
        rows.append(row)
    return rows


def import_apify_csv(
    conn: sqlite3.Connection,
    data: bytes,
    campaign_id: str,
    *,
    max_errors: int = 20,
) -> tuple[int, int, list[str]]:
    """
    Parse CSV bytes and insert mapped rows. Returns (imported, skipped, errors).
    """
    errors: list[str] = []
    imported = 0
    skipped = 0

    try:
        dict_rows = decode_csv_rows(data)
    except Exception as e:
        return 0, 0, [f"csv_decode:{type(e).__name__}:{e}"]

    if not dict_rows:
        return 0, 0, ["empty_csv"]

    for i, row in enumerate(dict_rows, start=2):
        mapped = map_apify_csv_row(row)
        if not mapped:
            skipped += 1
            if len(errors) < max_errors:
                errors.append(f"row {i}: missing_or_invalid_linkedin_url")
            continue
        try:
            ok = insert_lead_csv_import(
                conn,
                lead_id=mapped["lead_id"],
                linkedin_url=mapped["linkedin_url"],
                campaign_id=campaign_id,
                apollo_person_id=str(mapped.get("apollo_person_id") or ""),
                email=mapped.get("email"),
                first_name=mapped.get("first_name"),
                last_name=mapped.get("last_name"),
                full_name=mapped.get("full_name"),
                company_name=mapped.get("company_name"),
                title=mapped.get("title"),
                industry=mapped.get("industry"),
                location=mapped.get("location"),
                years_experience=int(mapped.get("years_experience") or 0),
                recent_activity=mapped.get("recent_activity"),
            )
        except Exception as e:
            skipped += 1
            if len(errors) < max_errors:
                errors.append(f"row {i}:{type(e).__name__}:{e}")
            continue
        if ok:
            imported += 1
        else:
            skipped += 1
            if len(errors) < max_errors:
                errors.append(f"row {i}: duplicate_lead_id_skipped")

    return imported, skipped, errors
