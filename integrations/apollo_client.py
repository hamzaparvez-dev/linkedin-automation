"""
integrations/apollo_client.py
Step 1 — Pull leads from Apollo.io using People API Search (mixed_people/api_search).

Legacy ``/v1/mixed_people/search`` is deprecated (422). This client uses
``POST {APOLLO_API_V1_BASE}/mixed_people/api_search`` with filters from
``APOLLO_SEARCH_FILTERS`` (+ optional ``APOLLO_SEARCH_FILTERS_JSON`` merge in config).

After each search page, ``people/bulk_match`` fills LinkedIn URLs and emails when Apollo
returns them (uses credits). Disable with ``APOLLO_EXTRACT_BULK_MATCH=false`` in ``.env``.
Default is **on** so SQLite rows are usable for Phantombuster and engagement.
"""

from __future__ import annotations

import csv
import logging
import os
import random
import time
from datetime import datetime
from typing import Any

import requests

from config import (
    APOLLO_API_KEY,
    APOLLO_API_V1_BASE,
    APOLLO_BULK_MATCH_SLEEP_S,
    APOLLO_SEARCH_FILTERS,
    ICP_INDUSTRY_KEYWORDS,
    RAW_LEADS_CSV,
)

logger = logging.getLogger(__name__)

APOLLO_API_SEARCH_PATH = "/mixed_people/api_search"

# Column order optimized for human lookup (spreadsheet / CRM).
MAIN_PIPELINE_CSV_COLUMNS: list[str] = [
    "id",
    "full_name",
    "first_name",
    "last_name",
    "linkedin_url",
    "email",
    "title",
    "company_name",
    "company_size",
    "industry",
    "location",
    "years_experience",
    "seniority",
    "extracted_at",
    "activity_level",
    "connection_count",
    "active_last_30_days",
    "score",
]
# Keys we never send to Apollo (local pagination / targets).
_NON_PAYLOAD_KEYS = frozenset({"total_leads_target"})


