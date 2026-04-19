"""Orchestrate Apollo fetch → post-filter → score → dedupe → normalize → CSV."""

from __future__ import annotations

import csv
import json
import logging
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any

import requests

from config import (
    APOLLO_API_KEY,
    APOLLO_WEB3_CSV_PATH,
    APOLLO_WEB3_MAX_PAGES,
    APOLLO_WEB3_PER_PAGE,
    APOLLO_WEB3_STUB_PREFILTER,
    BASE_DIR,
)
from lead_extraction.apollo_web3_client import ApolloWeb3Client
from lead_extraction.normalize import normalize_lead_record, normalize_linkedin_url
from lead_extraction.outreach_ready_filter import outreach_ready_reject_reason
from lead_extraction.post_filters import passes_strict_icp, stub_should_enrich
from lead_extraction.scorer import attach_score

logger = logging.getLogger(__name__)


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def _optional_json_string_list(env_name: str) -> list[str] | None:
    raw = os.getenv(env_name)
    if not raw or not str(raw).strip():
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Ignoring invalid JSON for %s", env_name)
        return None
    if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
        return parsed
    logger.warning("Ignoring %s (expected a JSON array of strings)", env_name)
    return None


CSV_FIELDNAMES: list[str] = [
    "id",
    "full_name",
    "linkedin_url",
    "email",
    "email_status",
    "title",
    "company_name",
    "company_size",
    "industry",
    "location",
    "first_name",
    "last_name",
    "years_experience",
    "seniority",
    "organization_keywords",
    "organization_short_description",
    "organization_founded_year",
    "score",
    "score_breakdown_json",
    "web3_keyword_hits",
    "extracted_at",
]


def _strip_internal_fields(lead: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in lead.items() if not k.startswith("_")}


def _row_dedupe_key(row: dict[str, Any]) -> str:
    li = normalize_linkedin_url(row.get("linkedin_url"))
    if li:
        return li
    rid = str(row.get("id") or "").strip()
    return f"id:{rid}" if rid else ""


def _dedupe_keys_from_lookup_merged(path: Path) -> set[str]:
    """Keys already present in dashboard merge file (all sources)."""
    if not path.is_file():
        return set()
    keys: set[str] = set()
    try:
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                k = _row_dedupe_key(row)
                if k:
                    keys.add(k)
    except OSError as e:
        logger.warning("Could not read %s for dedupe seed: %s", path, e)
    return keys


def _load_existing_apollo_csv(path: str) -> list[dict[str, Any]]:
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except OSError as e:
        logger.warning("Could not read existing Apollo CSV %s: %s", path, e)
        return []


