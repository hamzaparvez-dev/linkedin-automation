"""Apollo People Search (api_search) + bulk_match enrichment, with retries and logging."""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from typing import Any

import requests

from config import APOLLO_API_KEY, APOLLO_API_V1_BASE, APOLLO_BASE_URL, APOLLO_BULK_MATCH_SLEEP_S
from lead_extraction.icp_constants import (
    DEFAULT_APOLLO_PERSON_TITLES,
    DEFAULT_ORGANIZATION_NUM_EMPLOYEES_RANGES,
    DEFAULT_PERSON_LOCATIONS,
)

logger = logging.getLogger(__name__)

APOLLO_SEARCH_PATH = "/mixed_people/api_search"
APOLLO_MIXED_FILTERS_PATH = "/mixed_people_filters"


class ApolloWeb3Client:
    def __init__(
        self,
        *,
        person_titles: list[str] | None = None,
        person_locations: list[str] | None = None,
        organization_num_employees_ranges: list[str] | None = None,
        per_page: int = 100,
    ) -> None:
        self.person_titles = person_titles or list(DEFAULT_APOLLO_PERSON_TITLES)
        self.person_locations = person_locations or list(DEFAULT_PERSON_LOCATIONS)
        self.organization_num_employees_ranges = (
            organization_num_employees_ranges or list(DEFAULT_ORGANIZATION_NUM_EMPLOYEES_RANGES)
        )
        self.per_page = max(1, min(per_page, 100))
        self.session = self._build_session()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "Content-Type": "application/json",
                "Cache-Control": "no-cache",
                "X-Api-Key": APOLLO_API_KEY,
            }
        )
        return session

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        root = (base_url or APOLLO_API_V1_BASE).rstrip("/")
        url = f"{root}{path}"
        last_exc: Exception | None = None
        for attempt in range(1, 6):
            try:
                resp = self.session.request(method, url, timeout=60, **kwargs)
                if resp.status_code in (429, 500, 502, 503, 504):
                    sleep_s = min(60.0, (2 ** (attempt - 1)) * 0.5 + random.random() * 0.25)
                    logger.warning(
                        "Apollo %s %s returned %s (attempt %s/5). Backing off %.2fs",
                        method,
                        url,
                        resp.status_code,
                        attempt,
                        sleep_s,
                    )
                    time.sleep(sleep_s)
                    continue
                try:
                    resp.raise_for_status()
                except requests.HTTPError as e:
                    body = (resp.text or "").strip()
                    if body:
                        logger.error("Apollo error body (truncated): %s", body[:2000])
                    logger.error("Apollo %s %s failed: %s", method, url, e)
                    raise
                return resp
            except (requests.Timeout, requests.ConnectionError) as e:
                last_exc = e
                sleep_s = min(60.0, (2 ** (attempt - 1)) * 0.5 + random.random() * 0.25)
                logger.warning(
                    "Apollo transport error on %s %s: %s (attempt %s/5). Retrying in %.2fs",
                    method,
                    url,
                    e,
                    attempt,
                    sleep_s,
                )
                time.sleep(sleep_s)
        if last_exc:
            raise last_exc
        raise RuntimeError(f"Apollo request failed after retries: {method} {url}")

    def fetch_industry_tag_ids(self, keywords: tuple[str, ...]) -> list[str]:
        """Optional: map keywords to Apollo industry tag IDs (best-effort)."""
        for base in (APOLLO_BASE_URL, APOLLO_API_V1_BASE):
            try:
                root = base.rstrip("/")
                resp = self._request_json("GET", APOLLO_MIXED_FILTERS_PATH, base_url=root)
                data = resp.json()
                break
            except Exception as e:
                logger.info("Industry tag lookup skipped for base %s (%s)", base, e)
                data = None
        if not data:
            return []
        tag_ids: list[str] = []
        industries = data.get("organization_industry_tags") or []
        for tag in industries:
            name = str(tag.get("name") or "")
            if any(kw.lower() in name.lower() for kw in keywords):
                tid = tag.get("id")
                if tid is not None:
                    tag_ids.append(str(tid))
        logger.info("Resolved %d Apollo industry tag IDs (keyword subset)", len(tag_ids))
        return tag_ids

    def search_people_page(
        self,
        *,
        page: int,
        organization_industry_tag_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "person_titles": self.person_titles,
            "person_locations": self.person_locations,
            "organization_num_employees_ranges": self.organization_num_employees_ranges,
            "page": page,
            "per_page": self.per_page,
        }
        if organization_industry_tag_ids:
            payload["organization_industry_tag_ids"] = organization_industry_tag_ids

        resp = self._request_json("POST", APOLLO_SEARCH_PATH, json=payload, base_url=APOLLO_API_V1_BASE)
        return resp.json()

    def bulk_match_people(self, person_ids: list[str]) -> list[dict[str, Any]]:
        """Enrich up to 10 Apollo person IDs per request (consumes credits per Apollo billing)."""
        ids = [str(x).strip() for x in person_ids if str(x).strip()]
        if not ids:
            return []
        matches: list[dict[str, Any]] = []
        for i in range(0, len(ids), 10):
            chunk = ids[i : i + 10]
            resp = self._request_json(
                "POST",
                "/people/bulk_match",
                json={"details": [{"id": x} for x in chunk]},
                base_url=APOLLO_API_V1_BASE,
            )
            data = resp.json()
            status = data.get("status")
            if status and str(status).lower() != "success":
                logger.warning("bulk_match non-success status=%s payload=%s", status, str(data)[:800])
            matches.extend(data.get("matches") or [])
            time.sleep(max(0.0, APOLLO_BULK_MATCH_SLEEP_S))
        return matches

    @staticmethod
    def _years_experience(employment_history: list[dict[str, Any]] | None) -> int:
        if not employment_history:
            return 0
        total_months = 0
        for job in employment_history:
            start = job.get("start_date")
            if not start:
                continue
            try:
                start_dt = datetime.strptime(str(start)[:7], "%Y-%m")
                end_raw = job.get("end_date")
                end_dt = (
                    datetime.strptime(str(end_raw)[:7], "%Y-%m")
                    if end_raw
                    else datetime.now(timezone.utc).replace(tzinfo=None)
                )
                months = (end_dt.year - start_dt.year) * 12 + (end_dt.month - start_dt.month)
                total_months += max(0, months)
            except Exception:
                continue
        return total_months // 12

    def parse_person(self, p: dict[str, Any]) -> dict[str, Any]:
        org = p.get("organization") if isinstance(p.get("organization"), dict) else {}
        city = p.get("city") or ""
        country = p.get("country") or ""
        location = ", ".join([x for x in [str(city).strip(), str(country).strip()] if x])

        headcount = org.get("estimated_num_employees")
        if headcount is None or headcount == "":
            headcount = org.get("num_employees")

        keywords = org.get("keywords")
        if isinstance(keywords, list):
            org_keywords = ", ".join(str(x) for x in keywords if x is not None)
        else:
            org_keywords = str(keywords or "")

        employment = p.get("employment_history") if isinstance(p.get("employment_history"), list) else []

        founded_year = org.get("founded_year")
        try:
            founded_year_int = int(founded_year) if founded_year is not None and str(founded_year).strip() != "" else None
        except (TypeError, ValueError):
            founded_year_int = None

        email_status_raw = p.get("email_status") or p.get("contact_email_status") or ""
        if not email_status_raw and isinstance(org, dict):
            pd = org.get("primary_domain")
            if isinstance(pd, dict):
                email_status_raw = pd.get("email_domain_status") or ""
        email_status = str(email_status_raw or "").strip()

        return {
            "id": str(p.get("id") or ""),
            "first_name": str(p.get("first_name") or ""),
            "last_name": str(p.get("last_name") or ""),
            "full_name": str(p.get("name") or "").strip()
            or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip(),
            "title": str(p.get("title") or ""),
            "email": str(p.get("email") or ""),
            "email_status": email_status,
            "linkedin_url": str(p.get("linkedin_url") or ""),
            "company_name": str(org.get("name") or ""),
            "company_size": headcount if headcount is not None else "",
            "industry": str(org.get("industry") or ""),
            "location": location,
            "years_experience": self._years_experience(employment),
            "seniority": str(p.get("seniority") or ""),
            "organization_keywords": org_keywords,
            "organization_short_description": str(org.get("short_description") or ""),
            "organization_seo_description": str(org.get("seo_description") or ""),
            "organization_founded_year": founded_year_int if founded_year_int is not None else "",
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "score": 0,
            "score_breakdown_json": "",
            "web3_keyword_hits": "",
            "_organization_raw": org,
        }