def _truthy_env(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


class ApolloClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "Cache-Control": "no-cache",
                "X-Api-Key": APOLLO_API_KEY,
            }
        )
        self._search_root = APOLLO_API_V1_BASE.rstrip("/")
        # Default on: api_search stubs rarely include linkedin_url / email; bulk_match uses credits.
        self._bulk_match = _truthy_env("APOLLO_EXTRACT_BULK_MATCH", "true")

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> requests.Response:
        url = f"{self._search_root}{path}"
        last_exc: Exception | None = None
        for attempt in range(1, 6):
            try:
                resp = self.session.request(method, url, json=json_body, timeout=60)
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

    def _build_search_payload(self, page: int) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, val in APOLLO_SEARCH_FILTERS.items():
            if key in _NON_PAYLOAD_KEYS or val is None:
                continue
            payload[key] = val

        tags = payload.get("q_organization_keyword_tags")
        if tags is None or (isinstance(tags, list) and len(tags) == 0):
            payload["q_organization_keyword_tags"] = [str(k).lower() for k in ICP_INDUSTRY_KEYWORDS]

        payload["page"] = page
        payload["per_page"] = min(100, max(1, int(payload.get("per_page") or 100)))
        return payload

    def search_people(self, page: int = 1) -> dict[str, Any]:
        """Single paginated People API Search (api_search) call."""
        payload = self._build_search_payload(page)
        resp = self._request_json("POST", APOLLO_API_SEARCH_PATH, json_body=payload)
        return resp.json()

    def _bulk_match_people(self, person_ids: list[str]) -> list[dict[str, Any]]:
        ids = [str(x).strip() for x in person_ids if str(x).strip()]
        if not ids:
            return []
        matches: list[dict[str, Any]] = []
        for i in range(0, len(ids), 10):
            chunk = ids[i : i + 10]
            resp = self._request_json(
                "POST",
                "/people/bulk_match",
                json_body={"details": [{"id": x} for x in chunk]},
            )
            data = resp.json()
            status = data.get("status")
            if status and str(status).lower() != "success":
                logger.warning("bulk_match non-success status=%s payload=%s", status, str(data)[:800])
            matches.extend(data.get("matches") or [])
            time.sleep(max(0.0, APOLLO_BULK_MATCH_SLEEP_S))
        return matches

    def extract_leads(self, target_count: int | None = None) -> list[dict]:
        """
        Paginate api_search until we hit ``target_count`` leads (capped by Apollo 50k display limit).
        """
        target = target_count or int(APOLLO_SEARCH_FILTERS.get("total_leads_target") or 1000)
        per_page = min(100, max(1, int(APOLLO_SEARCH_FILTERS.get("per_page") or 100)))
        max_pages = min(500, max(1, (target + per_page - 1) // per_page))

        logger.info(
            "[Apollo] Starting extraction — target=%s (api_search, bulk_match=%s)",
            target,
            self._bulk_match,
        )

        all_leads: list[dict] = []
        page = 1

        while len(all_leads) < target and page <= max_pages:
            try:
                data = self.search_people(page=page)
                people = data.get("people") or []

                if not people:
                    logger.info("[Apollo] No more results at page %s. Stopping.", page)
                    break

                profiles: list[dict[str, Any]] = list(people)
                if self._bulk_match:
                    ids = [str(p["id"]) for p in people if isinstance(p, dict) and p.get("id")]
                    if ids:
                        profiles = self._bulk_match_people(ids)
                        logger.info(
                            "[Apollo] Page %s: bulk_match enriched %s profiles",
                            page,
                            len(profiles),
                        )

                for p in profiles:
                    if not isinstance(p, dict):
                        continue
                    lead = self._parse_person(p)
                    all_leads.append(lead)
                    if len(all_leads) >= target:
                        break

                total_entries = int(data.get("total_entries") or 0)
                logger.info(
                    "[Apollo] Page %s: +%s leads | collected %s/%s (Apollo total_entries≈%s)",
                    page,
                    len(people),
                    len(all_leads),
                    target,
                    total_entries,
                )

                if total_entries and page * per_page >= total_entries:
                    logger.info("[Apollo] Exhausted search results at page %s.", page)
                    break

                page += 1
                time.sleep(1.0)

            except requests.HTTPError as e:
                logger.error("[Apollo] HTTP error on page %s: %s", page, e)
                if e.response is not None and e.response.status_code == 422:
                    logger.error("Check APOLLO_SEARCH_FILTERS / APOLLO_SEARCH_FILTERS_JSON — Apollo returned 422.")
                break
            except Exception as e:
                logger.error("[Apollo] Unexpected error: %s", e)
                break

        logger.info("[Apollo] Extraction complete: %s leads collected", len(all_leads))
        return all_leads

    def _parse_person(self, p: dict) -> dict:
        employment = p.get("employment_history", [])
        if not isinstance(employment, list):
            employment = []
        years_exp = self._calculate_years_experience(employment)
        org = p.get("organization", {}) or {}
        if not isinstance(org, dict):
            org = {}

        city = p.get("city") or ""
        country = p.get("country") or ""
        location = ", ".join([x for x in [str(city).strip(), str(country).strip()] if x])

        return {
            "id": p.get("id", ""),
            "full_name": p.get("name", "")
            or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip(),
            "first_name": p.get("first_name", ""),
            "last_name": p.get("last_name", ""),
            "linkedin_url": p.get("linkedin_url", ""),
            "email": p.get("email", ""),
            "title": p.get("title", ""),
            "company_name": org.get("name", ""),
            "company_size": org.get("estimated_num_employees", org.get("num_employees", "")),
            "industry": org.get("industry", ""),
            "location": location or (p.get("city", "") + ", " + p.get("country", "")),
            "years_experience": years_exp,
            "seniority": p.get("seniority", ""),
            "extracted_at": datetime.utcnow().isoformat(),
            "activity_level": "",
            "connection_count": "",
            "active_last_30_days": "",
            "score": 0,
        }

    def _calculate_years_experience(self, employment_history: list) -> int:
        if not employment_history:
            return 0

        total_months = 0
        for job in employment_history:
            start = job.get("start_date")
            end = job.get("end_date")
            if not start:
                continue
            try:
                start_dt = datetime.strptime(str(start)[:7], "%Y-%m")
                end_dt = (
                    datetime.strptime(str(end)[:7], "%Y-%m") if end else datetime.utcnow()
                )
                months = (end_dt.year - start_dt.year) * 12 + (end_dt.month - start_dt.month)
                total_months += max(0, months)
            except Exception:
                pass

        return total_months // 12

    def save_to_csv(self, leads: list[dict], filepath: str | None = None) -> str:
        path = filepath or RAW_LEADS_CSV
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

        if not leads:
            logger.warning("No leads to save.")
            return path

        fieldnames = list(MAIN_PIPELINE_CSV_COLUMNS)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(leads)

        logger.info("[Apollo] Saved %s leads → %s", len(leads), path)
        return path
