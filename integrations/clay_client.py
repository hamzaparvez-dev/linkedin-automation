"""
integrations/clay_client.py
Step 2 — Push raw leads to Clay for enrichment.
Clay enriches LinkedIn URL, tags activity, and can run waterfalls.
"""

import requests
import logging
import time
from config import CLAY_API_KEY, CLAY_TABLE_ID, CLAY_BASE_URL

logger = logging.getLogger(__name__)

CLAY_FIELD_MAP = {
    "full_name": "Name",
    "title": "Job Title",
    "email": "Email",
    "company_name": "Company",
    "industry": "Industry",
    "location": "Location",
    "linkedin_url": "LinkedIn URL",
    "years_experience": "Years Experience",
    "score": "Lead Score",
    "activity_level": "Activity Level",
    "connection_count": "Connection Count",
}


class ClayClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {CLAY_API_KEY}",
            "Content-Type": "application/json",
        })

    # ── Push a batch of leads to a Clay table ─────────────────────────────────
    def push_leads(self, leads: list[dict], batch_size: int = 50) -> dict:
        """
        Push leads to Clay table in batches.
        Clay will then automatically run its enrichment waterfalls.
        Returns summary: {pushed, failed}.
        """
        logger.info(f"[Clay] Pushing {len(leads)} leads to table: {CLAY_TABLE_ID}")
        pushed, failed = 0, 0

        for i in range(0, len(leads), batch_size):
            batch = leads[i:i + batch_size]
            rows = [self._format_row(lead) for lead in batch]

            try:
                url = f"{CLAY_BASE_URL}/tables/{CLAY_TABLE_ID}/rows/bulk"
                resp = self.session.post(url, json={"rows": rows})
                resp.raise_for_status()
                result = resp.json()
                pushed += len(rows)
                logger.info(f"[Clay] Batch {i // batch_size + 1}: {len(rows)} rows pushed. Response: {result.get('message', 'OK')}")
            except requests.HTTPError as e:
                logger.error(f"[Clay] HTTP error on batch {i // batch_size + 1}: {e}")
                failed += len(batch)
            except Exception as e:
                logger.error(f"[Clay] Unexpected error: {e}")
                failed += len(batch)

            time.sleep(0.5)  # Rate limit buffer

        summary = {"pushed": pushed, "failed": failed}
        logger.info(f"[Clay] Push complete: {summary}")
        return summary

    # ── Pull enriched data back from Clay ─────────────────────────────────────
    def pull_enriched(self, limit: int = 2000) -> list[dict]:
        """
        Fetch enriched rows back from Clay after waterfalls have run.
        In production, you'd poll or use Clay webhooks for completion.
        """
        logger.info(f"[Clay] Pulling enriched leads (limit={limit})...")
        url = f"{CLAY_BASE_URL}/tables/{CLAY_TABLE_ID}/rows"
        params = {"limit": limit, "status": "enriched"}

        try:
            resp = self.session.get(url, params=params)
            resp.raise_for_status()
            rows = resp.json().get("rows", [])
            logger.info(f"[Clay] Fetched {len(rows)} enriched rows.")
            return [self._parse_row(row) for row in rows]
        except Exception as e:
            logger.error(f"[Clay] Failed to pull enriched leads: {e}")
            return []

    # ── Format a lead dict for Clay API ───────────────────────────────────────
    def _format_row(self, lead: dict) -> dict:
        return {
            clay_field: lead.get(internal_key, "")
            for internal_key, clay_field in CLAY_FIELD_MAP.items()
        }

    # ── Parse Clay row back to internal format ─────────────────────────────────
    def _parse_row(self, row: dict) -> dict:
        data = row.get("data", {})
        reverse_map = {v: k for k, v in CLAY_FIELD_MAP.items()}
        return {
            reverse_map.get(field, field.lower()): value
            for field, value in data.items()
        }
