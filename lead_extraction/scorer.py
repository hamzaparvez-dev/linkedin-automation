"""ICP-oriented scoring for Web3 founders (higher = stronger match)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from lead_extraction.post_filters import is_primary_headcount
from lead_extraction.icp_constants import WEB3_KEYWORDS


def _matched_web3_keywords(lead: dict[str, Any]) -> list[str]:
    text = " ".join(
        [
            str(lead.get("title") or ""),
            str(lead.get("company_name") or ""),
            str(lead.get("industry") or ""),
            str(lead.get("organization_keywords") or ""),
            str(lead.get("organization_short_description") or ""),
        ]
    ).lower()
    return [kw for kw in WEB3_KEYWORDS if kw in text]


def _company_age_bonus(lead: dict[str, Any]) -> int:
    """Approximate 0–5y company age proxy when Apollo provides founded_year."""
    raw = lead.get("organization_founded_year")
    if raw is None or raw == "":
        return 0
    try:
        y = int(raw)
    except (TypeError, ValueError):
        return 0

    age = max(0, datetime.now().year - y)
    if age <= 5:
        return 8
    if age <= 8:
        return 3
    return 0


def score_lead(lead: dict[str, Any]) -> tuple[int, dict[str, int]]:
    """
    Weighted score with explicit breakdown keys (sums to total).
    Designed so strong Web3 + tight headcount + clear founder title bubble up.
    """
    breakdown: dict[str, int] = {}
    title = (lead.get("title") or "").lower()

    # Founder / seniority signal
    if "co-founder" in title or "cofounder" in title or "co founder" in title:
        breakdown["co_founder"] = 25
    elif "founder" in title and "co" not in title:
        breakdown["founder"] = 22
    elif "chief executive" in title or title.strip() == "ceo" or " ceo" in f" {title}":
        breakdown["ceo"] = 20
    elif "president" in title:
        breakdown["president"] = 15
    elif "managing director" in title:
        breakdown["md"] = 14
    else:
        breakdown["other_founderish"] = 10

    # Company size (primary band is best ICP)
    if is_primary_headcount(lead):
        breakdown["headcount_primary"] = 20
    else:
        breakdown["headcount_secondary"] = 12

    # Web3 keyword strength
    hits = _matched_web3_keywords(lead)
    breakdown["web3_hit_count"] = min(30, len(hits) * 6)

    fy = _company_age_bonus(lead)
    if fy:
        breakdown["company_age_proxy"] = fy

    # Title-level Web3 terms (strong intent)
    title_l = (lead.get("title") or "").lower()
    if any(k in title_l for k in ("web3", "crypto", "blockchain", "defi", "nft", "dao", "protocol")):
        breakdown["web3_in_title"] = 15

    total = sum(breakdown.values())
    return total, breakdown


def attach_score(lead: dict[str, Any]) -> dict[str, Any]:
    total, breakdown = score_lead(lead)
    out = dict(lead)
    out["score"] = total
    out["score_breakdown_json"] = json.dumps(breakdown, separators=(",", ":"))
    out["web3_keyword_hits"] = ";".join(_matched_web3_keywords(lead))
    return out
