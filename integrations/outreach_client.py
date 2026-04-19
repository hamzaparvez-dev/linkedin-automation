"""
integrations/outreach_client.py
Step 5 — Push qualified leads to LinkedIn outreach tool.
Supports: Expandi (API) and Waalaxy (API).
Sends personalized connection requests: 50–80/day.
"""

import requests
import time
import csv
import logging
import os
from datetime import datetime, date
from config import (
    EXPANDI_API_KEY, EXPANDI_BASE_URL,
    WAALAXY_API_KEY,
    DAILY_CONNECTION_REQUESTS, PERSONALIZED_FIRST_LINE,
    OUTREACH_TOOL, OUTREACH_LOG_CSV
)

logger = logging.getLogger(__name__)


# ─── Personalised message generator ─────────────────────────────────────────
def generate_first_line(lead: dict) -> str:
    """
    Generate a dynamic, personalised first line for the connection request.
    Uses available lead signals: title, industry, activity, company.
    """
    name = lead.get("first_name") or lead.get("full_name", "there").split()[0]
    title = lead.get("title", "")
    company = lead.get("company_name", "")
    industry = lead.get("industry", "")
    activity = lead.get("activity_level", "")

    # Pick the most specific template based on available data
    if "active" in activity.lower() and industry:
        return (
            f"Hi {name}, noticed your recent posts on {industry} — "
            f"great perspective from someone running {company}."
        )
    elif title and company:
        return (
            f"Hi {name}, I came across your profile and was impressed by your work "
            f"as {title} at {company}."
        )
    elif industry:
        return (
            f"Hi {name}, your background in {industry} really caught my attention — "
            f"would love to connect."
        )
    else:
        return f"Hi {name}, I'd love to connect and exchange ideas — your profile stood out!"


# ─── Expandi Client ──────────────────────────────────────────────────────────
class ExpandiClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {EXPANDI_API_KEY}",
            "Content-Type": "application/json",
        })

    def add_to_campaign(self, campaign_id: str, leads: list[dict]) -> dict:
        url = f"{EXPANDI_BASE_URL}/campaigns/{campaign_id}/leads"
        payload = {
            "leads": [
                {
                    "linkedin_url": lead["linkedin_url"],
                    "first_name": lead.get("first_name", ""),
                    "last_name": lead.get("last_name", ""),
                    "message": generate_first_line(lead) if PERSONALIZED_FIRST_LINE else "",
                }
                for lead in leads
                if lead.get("linkedin_url")
            ]
        }
        resp = self.session.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()


# ─── Waalaxy Client ──────────────────────────────────────────────────────────
class WaalaxyClient:
    BASE_URL = "https://api.waalaxy.com/v1"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {WAALAXY_API_KEY}",
            "Content-Type": "application/json",
        })

    def import_leads(self, campaign_id: str, leads: list[dict]) -> dict:
        url = f"{self.BASE_URL}/campaigns/{campaign_id}/prospects"
        payload = {
            "prospects": [
                {
                    "linkedin_profile_url": lead["linkedin_url"],
                    "first_name": lead.get("first_name", ""),
                    "last_name": lead.get("last_name", ""),
                    "custom_intro": generate_first_line(lead) if PERSONALIZED_FIRST_LINE else "",
                }
                for lead in leads
                if lead.get("linkedin_url")
            ]
        }
        resp = self.session.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()


# ─── Main Outreach Dispatcher ────────────────────────────────────────────────
class OutreachDispatcher:
    """
    Dispatches daily batches of qualified leads to the chosen outreach tool.
    Respects the DAILY_CONNECTION_REQUESTS limit (50–80/day).
    """

    def __init__(self, campaign_id: str):
        self.campaign_id = campaign_id
        self.tool = OUTREACH_TOOL.lower()
        self.daily_limit = DAILY_CONNECTION_REQUESTS

        if self.tool == "expandi":
            self.client = ExpandiClient()
        elif self.tool == "waalaxy":
            self.client = WaalaxyClient()
        else:
            raise ValueError(f"Unknown outreach tool: {self.tool}. Use 'expandi' or 'waalaxy'.")

        logger.info(f"[Outreach] Using tool: {self.tool} | Daily limit: {self.daily_limit}")

    def dispatch(self, qualified_leads: list[dict]) -> dict:
        """
        Send today's batch of leads. Takes only DAILY_LIMIT leads.
        Returns dispatch summary.
        """
        batch = qualified_leads[:self.daily_limit]
        logger.info(f"[Outreach] Dispatching {len(batch)} leads today (from {len(qualified_leads)} qualified)")

        try:
            if self.tool == "expandi":
                result = self.client.add_to_campaign(self.campaign_id, batch)
            else:
                result = self.client.import_leads(self.campaign_id, batch)

            logger.info(f"[Outreach] Success: {result}")
            self._log_dispatched(batch, status="sent")
            return {"sent": len(batch), "status": "success", "response": result}

        except requests.HTTPError as e:
            logger.error(f"[Outreach] HTTP error: {e}")
            self._log_dispatched(batch, status="error")
            return {"sent": 0, "status": "error", "message": str(e)}
        except Exception as e:
            logger.error(f"[Outreach] Unexpected error: {e}")
            return {"sent": 0, "status": "error", "message": str(e)}

    # ── Log outreach activity ─────────────────────────────────────────────────
    def _log_dispatched(self, leads: list[dict], status: str):
        os.makedirs(os.path.dirname(OUTREACH_LOG_CSV), exist_ok=True)
        file_exists = os.path.exists(OUTREACH_LOG_CSV)

        with open(OUTREACH_LOG_CSV, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["date", "full_name", "linkedin_url", "score",
                                  "message_sent", "tool", "status"])
            for lead in leads:
                writer.writerow([
                    date.today().isoformat(),
                    lead.get("full_name", ""),
                    lead.get("linkedin_url", ""),
                    lead.get("score", ""),
                    generate_first_line(lead) if PERSONALIZED_FIRST_LINE else "",
                    self.tool,
                    status,
                ])
        logger.info(f"[Outreach] Logged {len(leads)} dispatched leads → {OUTREACH_LOG_CSV}")
