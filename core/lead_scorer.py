"""
core/lead_scorer.py
Step 4 — Score each lead based on the defined criteria.

Scoring Rules:
  +2  Founder / CEO title
  +2  10+ years experience
  +2  Active on LinkedIn in last 30 days
  +1  500+ connections
  +2  Service / consulting business
  ─────────────────
  Max: 9 points
  Threshold to qualify: >= 6
"""

import csv
import os
import logging
from config import (
    ICP_INDUSTRY_KEYWORDS,
    SCORING_RULES,
    MIN_SCORE_THRESHOLD,
    SCORED_LEADS_CSV,
    QUALIFIED_LEADS_CSV,
)

logger = logging.getLogger(__name__)

FOUNDER_CEO_TITLES = {"founder", "co-founder", "ceo", "chief executive officer", "owner"}


class LeadScorer:

    def score_lead(self, lead: dict) -> dict:
        """Compute score for a single lead and attach breakdown."""
        score = 0
        breakdown = {}

        # Rule 1: Founder / CEO title (+2)
        title = (lead.get("title") or "").lower()
        is_founder_ceo = any(t in title for t in FOUNDER_CEO_TITLES)
        if is_founder_ceo:
            score += SCORING_RULES["founder_ceo_title"]
            breakdown["founder_ceo_title"] = SCORING_RULES["founder_ceo_title"]

        # Rule 2: 10+ years experience (+2)
        try:
            years_exp = int(lead.get("years_experience") or 0)
        except (ValueError, TypeError):
            years_exp = 0
        if years_exp >= 10:
            score += SCORING_RULES["ten_plus_years_exp"]
            breakdown["ten_plus_years_exp"] = SCORING_RULES["ten_plus_years_exp"]

        # Rule 3: Active on LinkedIn last 30 days (+2)
        active = lead.get("active_last_30_days")
        if active is True or str(active).lower() in ("true", "yes", "1"):
            score += SCORING_RULES["active_last_30_days"]
            breakdown["active_last_30_days"] = SCORING_RULES["active_last_30_days"]

        # Rule 4: 500+ connections (+1)
        try:
            connections = int(str(lead.get("connection_count") or "0").replace(",", "").replace("+", ""))
        except (ValueError, TypeError):
            connections = 0
        if connections >= 500:
            score += SCORING_RULES["500_plus_connections"]
            breakdown["500_plus_connections"] = SCORING_RULES["500_plus_connections"]

        # Rule 5: ICP match — industry / company keywords (PRD §5.3)
        industry = (lead.get("industry") or "").lower()
        company = (lead.get("company_name") or "").lower()
        title_l = (lead.get("title") or "").lower()
        kws = [str(k).lower() for k in ICP_INDUSTRY_KEYWORDS]
        is_icp = any(
            kw in industry or kw in company or kw in title_l
            for kw in kws
        )
        if is_icp:
            score += SCORING_RULES["icp_match"]
            breakdown["icp_match"] = SCORING_RULES["icp_match"]

        lead["score"] = score
        lead["score_breakdown"] = breakdown
        lead["qualified"] = score >= MIN_SCORE_THRESHOLD
        return lead

    def score_all(self, leads: list[dict]) -> list[dict]:
        """Score a list of leads and return sorted by score desc."""
        logger.info(f"[Scorer] Scoring {len(leads)} leads (threshold: {MIN_SCORE_THRESHOLD})...")
        scored = [self.score_lead(lead) for lead in leads]
        scored.sort(key=lambda x: x["score"], reverse=True)

        qualified = [l for l in scored if l["qualified"]]
        disqualified = [l for l in scored if not l["qualified"]]

        logger.info(f"[Scorer] Results: {len(qualified)} qualified | {len(disqualified)} disqualified")
        self._log_score_distribution(scored)
        return scored

    def filter_qualified(self, scored_leads: list[dict]) -> list[dict]:
        """Return only leads that meet the minimum score threshold."""
        return [l for l in scored_leads if l.get("qualified")]

    def save_scored(self, scored_leads: list[dict], filepath: str = None) -> str:
        path = filepath or SCORED_LEADS_CSV
        return self._save_csv(scored_leads, path)

    def save_qualified(self, qualified_leads: list[dict], filepath: str = None) -> str:
        path = filepath or QUALIFIED_LEADS_CSV
        return self._save_csv(qualified_leads, path)

    def _save_csv(self, leads: list[dict], path: str) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not leads:
            logger.warning(f"No leads to save to {path}")
            return path

        # Flatten score_breakdown dict for CSV
        flat_leads = []
        for lead in leads:
            flat = {k: v for k, v in lead.items() if k != "score_breakdown"}
            breakdown = lead.get("score_breakdown", {})
            for rule, pts in breakdown.items():
                flat[f"score_{rule}"] = pts
            flat_leads.append(flat)

        fieldnames = list(flat_leads[0].keys())
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(flat_leads)

        logger.info(f"[Scorer] Saved {len(flat_leads)} leads → {path}")
        return path

    def _log_score_distribution(self, scored: list[dict]):
        """Print score distribution for visibility."""
        from collections import Counter
        dist = Counter(l["score"] for l in scored)
        logger.info("[Scorer] Score distribution:")
        for score in sorted(dist.keys(), reverse=True):
            bar = "█" * dist[score]
            marker = " ← QUALIFIED" if score >= MIN_SCORE_THRESHOLD else ""
            logger.info(f"  Score {score}: {dist[score]:4d} leads  {bar}{marker}")
