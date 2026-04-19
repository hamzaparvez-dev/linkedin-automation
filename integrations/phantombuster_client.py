"""
integrations/phantombuster_client.py
Step 3 — Enrich leads with LinkedIn data via Phantombuster.
Extracts: profile details, recent post activity, connection count.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Optional

import requests

from config import (
    PHANTOMBUSTER_AGENT_ID,
    PHANTOMBUSTER_API_KEY,
    PHANTOMBUSTER_BASE_URL,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 3

# Serialize launch+wait per agent id across all PhantombusterClient instances.
_agent_launch_locks: dict[str, threading.Lock] = {}
_agent_launch_locks_guard = threading.Lock()


def _shared_lock_for_agent(agent_id: str) -> threading.Lock:
    with _agent_launch_locks_guard:
        if agent_id not in _agent_launch_locks:
            _agent_launch_locks[agent_id] = threading.Lock()
        return _agent_launch_locks[agent_id]


class PhantombusterClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "X-Phantombuster-Key": PHANTOMBUSTER_API_KEY,
                "Content-Type": "application/json",
            }
        )

    def _launch_agent_unlocked(
        self,
        agent_id: str,
        argument: dict[str, Any],
        bonus_argument: Optional[dict[str, Any]],
    ) -> str:
        """POST /agents/launch (caller must hold per-agent lock when using run_agent)."""
        if not agent_id:
            raise ValueError("agent_id is required")
        if bonus_argument is not None:
            from core.phantom_payload import validate_phantom_bonus_argument

            ok_b, msg_b = validate_phantom_bonus_argument(bonus_argument)
            if not ok_b:
                raise ValueError(f"invalid_phantom_bonus_argument:{msg_b}")

        if isinstance(argument, dict) and (
            argument.get("profileUrls") is not None or argument.get("profileUrl") is not None
        ):
            from core.phantom_payload import log_engagement_argument_json, validate_engagement_argument

            ok, msg = validate_engagement_argument(argument, bonus_argument=bonus_argument)
            if not ok:
                raise ValueError(f"invalid_phantom_argument:{msg}")
            log_engagement_argument_json(argument, agent_id=agent_id)

        if isinstance(argument, dict) and argument.get("profileUrl") is not None:
            logger.info(
                "[DEBUG] final_argument_json: %s",
                json.dumps(argument, ensure_ascii=False, sort_keys=True),
            )

        url = f"{PHANTOMBUSTER_BASE_URL}/agents/launch"
        payload: dict[str, Any] = {"id": agent_id, "argument": argument}
        if bonus_argument is not None:
            from core.phantom_payload import redact_phantom_bonus_argument_for_log

            # bonusArgument overrides session/UA for this launch only (per Phantombuster); omitting it on a later
            # launch does not change the phantom's saved dashboard session.
            logger.info(
                "[phantom_bonus_argument] agent_id=%s bonus=%s",
                agent_id,
                json.dumps(redact_phantom_bonus_argument_for_log(bonus_argument), ensure_ascii=False, sort_keys=True),
            )
            payload["bonusArgument"] = json.dumps(bonus_argument)

        try:
            from core.phantom_payload import redact_phantom_argument_for_log, redact_phantom_bonus_argument_for_log

            log_payload: dict[str, Any] = {"id": agent_id, "argument": redact_phantom_argument_for_log(argument)}
            if bonus_argument is not None:
                log_payload["bonusArgument"] = json.dumps(
                    redact_phantom_bonus_argument_for_log(bonus_argument), ensure_ascii=False, sort_keys=True
                )
            logger.info(
                "[Phantombuster] launch API payload JSON: %s",
                json.dumps(log_payload, ensure_ascii=False),
            )
        except (TypeError, ValueError):
            logger.info("[Phantombuster] launch API payload (non-JSON-serializable)")
        last_err: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.post(url, json=payload, timeout=120)
                resp.raise_for_status()
                container_id = resp.json().get("containerId")
                logger.info("[Phantombuster] Agent %s launched. Container: %s", agent_id, container_id)
                return str(container_id or "")
            except Exception as e:
                last_err = e
                logger.warning("[Phantombuster] launch attempt %s/%s failed: %s", attempt, MAX_RETRIES, e)
                time.sleep(2**attempt)
        raise last_err or RuntimeError("launch failed")

    def launch_agent(
        self,
        agent_id: str,
        argument: dict[str, Any],
        bonus_argument: Optional[dict[str, Any]] = None,
    ) -> str:
        """Launch any phantom by id; argument must match the phantom's expected schema."""
        lock = _shared_lock_for_agent(agent_id)
        with lock:
            return self._launch_agent_unlocked(agent_id, argument, bonus_argument)

    # ── Launch agent for a batch of LinkedIn URLs ─────────────────────────────
    def launch_profile_scraper(self, linkedin_urls: list[str]) -> str:
        """
        Launch the LinkedIn Profile Scraper phantom.
        Returns the launch container ID for polling.
        """
        argument = {
            "spreadsheetUrl": "",
            "profileUrls": linkedin_urls,
            "numberOfLinesPerLaunch": len(linkedin_urls),
            "extractActivities": True,
            "activityDays": 30,
        }
        return self.launch_agent(PHANTOMBUSTER_AGENT_ID, argument)

    # ── Poll until agent finishes ──────────────────────────────────────────────
    def wait_for_completion(self, container_id: str, timeout_minutes: int = 30) -> dict:
        """Poll Phantombuster every 30s until the agent finishes or times out."""
        url = f"{PHANTOMBUSTER_BASE_URL}/containers/fetch-result-object"
        deadline = datetime.utcnow() + timedelta(minutes=timeout_minutes)

        while datetime.utcnow() < deadline:
            resp = self.session.get(url, params={"id": container_id})
            resp.raise_for_status()
            result = resp.json()
            status = result.get("status")

            if status == "finished":
                logger.info("[Phantombuster] Agent finished successfully.")
                return result
            if status == "error":
                logger.error("[Phantombuster] Agent errored: %s", result.get("message"))
                return result
            logger.debug("[Phantombuster] Status: %s — waiting...", status)
            time.sleep(30)

        logger.warning("[Phantombuster] Timed out after %s minutes.", timeout_minutes)
        return {}

    # ── Fetch the output CSV/JSON from completed agent ────────────────────────────
    def fetch_output(self, container_id: str) -> list[dict]:
        """Download and parse the agent's output data."""
        url = f"{PHANTOMBUSTER_BASE_URL}/containers/fetch-output"
        resp = self.session.get(url, params={"id": container_id})
        resp.raise_for_status()
        data = resp.json()

        # Phantombuster output is in resultObject as JSON array
        result_object = data.get("resultObject", "[]")
        if isinstance(result_object, str):
            try:
                return json.loads(result_object)
            except Exception:
                return []
        return result_object or []

    # ── High-level enrich function ─────────────────────────────────────────────
    def enrich_leads(self, leads: list[dict]) -> list[dict]:
        """
        Takes leads with linkedin_url, enriches them with:
        - connection_count
        - active_last_30_days (bool)
        - activity_level (string tag)
        """
        urls = [lead["linkedin_url"] for lead in leads if lead.get("linkedin_url")]
        logger.info("[Phantombuster] Enriching %s LinkedIn profiles...", len(urls))

        if not urls:
            logger.warning("[Phantombuster] No LinkedIn URLs found — skipping enrichment.")
            return leads

        # Process in batches of 50 (Phantombuster rate limits)
        batch_size = 50
        enrichment_map: dict[str, dict] = {}

        for i in range(0, len(urls), batch_size):
            batch = urls[i : i + batch_size]
            logger.info("[Phantombuster] Batch %s: %s URLs", i // batch_size + 1, len(batch))

            container_id = self.launch_profile_scraper(batch)
            result = self.wait_for_completion(container_id)

            if result.get("status") == "finished":
                output = self.fetch_output(container_id)
                for profile in output:
                    url = profile.get("linkedinUrl") or profile.get("profileUrl", "")
                    enrichment_map[url] = self._parse_profile(profile)
            else:
                logger.warning("[Phantombuster] Batch failed — skipping enrichment for this batch.")

            time.sleep(5)  # Brief pause between batches

        # Merge enrichment data back into leads
        enriched = []
        for lead in leads:
            li_url = lead.get("linkedin_url", "")
            extra = enrichment_map.get(li_url, {})
            lead.update(extra)
            enriched.append(lead)

        logger.info("[Phantombuster] Enrichment complete: %s profiles resolved.", len(enrichment_map))
        return enriched

    def run_agent(
        self,
        agent_id: str,
        argument: dict[str, Any],
        *,
        timeout_minutes: int = 45,
        bonus_argument: Optional[dict[str, Any]] = None,
    ) -> tuple[dict, str]:
        """Launch, wait, return (result_object, container_id). Serialized per agent_id."""
        lock = _shared_lock_for_agent(agent_id)
        with lock:
            cid = self._launch_agent_unlocked(agent_id, argument, bonus_argument)
            result = self.wait_for_completion(cid, timeout_minutes=timeout_minutes)
        return result, cid

    def is_agent_runtime_busy(self, agent_id: str) -> bool:
        """
        True if Phantombuster reports a non-finished container for this agent (or on HTTP/parse errors — fail-safe).

        Uses GET /orgs/fetch-running-containers when possible, then GET /agents/fetch as a heuristic fallback.
        """
        aid = str(agent_id or "").strip()
        if not aid:
            return True

        def _containers_from_payload(data: Any) -> list[dict[str, Any]]:
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
            if isinstance(data, dict):
                raw: list[Any] = []
                for key in ("containers", "runningContainers", "data", "running", "containerObject"):
                    v = data.get(key)
                    if isinstance(v, list):
                        raw = v
                        break
                if not raw:
                    v2 = data.get("containersObject")
                    if isinstance(v2, list):
                        raw = v2
                return [x for x in raw if isinstance(x, dict)]
            return []

        terminal = frozenset({"finished", "error", "aborted", "failed", "stopped", "killed", "not running", "not_running"})

        try:
            url = f"{PHANTOMBUSTER_BASE_URL}/orgs/fetch-running-containers"
            resp = self.session.get(url, timeout=45)
            resp.raise_for_status()
            items = _containers_from_payload(resp.json())
        except Exception as e:
            logger.warning(
                "[Phantombuster] is_agent_runtime_busy: fetch-running-containers failed agent_id=%s err=%s — treating as busy",
                aid,
                e,
            )
            return True

        for item in items:
            ag = str(item.get("agentId") or item.get("agent_id") or "").strip()
            if ag != aid:
                continue
            st = str(item.get("status") or item.get("state") or "").strip().lower()
            if st in terminal:
                continue
            # Endpoint is running containers; missing/unknown status still counts as busy.
            logger.info("[Phantombuster] is_agent_runtime_busy agent_id=%s (running-containers) status=%r", aid, st or None)
            return True

        try:
            url2 = f"{PHANTOMBUSTER_BASE_URL}/agents/fetch"
            r2 = self.session.get(url2, params={"id": aid}, timeout=45)
            r2.raise_for_status()
            agent = r2.json()
        except Exception as e:
            logger.warning(
                "[Phantombuster] is_agent_runtime_busy: agents/fetch fallback failed agent_id=%s err=%s — treating as busy",
                aid,
                e,
            )
            return True

        if not isinstance(agent, dict):
            return False

        for key in ("running", "isRunning", "isLaunching", "containerRunning", "hasRunningContainer"):
            if agent.get(key) is True:
                logger.info("[Phantombuster] is_agent_runtime_busy agent_id=%s (agents/fetch flag %s)", aid, key)
                return True

        for nest_key in ("runningContainer", "running_container", "latestContainer", "lastContainer"):
            rc = agent.get(nest_key)
            if isinstance(rc, dict):
                st = str(rc.get("status") or "").strip().lower()
                if st and st not in terminal:
                    logger.info("[Phantombuster] is_agent_runtime_busy agent_id=%s (%s.status=%r)", aid, nest_key, st)
                    return True
                if rc.get("id") and not st:
                    logger.info("[Phantombuster] is_agent_runtime_busy agent_id=%s (%s present, no terminal status)", aid, nest_key)
                    return True

        return False

    # ── Parse a single Phantombuster profile result ───────────────────────────
    def _parse_profile(self, profile: dict) -> dict:
        connections_raw = profile.get("connections", "0")
        try:
            connections = int(str(connections_raw).replace("+", "").replace(",", "").strip())
        except (ValueError, TypeError):
            connections = 0

        # Activity: Phantombuster returns a list of recent posts
        recent_posts = profile.get("recentPosts", []) or []
        active_30_days = len(recent_posts) > 0

        if len(recent_posts) >= 8:
            activity_level = "Very Active"
        elif len(recent_posts) >= 3:
            activity_level = "Active"
        elif len(recent_posts) >= 1:
            activity_level = "Occasionally Active"
        else:
            activity_level = "Not Active"

        return {
            "connection_count": connections,
            "active_last_30_days": active_30_days,
            "activity_level": activity_level,
            "recent_post_count": len(recent_posts),
            "profile_picture": profile.get("profilePicture", ""),
            "about": profile.get("description", "")[:300],  # Truncate bio
        }
