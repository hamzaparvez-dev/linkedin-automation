"""
Strict ICP rules for outreach-ready CSV export (quality gate).

Independent from post_filters.py / icp_constants.py headcount limits so the main
Apollo Web3 pipeline behavior stays unchanged.
"""

from __future__ import annotations

import os
import re
from typing import Any

from lead_extraction.company_size import parse_employee_upper_bound
from lead_extraction.icp_constants import WEB3_KEYWORDS
from lead_extraction.normalize import normalize_linkedin_url

# Generic / role inboxes (localpart before @).
_GENERIC_LOCALPARTS: frozenset[str] = frozenset(
    x.strip().lower()
    for x in os.getenv(
        "ICP_GENERIC_EMAIL_LOCALPARTS",
        "info,support,admin,contact,hello,team,sales,noreply,no-reply,help,office,mail",
    ).split(",")
    if x.strip()
)

_PERSONAL_EMAIL_DOMAINS: frozenset[str] = frozenset(
    x.strip().lower()
    for x in os.getenv(
        "ICP_PERSONAL_EMAIL_DOMAINS",
        "gmail.com,yahoo.com,yahoo.co.uk,hotmail.com,outlook.com,live.com,msn.com,"
        "icloud.com,me.com,mac.com,aol.com,protonmail.com,proton.me,gmx.com,mail.com,"
        "hey.com,pm.me",
    ).split(",")
    if x.strip()
)

# Industry: allow IT / software / internet; financial services only with Web3 signal (handled below).
_INDUSTRY_ALLOW_FRAGMENTS: tuple[str, ...] = (
    "information technology",
    "computer software",
    "internet",
)

_EXCLUSION_PHRASES: tuple[str, ...] = tuple(
    x.strip().lower()
    for x in os.getenv(
        "ICP_OUTREACH_EXCLUSION_PHRASES",
        "marketing agency,digital agency,ad agency,advertising agency,"
        "consulting firm,management consulting,staffing agency,outsourcing,"
        "healthcare,pharma,pharmaceutical,hospital,clinic,"
        "agriculture,logistics,nonprofit,non-profit,charity,"
        "recruiting agency,media agency,creative agency",
    ).split(",")
    if x.strip()
)

_GEO_SUBSTRINGS: tuple[str, ...] = tuple(
    x.strip().lower()
    for x in os.getenv(
        "ICP_OUTREACH_GEO_SUBSTRINGS",
        "united states,usa,u.s.,u.s.a.,singapore,united arab emirates,uae,dubai,abu dhabi,india",
    ).split(",")
    if x.strip()
)


def _company_name_reject_tokens() -> tuple[str, ...]:
    raw = os.getenv(
        "ICP_COMPANY_NAME_REJECT_SUBSTRINGS",
        "agency,consulting,solutions,services,studio",
    )
    return tuple(x.strip().lower() for x in raw.split(",") if x.strip())


def _product_signal_substrings() -> tuple[str, ...]:
    raw = os.getenv(
        "ICP_PRODUCT_SIGNAL_SUBSTRINGS",
        "platform,protocol,saas,infrastructure,b2b,api,enterprise,software",
    )
    return tuple(x.strip().lower() for x in raw.split(",") if x.strip())


def _require_product_signal() -> bool:
    return os.getenv("ICP_OUTREACH_REQUIRE_PRODUCT_SIGNAL", "").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _lower(s: Any) -> str:
    return str(s or "").strip().lower()


def _haystack(row: dict[str, Any]) -> str:
    parts = [
        row.get("title"),
        row.get("company_name"),
        row.get("industry"),
        row.get("organization_keywords"),
        row.get("organization_short_description"),
        row.get("organization_seo_description"),
    ]
    return " ".join(_lower(p) for p in parts)


def has_web3_signal(row: dict[str, Any]) -> bool:
    hits = _lower(row.get("web3_keyword_hits"))
    if hits and hits not in ("0", "none", "false"):
        return True
    text = _haystack(row)
    return any(kw in text for kw in WEB3_KEYWORDS)


def has_product_company_signal(row: dict[str, Any]) -> bool:
    """B2B / product-building signals (same haystack as Web3 scan)."""
    text = _haystack(row)
    return any(sig in text for sig in _product_signal_substrings())


def strict_founder_ceo_title(title: str) -> bool:
    """Founder, Co-Founder, CEO (incl. Chief Executive Officer). Excludes President-only, MD, etc."""
    t = _lower(title).replace("–", "-")
    if "co-founder" in t or "co founder" in t:
        return True
    if "cofounder" in t.replace(" ", ""):
        return True
    if "chief executive" in t:
        return True
    if re.search(r"\bceo\b", t):
        return True
    if re.search(r"\bfounder\b", t):
        return True
    return False


def industry_allowed(row: dict[str, Any]) -> bool:
    ind = _lower(row.get("industry"))
    if not ind:
        return False
    if "financial" in ind and "service" in ind:
        return has_web3_signal(row)
    return any(fragment in ind for fragment in _INDUSTRY_ALLOW_FRAGMENTS)