def _dedupe_by_linkedin(leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for lead in leads:
        key = _row_dedupe_key(lead)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(lead)
    return out


def _merge_apollo_rows(
    existing: list[dict[str, Any]], fresh: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Union by LinkedIn (or Apollo id); newer run wins on same key."""
    by_key: dict[str, dict[str, Any]] = {}
    for row in existing:
        k = _row_dedupe_key(row)
        if k:
            by_key[k] = row
    for row in fresh:
        k = _row_dedupe_key(row)
        if k:
            by_key[k] = row
    return list(by_key.values())


def _write_csv(leads: list[dict[str, Any]], path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if not leads:
        logger.warning("No leads to write to %s", path)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
            w.writeheader()
        return

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        for row in leads:
            w.writerow({k: row.get(k, "") for k in CSV_FIELDNAMES})


def run_apollo_web3_extraction(
    *,
    target: int,
    output_path: str | None = None,
    client: ApolloWeb3Client | None = None,
) -> str:
    """
    Fetch from Apollo until `target` ICP-qualified leads are collected (best-effort),
    or Apollo pages are exhausted / capped.
    """
    if not APOLLO_API_KEY or APOLLO_API_KEY == "YOUR_APOLLO_API_KEY":
        raise RuntimeError("APOLLO_API_KEY is missing or placeholder. Set it in your environment / .env file.")

    out = output_path or APOLLO_WEB3_CSV_PATH
    if client is None:
        client = ApolloWeb3Client(
            person_titles=_optional_json_string_list("APOLLO_WEB3_PERSON_TITLES"),
            person_locations=_optional_json_string_list("APOLLO_WEB3_PERSON_LOCATIONS"),
            organization_num_employees_ranges=_optional_json_string_list("APOLLO_WEB3_ORG_EMPLOYEE_RANGES"),
            per_page=APOLLO_WEB3_PER_PAGE,
        )
    apollo = client

    merged_lookup = BASE_DIR / "output" / "leads_lookup_merged.csv"
    existing_rows = _load_existing_apollo_csv(out)
    seen_linkedin: set[str] = set()
    seen_linkedin |= _dedupe_keys_from_lookup_merged(merged_lookup)
    for row in existing_rows:
        k = _row_dedupe_key(row)
        if k:
            seen_linkedin.add(k)

    qualified: list[dict[str, Any]] = []
    page = 1
    strict_outreach = _truthy_env("APOLLO_WEB3_STRICT_OUTREACH")
    outreach_reject_counts: Counter[str] = Counter()

    logger.info(
        "Web3 Apollo extraction started (target=%s, per_page=%s, max_pages=%s, strict_outreach=%s)",
        target,
        APOLLO_WEB3_PER_PAGE,
        APOLLO_WEB3_MAX_PAGES,
        strict_outreach,
    )

    while len(qualified) < target and page <= APOLLO_WEB3_MAX_PAGES:
        try:
            data = apollo.search_people_page(page=page, organization_industry_tag_ids=None)
        except requests.RequestException as e:
            logger.error("Apollo search failed on page %s: %s", page, e)
            break

        people = data.get("people") or []
        if not people:
            logger.info("Apollo returned no people at page %s; stopping.", page)
            break

        queued_ids = [
            str(p["id"])
            for p in people
            if isinstance(p, dict) and p.get("id") and stub_should_enrich(p, APOLLO_WEB3_STUB_PREFILTER)
        ]
        logger.info(
            "Apollo page %s: fetched %s stubs, %s queued for bulk_match (stub_prefilter=%s)",
            page,
            len(people),
            len(queued_ids),
            APOLLO_WEB3_STUB_PREFILTER,
        )

        try:
            full_profiles = apollo.bulk_match_people(queued_ids)
        except requests.RequestException as e:
            logger.error("Apollo bulk_match failed on page %s: %s", page, e)
            break

        logger.info("Apollo page %s: bulk_match returned %s profiles", page, len(full_profiles))

        for person in full_profiles:
            if not isinstance(person, dict):
                continue
            raw_lead = apollo.parse_person(person)
            if not passes_strict_icp(raw_lead):
                continue
            if strict_outreach:
                rr = outreach_ready_reject_reason(raw_lead)
                if rr:
                    outreach_reject_counts[rr] += 1
                    continue
            scored = attach_score(raw_lead)
            normalized = normalize_lead_record(scored)
            dedupe_key = _row_dedupe_key(normalized)
            if not dedupe_key:
                continue

            if dedupe_key in seen_linkedin:
                continue
            seen_linkedin.add(dedupe_key)

            qualified.append(_strip_internal_fields(normalized))
            if len(qualified) >= target:
                break

        total_entries = int(data.get("total_entries") or 0)
        if total_entries and page * apollo.per_page >= total_entries:
            logger.info("Exhausted Apollo api_search results (page=%s total_entries=%s).", page, total_entries)
            break

        page += 1
        time.sleep(1.0)

    qualified.sort(key=lambda r: int(r.get("score") or 0), reverse=True)
    qualified = _dedupe_by_linkedin(qualified)
    combined = _merge_apollo_rows(existing_rows, qualified)
    combined.sort(key=lambda r: int(r.get("score") or 0), reverse=True)
    combined = _dedupe_by_linkedin(combined)

    if strict_outreach and outreach_reject_counts:
        logger.info(
            "Strict outreach rejections (rows that passed stub ICP but failed outreach_ready): %s",
            dict(outreach_reject_counts.most_common()),
        )

    _write_csv(combined, out)
    logger.info(
        "Wrote %s qualified Web3 leads → %s (+%s new this run, %s loaded from existing CSV)",
        len(combined),
        out,
        len(qualified),
        len(existing_rows),
    )
    return out
