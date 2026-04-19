"""
config.py — Central configuration (PRD v2.0).
API keys via environment; multi-account via config/accounts.json.
"""

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = os.getenv("DATABASE_PATH", str(BASE_DIR / "data" / "leadgen.db"))
ACCOUNT_CONFIG_PATH = os.getenv(
    "ACCOUNT_CONFIG_PATH",
    str(BASE_DIR / "config" / "accounts.json"),
)

# ─── Apollo.io ────────────────────────────────────────────────────────────────
APOLLO_API_KEY = os.getenv("APOLLO_API_KEY", "YOUR_APOLLO_API_KEY")
APOLLO_BASE_URL = "https://api.apollo.io/v1"
# Current People Search + bulk enrichment (api_search returns partial rows).
APOLLO_API_V1_BASE = os.getenv("APOLLO_API_V1_BASE", "https://api.apollo.io/api/v1")

# ─── Clay (legacy, optional) ───────────────────────────────────────────────────
CLAY_API_KEY = os.getenv("CLAY_API_KEY", "")
CLAY_TABLE_ID = os.getenv("CLAY_TABLE_ID", "")
CLAY_BASE_URL = "https://api.clay.com/v1"

# ─── Phantombuster ────────────────────────────────────────────────────────────
PHANTOMBUSTER_API_KEY = os.getenv("PHANTOMBUSTER_API_KEY", "YOUR_PHANTOMBUSTER_API_KEY")
PHANTOMBUSTER_BASE_URL = "https://api.phantombuster.com/api/v2"
PHANTOMBUSTER_AGENT_ID = os.getenv("PHANTOMBUSTER_AGENT_ID", "YOUR_AGENT_ID")
PHANTOMBUSTER_CONNECT_AGENT_ID = os.getenv("PHANTOMBUSTER_CONNECT_AGENT_ID", "")
PHANTOMBUSTER_DM_AGENT_ID = os.getenv("PHANTOMBUSTER_DM_AGENT_ID", "")
# Engagement phantom argument shape: singular = profileUrl (string) + numberOfAddsPerLaunch;
# array = profileUrls (list) + numberOfLinesPerLaunch (legacy phantoms).
_pmode = os.getenv("PHANTOMBUSTER_ENGAGEMENT_PROFILE_MODE", "singular").strip().lower()
PHANTOMBUSTER_ENGAGEMENT_PROFILE_MODE = _pmode if _pmode in ("singular", "array") else "singular"
# LinkedIn session + browser context for LinkHelp-style connect/DM phantoms (API launches omit UI defaults).
_sc = os.getenv("PHANTOMBUSTER_SESSION_COOKIE", "").strip()
PHANTOMBUSTER_SESSION_COOKIE = _sc or os.getenv("LINKEDIN_SESSION_COOKIE", "").strip() or os.getenv(
    "LI_AT_COOKIE", ""
).strip()
PHANTOMBUSTER_USER_AGENT = os.getenv(
    "PHANTOMBUSTER_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
).strip()
PHANTOMBUSTER_INPUT_TYPE = os.getenv("PHANTOMBUSTER_INPUT_TYPE", "profileUrl").strip()
PHANTOMBUSTER_EMAIL_CHOOSER = os.getenv("PHANTOMBUSTER_EMAIL_CHOOSER", "none").strip()
PHANTOMBUSTER_ONLY_SECOND_CIRCLE = os.getenv("PHANTOMBUSTER_ONLY_SECOND_CIRCLE", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
PHANTOMBUSTER_DWELL_TIME = os.getenv("PHANTOMBUSTER_DWELL_TIME", "true").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# ─── Legacy outreach (off by default, PRD v2) ─────────────────────────────────
WAALAXY_API_KEY = os.getenv("WAALAXY_API_KEY", "")
EXPANDI_API_KEY = os.getenv("EXPANDI_API_KEY", "")
EXPANDI_BASE_URL = "https://api.expandi.io/v1"
OUTREACH_TOOL = os.getenv("OUTREACH_TOOL", "expandi")
ENABLE_LEGACY_OUTREACH = os.getenv("ENABLE_LEGACY_OUTREACH", "false").lower() in (
    "1",
    "true",
    "yes",
)

# ─── Behavior & safety (PRD §7, §15) ───────────────────────────────────────────
HARD_CAP_CONNECT = int(os.getenv("HARD_CAP_CONNECT", "40"))
HARD_CAP_DM = int(os.getenv("HARD_CAP_DM", "30"))
HARD_CAP_REPLY = int(os.getenv("HARD_CAP_REPLY", "20"))
BURST_WINDOW_MINUTES = int(os.getenv("BURST_WINDOW_MINUTES", "15"))
BURST_MAX_ACTIONS = int(os.getenv("BURST_MAX_ACTIONS", "8"))
DM_DELAY_MIN_HOURS = int(os.getenv("DM_DELAY_MIN_HOURS", "24"))
DM_DELAY_MAX_HOURS = int(os.getenv("DM_DELAY_MAX_HOURS", "72"))

# Timeline day offsets from connection (Day 1 = first DM). Gaps between stages are derived:
# FU1 after dm: followup_1 - dm; FU2 after FU1: followup_2 - followup_1; FU3 after FU2: followup_3 - followup_2.
_DEFAULT_FOLLOW_UP_SCHEDULE: dict[str, int] = {
    "dm": 1,
    "followup_1": 3,
    "followup_2": 6,
    "followup_3": 11,
}


def _load_follow_up_schedule() -> dict[str, int]:
    base = dict(_DEFAULT_FOLLOW_UP_SCHEDULE)
    raw = (os.getenv("FOLLOW_UP_SCHEDULE") or "").strip()
    if not raw:
        return base
    try:
        parsed: Any = json.loads(raw)
        if isinstance(parsed, dict):
            for k in _DEFAULT_FOLLOW_UP_SCHEDULE:
                if k not in parsed:
                    continue
                v = parsed[k]
                if type(v) is bool:
                    continue
                if isinstance(v, int):
                    base[k] = v
                elif isinstance(v, float):
                    base[k] = int(v)
                else:
                    base[k] = int(str(v).strip())
    except (ValueError, TypeError, json.JSONDecodeError):
        return dict(_DEFAULT_FOLLOW_UP_SCHEDULE)
    order = ("dm", "followup_1", "followup_2", "followup_3")
    prev = -1
    for k in order:
        if base[k] <= prev:
            return dict(_DEFAULT_FOLLOW_UP_SCHEDULE)
        prev = base[k]
    return base


FOLLOW_UP_SCHEDULE: dict[str, int] = _load_follow_up_schedule()


def follow_up_eligibility_gaps() -> tuple[int, int, int, int]:
    """
    Calendar-day minimums: (after connect for first DM, after first_dm for FU1,
    after followup_1 for FU2, after followup_2 for FU3).
    """
    s = FOLLOW_UP_SCHEDULE
    dm = int(s["dm"])
    f1 = int(s["followup_1"])
    f2 = int(s["followup_2"])
    f3 = int(s["followup_3"])
    return (dm, f1 - dm, f2 - f1, f3 - f2)


STRATEGY_ROLLING_DAYS = int(os.getenv("STRATEGY_ROLLING_DAYS", "14"))
STRATEGY_MAX_SHARE = float(os.getenv("STRATEGY_MAX_SHARE", "0.5"))

# ─── AI (optional, PRD §10) — OpenRouter-compatible chat completions API ───────
# Prefer OPEN_ROUTER_*; if OPEN_ROUTER_API_KEY is unset, OPENAI_API_KEY is still honored.
_or_key = os.getenv("OPEN_ROUTER_API_KEY", "").strip()
_oi_key = os.getenv("OPENAI_API_KEY", "").strip()
OPEN_ROUTER_API_KEY = _or_key or _oi_key

_or_model = os.getenv("OPEN_ROUTER_MODEL", "").strip()
_oi_model = os.getenv("OPENAI_MODEL", "").strip()
OPEN_ROUTER_MODEL = _or_model or _oi_model or "openai/gpt-4o-mini"

_or_base = os.getenv("OPEN_ROUTER_BASE_URL", "").strip()
_oi_base = os.getenv("OPENAI_BASE_URL", "").strip()
if _or_base:
    OPEN_ROUTER_BASE_URL = _or_base
elif _or_key:
    # OpenRouter key set: do not fall back to a stale OPENAI_BASE_URL from an old .env
    OPEN_ROUTER_BASE_URL = "https://openrouter.ai/api/v1"
elif _oi_base:
    OPEN_ROUTER_BASE_URL = _oi_base
elif _oi_key:
    OPEN_ROUTER_BASE_URL = "https://api.openai.com/v1"
else:
    OPEN_ROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MAX_DM_WORDS = int(os.getenv("MAX_DM_WORDS", "40"))
MAX_CONNECT_NOTE_WORDS = int(os.getenv("MAX_CONNECT_NOTE_WORDS", "25"))

# ─── Apollo Search Filters ────────────────────────────────────────────────────
INDUSTRY_KEYWORDS = ["Consulting", "Marketing", "SaaS", "Software", "Management Consulting"]
ICP_INDUSTRY_KEYWORDS = json.loads(
    os.getenv("ICP_INDUSTRY_KEYWORDS", json.dumps(INDUSTRY_KEYWORDS))
)

# Optional JSON override for Apollo People Search (merged over defaults below).
try:
    _APOLLO_SEARCH_EXTRA = json.loads(os.getenv("APOLLO_SEARCH_FILTERS_JSON", "{}") or "{}")
except json.JSONDecodeError:
    _APOLLO_SEARCH_EXTRA = {}


def _default_apollo_search_filters() -> dict:
    """People API Search (api_search) body fields + total_leads_target for pagination."""
    return {
        "person_titles": ["Founder", "CEO", "Co-Founder", "Managing Director"],
        "person_seniorities": ["owner", "founder", "c_suite"],
        "include_similar_titles": False,
        "person_locations": ["United States", "United Kingdom", "India"],
        "organization_num_employees_ranges": ["1,50"],
        "contact_email_status": ["verified", "likely to engage"],
        "organization_industry_tag_ids": [],
        # If empty list, ApolloClient expands from ICP_INDUSTRY_KEYWORDS (q_organization_keyword_tags).
        "q_organization_keyword_tags": [],
        "per_page": 100,
        "total_leads_target": 1000,
    }


_extra = _APOLLO_SEARCH_EXTRA if isinstance(_APOLLO_SEARCH_EXTRA, dict) else {}
APOLLO_SEARCH_FILTERS: dict = {**_default_apollo_search_filters(), **_extra}

# ─── Lead Scoring (PRD §5.3) ───────────────────────────────────────────────────
SCORING_RULES = {
    "founder_ceo_title": 2,
    "ten_plus_years_exp": 2,
    "active_last_30_days": 2,
    "500_plus_connections": 1,
    "icp_match": 2,
}
MIN_SCORE_THRESHOLD = int(os.getenv("MIN_SCORE_THRESHOLD", "6"))

# ─── Legacy CSV paths (exports + compatibility) ────────────────────────────────
RAW_LEADS_CSV = "output/1_raw_leads.csv"
ENRICHED_LEADS_CSV = "output/2_enriched_leads.csv"
SCORED_LEADS_CSV = "output/3_scored_leads.csv"
QUALIFIED_LEADS_CSV = "output/4_qualified_leads.csv"
OUTREACH_LOG_CSV = "output/5_outreach_log.csv"

# ─── Apollo Web3 ICP extraction (lead_extraction/) ───────────────────────────
APOLLO_WEB3_CSV_PATH = os.getenv("APOLLO_WEB3_CSV_PATH", str(BASE_DIR / "output" / "apollo_leads.csv"))
APOLLO_WEB3_MAX_PAGES = int(os.getenv("APOLLO_WEB3_MAX_PAGES", "250"))
APOLLO_WEB3_PER_PAGE = int(os.getenv("APOLLO_WEB3_PER_PAGE", "100"))
# loose: founder-title stub + Web3 hint in stub title/company (cheapest bulk_match)
# founder_only: founder-title stub only (recommended: Web3 often appears only after enrichment)
# off: enrich every stub (highest credits)
APOLLO_WEB3_STUB_PREFILTER = os.getenv("APOLLO_WEB3_STUB_PREFILTER", "founder_only").strip().lower()
APOLLO_BULK_MATCH_SLEEP_S = float(os.getenv("APOLLO_BULK_MATCH_SLEEP_S", "0.35"))
DAILY_CONNECTION_REQUESTS = int(os.getenv("DAILY_CONNECTION_REQUESTS", "60"))
PERSONALIZED_FIRST_LINE = True


def _truthy_env(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# ─── Production / engagement safety ───────────────────────────────────────────
# When true, engagement never launches Phantombuster (CLI --live overrides for that run).
DRY_RUN = _truthy_env("DRY_RUN", "false")
LOG_DIR = Path(os.getenv("LOG_DIR", str(BASE_DIR / "logs")))
# Max leads processed per account per connect (and per DM) pass when --max-leads is omitted.
ENGAGEMENT_MAX_LEADS_PER_ACCOUNT = int(os.getenv("ENGAGEMENT_MAX_LEADS_PER_ACCOUNT", "50"))
# If true and 2+ accounts share the same non-empty connect agent id, abort engagement (misconfiguration).
STRICT_DISTINCT_PHANTOM_CONNECT_AGENTS = _truthy_env("STRICT_DISTINCT_PHANTOM_CONNECT_AGENTS", "false")
# If true, a connect/DM phantom failure whose logs look like an expired LinkedIn session sets accounts_meta.paused=1
# for that account so the run stops hammering Phantombuster until you refresh cookies and run: python main.py --resume-account <id>
ENGAGEMENT_AUTO_PAUSE_ACCOUNT_ON_PB_AUTH_FAILURE = _truthy_env(
    "ENGAGEMENT_AUTO_PAUSE_ACCOUNT_ON_PB_AUTH_FAILURE", "true"
)


def resolve_engagement_dry_run(*, cli_dry: bool, cli_live: bool) -> bool:
    """CLI --live wins over env DRY_RUN; --dry-run forces dry."""
    if cli_live:
        return False
    if cli_dry:
        return True
    return DRY_RUN
