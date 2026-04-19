"""Stable lead_id and LinkedIn URL normalization."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse, urlunparse


def normalize_linkedin_url(url: str) -> str:
    if not url or not isinstance(url, str):
        return ""
    u = url.strip()
    if not u:
        return ""
    if not u.startswith("http"):
        u = "https://" + u
    parsed = urlparse(u)
    path = parsed.path or ""
    path = re.sub(r"/+$", "", path)
    netloc = (parsed.netloc or "").lower().replace("www.", "")
    return urlunparse(("https", netloc, path, "", "", ""))


def make_lead_id(apollo_person_id: str, linkedin_url: str) -> str:
    base = f"apollo:{apollo_person_id or 'unknown'}:{normalize_linkedin_url(linkedin_url)}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]
