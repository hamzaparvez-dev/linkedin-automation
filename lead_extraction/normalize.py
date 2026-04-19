"""Normalize lead fields and LinkedIn URLs for deduplication and CSV output."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_LINKEDIN_HOSTS = frozenset(
    {
        "linkedin.com",
        "www.linkedin.com",
        "nl.linkedin.com",
        "uk.linkedin.com",
    }
)


def normalize_linkedin_url(url: str | None) -> str:
    if not url or not isinstance(url, str):
        return ""
    u = url.strip()
    if not u:
        return ""
    if u.startswith("linkedin.com"):
        u = "https://www." + u
    if not u.startswith("http"):
        u = "https://" + u.lstrip("/")
    parsed = urlparse(u)
    host = (parsed.netloc or "").lower().split("@")[-1]
    if host.startswith("www."):
        host = host[4:]
    if host not in _LINKEDIN_HOSTS and "linkedin.com" not in host:
        # Still normalize path-only strings that look like /in/foo
        path = (parsed.path or u).strip().lower()
        path = re.sub(r"/+", "/", path).rstrip("/")
        if path.startswith("/in/") or path.startswith("/company/"):
            return f"https://www.linkedin.com{path}"
        return u.strip().lower()

    path = (parsed.path or "").strip().lower()
    path = re.sub(r"/+", "/", path).rstrip("/")
    # Drop tracking query params; keep meaningful ones rarely used on /in/
    query_pairs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() not in {"trk", "original_referer"}]
    new_query = urlencode(query_pairs) if query_pairs else ""
    rebuilt = urlunparse(("https", "www.linkedin.com", path, "", new_query, ""))
    return rebuilt.rstrip("?")


def _str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    return str(v).strip()


def normalize_lead_record(lead: dict[str, Any]) -> dict[str, Any]:
    out = dict(lead)
    for k in (
        "first_name",
        "last_name",
        "full_name",
        "title",
        "email",
        "email_status",
        "company_name",
        "industry",
        "location",
        "seniority",
        "organization_founded_year",
    ):
        if k in out:
            out[k] = _str(out.get(k))
    out["linkedin_url"] = normalize_linkedin_url(out.get("linkedin_url"))
    out["company_size"] = _str(out.get("company_size"))
    return out