def excluded_vertical(row: dict[str, Any]) -> bool:
    h = _haystack(row)
    return any(phrase in h for phrase in _EXCLUSION_PHRASES)


def company_name_matches_service_reject(row: dict[str, Any]) -> bool:
    """
    Reject service-like company names using ICP_COMPANY_NAME_REJECT_SUBSTRINGS.
    Single-word tokens use word boundaries; multi-word tokens use substring match.
    For 'consulting', allow names that clearly sell software (legacy heuristic).
    """
    name = _lower(row.get("company_name"))
    if not name:
        return True
    for token in _company_name_reject_tokens():
        if " " in token:
            if token in name:
                return True
            continue
        if not re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", name):
            continue
        if token == "consulting" and "software" in name:
            continue
        return True
    return False


def agency_or_non_product_company(row: dict[str, Any]) -> bool:
    """Hard reject agency / consulting / service-style company names."""
    return company_name_matches_service_reject(row)


def headcount_allowed(row: dict[str, Any]) -> bool:
    max_e = int(os.getenv("ICP_OUTREACH_MAX_EMPLOYEES", "50"))
    org = row.get("_organization_raw") if isinstance(row.get("_organization_raw"), dict) else None
    upper = parse_employee_upper_bound(row.get("company_size"), org)
    if upper is None:
        return False
    return upper <= max_e and upper >= 1


def email_localpart_domain_ok(email: str) -> bool:
    e = str(email or "").strip().lower()
    if not e or "@" not in e:
        return False
    local, _, domain = e.partition("@")
    domain = domain.strip().lower()
    local_base = local.split("+", 1)[0].strip().lower()
    if not local_base or not domain:
        return False
    if domain in _PERSONAL_EMAIL_DOMAINS:
        return False
    part = local_base.split("@", 1)[0]
    if part in _GENERIC_LOCALPARTS:
        return False
    return True


def linkedin_ok(row: dict[str, Any]) -> bool:
    return bool(normalize_linkedin_url(row.get("linkedin_url")))


def geo_preferred(row: dict[str, Any]) -> bool:
    loc = _lower(row.get("location"))
    return any(g in loc for g in _GEO_SUBSTRINGS)


def apollo_email_status_ok(row: dict[str, Any]) -> bool:
    """
    If ICP_REQUIRE_APOLLO_EMAIL_STATUS is set, require non-empty email_status in allowlist.
    Missing column (legacy CSV) passes when requirement is off; when on, empty fails.
    """
    require = os.getenv("ICP_REQUIRE_APOLLO_EMAIL_STATUS", "").lower() in ("1", "true", "yes", "on")
    raw = str(row.get("email_status") or "").strip().lower()
    allowed = frozenset(
        x.strip().lower()
        for x in os.getenv("ICP_APOLLO_EMAIL_STATUSES", "verified,likely to engage").split(",")
        if x.strip()
    )
    if not require:
        if not raw:
            return True
        return raw in allowed
    if not raw:
        return False
    return raw in allowed


def outreach_ready_reject_reason(row: dict[str, Any], *, geo_strict: bool | None = None) -> str | None:
    """
    Return None if the row passes all gates; otherwise a short machine reason
    (for export script logging).
    """
    if geo_strict is None:
        geo_strict = os.getenv("ICP_GEO_STRICT", "").lower() in ("1", "true", "yes", "on")

    if not linkedin_ok(row):
        return "missing_or_invalid_linkedin"
    if not str(row.get("email") or "").strip():
        return "missing_email"
    if not email_localpart_domain_ok(str(row.get("email") or "")):
        return "generic_or_personal_email"
    if not apollo_email_status_ok(row):
        return "apollo_email_status"
    if not strict_founder_ceo_title(str(row.get("title") or "")):
        return "title_not_founder_ceo"
    if excluded_vertical(row):
        return "excluded_vertical_keyword"
    if agency_or_non_product_company(row):
        return "agency_or_consulting_name_pattern"
    if not industry_allowed(row):
        return "industry_not_allowed"
    if not has_web3_signal(row):
        return "missing_web3_signal"
    if _require_product_signal() and not has_product_company_signal(row):
        return "missing_product_signal"
    if not headcount_allowed(row):
        return "headcount_unknown_or_over_cap"
    if geo_strict and not geo_preferred(row):
        return "geo_not_in_allowlist"
    return None


def passes_outreach_ready(row: dict[str, Any], *, geo_strict: bool | None = None) -> bool:
    return outreach_ready_reject_reason(row, geo_strict=geo_strict) is None


def dedupe_key(row: dict[str, Any]) -> str:
    li = normalize_linkedin_url(row.get("linkedin_url"))
    if li:
        return f"li:{li}"
    em = str(row.get("email") or "").strip().lower()
    if em:
        return f"em:{em}"
    return ""


def dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable dedupe: LinkedIn URL primary, email secondary (same order as input)."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        k = dedupe_key(row)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(row)
    return out
