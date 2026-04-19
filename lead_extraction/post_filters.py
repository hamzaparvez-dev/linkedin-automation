"""Strict post-filtering after Apollo fetch (Web3, headcount, founder title)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

from lead_extraction.company_size import parse_employee_upper_bound
from lead_extraction.icp_constants import (
    FOUNDER_TITLE_SUBSTRINGS,
    PRIMARY_EMPLOYEE_MAX,
    SECONDARY_EMPLOYEE_MAX,
    WEB3_KEYWORDS,
)


def _haystack(lead: dict[str, Any]) -> str:
    parts = [
        lead.get("title") or "",
        lead.get("company_name") or "",
        lead.get("industry") or "",
        lead.get("organization_keywords") or "",
        lead.get("organization_short_description") or "",
        lead.get("organization_seo_description") or "",
    ]
    return " ".join(parts).lower()


def matches_web3_keywords(lead: dict[str, Any]) -> bool:
    text = _haystack(lead)
    return any(kw in text for kw in WEB3_KEYWORDS)


def is_founder_level_title(lead: dict[str, Any]) -> bool:
    title = (lead.get("title") or "").lower()
    return any(fragment in title for fragment in FOUNDER_TITLE_SUBSTRINGS)


def is_small_company_icp(lead: dict[str, Any]) -> bool:
    org = lead.get("_organization_raw") if isinstance(lead.get("_organization_raw"), dict) else None
    upper = parse_employee_upper_bound(lead.get("company_size"), org)
    if upper is None:
        # Without headcount, do not pass strict ICP (avoids polluting CSV).
        return False
    return upper <= SECONDARY_EMPLOYEE_MAX


def is_primary_headcount(lead: dict[str, Any]) -> bool:
    org = lead.get("_organization_raw") if isinstance(lead.get("_organization_raw"), dict) else None
    upper = parse_employee_upper_bound(lead.get("company_size"), org)
    return upper is not None and upper <= PRIMARY_EMPLOYEE_MAX


def passes_strict_icp(lead: dict[str, Any]) -> bool:
    return (
        matches_web3_keywords(lead)
        and is_founder_level_title(lead)
        and is_small_company_icp(lead)
    )


def stub_should_enrich(stub: dict[str, Any], mode: str) -> bool:
    """
    api_search returns partial profiles; bulk_match costs credits.
    Use a cheap stub gate before enrichment.
    """
    m = (mode or "loose").strip().lower()
    pid = stub.get("id")
    if not pid:
        return False
    if m == "off":
        return True

    title = str(stub.get("title") or "")
    org = stub.get("organization") if isinstance(stub.get("organization"), dict) else {}
    company = str(org.get("name") or "")
    stub_lead: dict[str, Any] = {
        "title": title,
        "company_name": company,
        "industry": "",
        "organization_keywords": "",
        "organization_short_description": "",
        "organization_seo_description": "",
    }
    if not is_founder_level_title(stub_lead):
        return False
    if m == "founder_only":
        return True
    if m == "loose":
        return matches_web3_keywords(stub_lead)
    logger.warning("Unknown APOLLO_WEB3_STUB_PREFILTER=%r; defaulting to loose", mode)
    return matches_web3_keywords(stub_lead)
