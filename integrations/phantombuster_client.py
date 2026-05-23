"""
integrations/phantombuster_client.py
Phantombuster API client for Auto-Connect and Auto-DM: launch, poll, optional fetch-output.
(LinkedIn profile-scraper batch enrichment was removed; engagement uses per-account agent ids from accounts.json / env.)
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
    PHANTOMBUSTER_API_KEY,
    PHANTOMBUSTER_BASE_URL,
    PHANTOMBUSTER_LOG_POLLING_DEBUG,
    PHANTOMBUSTER_SERIALIZE_AGENT_LAUNCHES,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
# Max chars of response body captured for logs / SQLite.
_MAX_PB_ERROR_BODY = 2000
_MAX_FORMAT_MSG = _MAX_PB_ERROR_BODY + 200


def _truncate_pb_body(text: str, max_len: int = _MAX_PB_ERROR_BODY) -> str:
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 3] + "..."


class PhantombusterHttpError(RuntimeError):
    """Phantombuster REST returned a non-success status or an unusable success payload."""

    def __init__(
        self,
        *,
        status_code: int,
        body_snippet: str,
        context: str = "",
        url: str = "",
    ) -> None:
        self.status_code = status_code
        self.body_snippet = _truncate_pb_body(body_snippet)
        self.context = (context or "").strip()
        self.url = (url or "").strip()
        head = f"{self.context} " if self.context else ""
        tail = f": {self.body_snippet}" if self.body_snippet else (f" ({self.url})" if self.url else "")
        full = f"{head}HTTP {status_code}{tail}"
        super().__init__(full[:_MAX_FORMAT_MSG])

    @classmethod
    def from_response(cls, resp: requests.Response, *, context: str = "") -> "PhantombusterHttpError":
        try:
            body = resp.text or ""
        except Exception:
            body = ""
        return cls(
            status_code=int(resp.status_code),
            body_snippet=body,
            context=context,
            url=str(getattr(resp, "url", "") or ""),
        )


def format_phantom_api_error(exc: BaseException) -> str:
    """String for action_log / UI when a Phantombuster HTTP call or parse step fails."""
    if isinstance(exc, PhantombusterHttpError):
        return str(exc)[:_MAX_FORMAT_MSG]
    if isinstance(exc, requests.HTTPError):
        r = exc.response
        if r is not None:
            return str(PhantombusterHttpError.from_response(r, context="HTTPError"))[:_MAX_FORMAT_MSG]
    return f"exception:{type(exc).__name__}:{exc!s}"[:900]


def synthetic_polling_timeout_result(container_id: str) -> dict[str, Any]:
    """Result-shaped dict when /containers/fetch-result-object never returns finished/error before the deadline."""
    return {
        "status": "timeout",
        "_synthetic": True,
        "_synthetic_kind": "polling_timeout",
        "_container_id": container_id,
    }


def is_synthetic_polling_timeout_result(result: Any) -> bool:
    if not isinstance(result, dict) or not result.get("_synthetic"):
        return False
    return result.get("status") == "timeout" and result.get("_synthetic_kind") == "polling_timeout"


# All terminal `status` values (lowercase) — return API dict immediately.
_PHANTOM_POLL_TERMINAL: frozenset[str] = frozenset(
    {
        "finished",
        "error",
        "aborted",
        "canceled",
        "cancelled",
    }
)
# Fast re-poll window after launch: short sleep while status is non-terminal (e.g. 3s "already processed" runs)
_POLL_FAST_SEC = 3
_POLL_SLOW_SEC = 30
_EARLY_FAST_POLL_WINDOW = timedelta(minutes=3)
# If `status` is never set but `resultObject` is present (Phantombuster quirk), treat as finished after
# this many identical JSON snapshots in a row; inject status=finished so engagement_runner can proceed.
_RESULTOBJECT_INFER_REPEATS = 3
# "Already processed" / no-output dupes: empty resultObject, missing status — infer after this wall-clock stretch.
_NOSTATUS_DEDUPE_SEC = 60
_SYNTHETIC_INFERRED = "_synthetic_inferred"
_SYNTHETIC_INFER_KEY = "resultobject_nostatus_stable"
_SYNTHETIC_INFER_KEY_60S_EMPTY = "nostatus_dedupe_60s_empty"


def _is_empty_or_none_status(status: Any) -> bool:
    if status is None:
        return True
    if isinstance(status, str) and not str(status).strip():
        return True
    return False


def _resultobject_materially_nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, dict)):
        return len(value) > 0
    if isinstance(value, str):
        t = value.strip()
        if not t:
            return False
        if t in ("[]", "{}", "null"):
            return False
        try:
            parsed: Any = json.loads(t)
        except (ValueError, TypeError, json.JSONDecodeError):
            return bool(t)
        if isinstance(parsed, (list, dict)) and len(parsed) == 0:
            return False
        if isinstance(parsed, str) and not str(parsed).strip():
            return False
        return True
    return True


def _fingerprint_nostatus_result(result: dict[str, Any]) -> str:
    try:
        return json.dumps(result, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(result)


def _normalize_fetch_result(result: dict[str, Any]) -> dict[str, Any]:
    """Parse resultObject JSON object strings so downstream dedupe heuristics see nested events."""
    ro = result.get("resultObject")
    if isinstance(ro, str):
        t = ro.strip()
        if t.startswith("{"):
            try:
                parsed = json.loads(t)
                return {**result, "resultObject": parsed}
            except (ValueError, TypeError, json.JSONDecodeError):
                pass
    return result


def _now_utc() -> datetime:
    """Test seam: time source for polling loop (use patch in unit tests)."""
    return datetime.utcnow()


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
            raise ValueError(
                "bonus_argument_not_supported_use_phantombuster_workspace_session"
            )

        if isinstance(argument, dict) and (
            argument.get("profileUrls") is not None
            or argument.get("profileUrl") is not None
            or argument.get("spreadsheetUrl") is not None
        ):
            from core.phantom_payload import (
                is_message_sender_style_argument,
                log_engagement_argument_json,
                validate_dm_message_sender_argument,
                validate_engagement_argument,
            )

            if is_message_sender_style_argument(argument):
                ok, msg = validate_dm_message_sender_argument(argument)
            else:
                ok, msg = validate_engagement_argument(argument)
            if not ok:
                raise ValueError(f"invalid_phantom_argument:{msg}")
            log_engagement_argument_json(argument, agent_id=agent_id)

        if isinstance(argument, dict) and (
            argument.get("profileUrl") is not None or argument.get("spreadsheetUrl") is not None
        ):
            logger.info(
                "[DEBUG] final_argument_json: %s",
                json.dumps(argument, ensure_ascii=False, sort_keys=True),
            )

        url = f"{PHANTOMBUSTER_BASE_URL}/agents/launch"
        payload: dict[str, Any] = {"id": agent_id, "argument": argument}

        try:
            from core.phantom_payload import redact_phantom_argument_for_log

            log_payload: dict[str, Any] = {"id": agent_id, "argument": redact_phantom_argument_for_log(argument)}
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
                if not resp.ok:
                    last_err = PhantombusterHttpError.from_response(resp, context="POST /agents/launch")
                    logger.warning(
                        "[Phantombuster] launch attempt %s/%s failed: %s", attempt, MAX_RETRIES, last_err
                    )
                    time.sleep(2**attempt)
                    continue
                try:
                    data = resp.json()
                except (ValueError, json.JSONDecodeError) as je:
                    raw = _truncate_pb_body(getattr(resp, "text", "") or "", max_len=800)
                    last_err = PhantombusterHttpError(
                        status_code=resp.status_code,
                        body_snippet=f"invalid JSON ({je}); body={raw}",
                        context="POST /agents/launch",
                        url=str(getattr(resp, "url", "") or ""),
                    )
                    logger.warning(
                        "[Phantombuster] launch attempt %s/%s failed: %s", attempt, MAX_RETRIES, last_err
                    )
                    time.sleep(2**attempt)
                    continue
                container_id = data.get("containerId") if isinstance(data, dict) else None
                if not container_id:
                    blob = (
                        json.dumps(data, ensure_ascii=False, default=str)
                        if isinstance(data, dict)
                        else str(data)
                    )
                    last_err = PhantombusterHttpError(
                        status_code=resp.status_code,
                        body_snippet=f"missing containerId; response={blob}",
                        context="POST /agents/launch",
                        url=str(getattr(resp, "url", "") or ""),
                    )
                    logger.warning(
                        "[Phantombuster] launch attempt %s/%s failed: %s", attempt, MAX_RETRIES, last_err
                    )
                    time.sleep(2**attempt)
                    continue
                logger.info("[Phantombuster] Agent %s launched. Container: %s", agent_id, container_id)
                return str(container_id)
            except requests.RequestException as e:
                r = getattr(e, "response", None)
                if r is not None:
                    last_err = PhantombusterHttpError.from_response(r, context="POST /agents/launch")
                else:
                    last_err = e
                logger.warning("[Phantombuster] launch attempt %s/%s failed: %s", attempt, MAX_RETRIES, last_err)
                time.sleep(2**attempt)
        raise last_err or RuntimeError("launch failed")

    def launch_agent(
        self,
        agent_id: str,
        argument: dict[str, Any],
        bonus_argument: Optional[dict[str, Any]] = None,
    ) -> str:
        """Launch any phantom by id; argument must match the phantom's expected schema."""
        if PHANTOMBUSTER_SERIALIZE_AGENT_LAUNCHES:
            lock = _shared_lock_for_agent(agent_id)
            with lock:
                return self._launch_agent_unlocked(agent_id, argument, bonus_argument)
        return self._launch_agent_unlocked(agent_id, argument, bonus_argument)

    # ── Poll until agent finishes ──────────────────────────────────────────────
    def _log_polling_result(self, container_id: str, result: Any) -> None:
        if PHANTOMBUSTER_LOG_POLLING_DEBUG:
            try:
                blob = json.dumps(result, default=str)[:3000]
            except (TypeError, ValueError):
                blob = str(result)[:3000]
            logger.info("[Phantombuster] poll result (PHANTOMBUSTER_LOG_POLLING_DEBUG) container=%s: %s", container_id, blob)
            return
        if isinstance(result, dict):
            st = result.get("status")
            keys = list(result.keys())[:30]
        else:
            st = "non-dict"
            keys = []
        logger.info(
            "[Phantombuster] poll container=%s status=%s top_keys=%s",
            container_id,
            st,
            keys,
        )

    def wait_for_completion(self, container_id: str, timeout_minutes: int = 30) -> dict:
        """
        Poll /containers/fetch-result-object until status is a terminal value, a synthetic match, or the deadline.
        First request is immediate; for the first few minutes, re-poll every _POLL_FAST_SEC, then _POLL_SLOW_SEC.
        On deadline, returns synthetic_polling_timeout_result (not empty {}), so callers can log pb_polling_timeout.

        Inferred `status: finished` (Phantombuster API quirks):
        - Missing/empty `status` with a stable non-empty `resultObject` — after _RESULTOBJECT_INFER_REPEATS identical
          full-response snapshots.
        - Missing/empty `status` and empty/unchanging `resultObject` for _NOSTATUS_DEDUPE_SEC (already-processed / no rows) —
          the `running` status clears the ghost clock so real jobs (3–5+ min) keep polling.
        """
        url = f"{PHANTOMBUSTER_BASE_URL}/containers/fetch-result-object"
        deadline = _now_utc() + timedelta(minutes=timeout_minutes)
        started = _now_utc()
        nostatus_stable_fp: str | None = None
        nostatus_stable_repeats = 0
        ghost_start: Optional[datetime] = None

        while _now_utc() < deadline:
            resp = self.session.get(url, params={"id": container_id})
            if not resp.ok:
                raise PhantombusterHttpError.from_response(
                    resp, context="GET /containers/fetch-result-object"
                )
            try:
                result: Any = resp.json()
            except (ValueError, json.JSONDecodeError) as je:
                raise PhantombusterHttpError(
                    status_code=resp.status_code,
                    body_snippet=f"invalid JSON ({je}); body={_truncate_pb_body(resp.text or '', max_len=800)}",
                    context="GET /containers/fetch-result-object",
                    url=str(getattr(resp, "url", "") or ""),
                ) from je
            if not isinstance(result, dict):
                logger.warning(
                    "[Phantombuster] fetch-result-object returned non-dict for container=%s: %s",
                    container_id,
                    type(result).__name__,
                )
                ghost_start = None
                nostatus_stable_fp = None
                nostatus_stable_repeats = 0
                time.sleep(_POLL_FAST_SEC)
                continue

            self._log_polling_result(container_id, result)
            status = result.get("status")
            st_str = str(status).strip().lower() if status is not None else ""
            ro = result.get("resultObject")

            if st_str in _PHANTOM_POLL_TERMINAL:
                if st_str == "finished":
                    logger.info("[Phantombuster] Agent finished successfully (container=%s).", container_id)
                elif st_str == "error":
                    logger.error("[Phantombuster] Agent errored: %s", result.get("message"))
                else:
                    logger.warning(
                        "[Phantombuster] Agent stopped with terminal status=%s (container=%s).",
                        status,
                        container_id,
                    )
                return _normalize_fetch_result(result)
            if st_str == "running":
                logger.debug(
                    "[Phantombuster] status=running, polling (no 60s ghost) container=%s",
                    container_id,
                )
                ghost_start = None
                nostatus_stable_fp = None
                nostatus_stable_repeats = 0
                if _now_utc() - started < _EARLY_FAST_POLL_WINDOW:
                    time.sleep(_POLL_FAST_SEC)
                else:
                    time.sleep(_POLL_SLOW_SEC)
                continue
            if not _is_empty_or_none_status(status):
                # e.g. pending, queued — not the dedupe-ghost: reset clocks
                logger.debug(
                    "[Phantombuster] non-terminal status=%s — reset ghost timer (container=%s)",
                    status,
                    container_id,
                )
                ghost_start = None
                nostatus_stable_fp = None
                nostatus_stable_repeats = 0
                if _now_utc() - started < _EARLY_FAST_POLL_WINDOW:
                    time.sleep(_POLL_FAST_SEC)
                else:
                    time.sleep(_POLL_SLOW_SEC)
                continue

            # Status missing, null, or empty string: dedupe-ghost and/or resultObject-stability
            if ghost_start is None:
                ghost_start = _now_utc()
            if _resultobject_materially_nonempty(ro):
                fp = _fingerprint_nostatus_result(result)
                if fp == nostatus_stable_fp:
                    nostatus_stable_repeats += 1
                else:
                    nostatus_stable_fp = fp
                    nostatus_stable_repeats = 1
                if nostatus_stable_repeats >= _RESULTOBJECT_INFER_REPEATS:
                    combined: dict[str, Any] = {
                        **result,
                        "status": "finished",
                        _SYNTHETIC_INFERRED: _SYNTHETIC_INFER_KEY,
                    }
                    logger.info(
                        "[Phantombuster] inferred_status=finished resultObject stable x%s (no status) container=%s",
                        _RESULTOBJECT_INFER_REPEATS,
                        container_id,
                    )
                    return _normalize_fetch_result(combined)
                logger.debug(
                    "[Phantombuster] Missing status; awaiting stable resultObject (repeat %s/%s) container=%s",
                    nostatus_stable_repeats,
                    _RESULTOBJECT_INFER_REPEATS,
                    container_id,
                )
            else:
                nostatus_stable_fp = None
                nostatus_stable_repeats = 0
                logger.debug(
                    "[Phantombuster] Missing or empty status; empty resultObject (container=%s) keys=%s",
                    container_id,
                    list(result.keys())[:25],
                )
            # 60s dedupe ghost: only for empty/unchanging output (PB "already processed" with no rows).
            # When resultObject is materially non-empty, rely on stable 3x above, not a wall clock.
            if (not _resultobject_materially_nonempty(ro)) and (
                _now_utc() - ghost_start >= timedelta(seconds=_NOSTATUS_DEDUPE_SEC)
            ):
                empty_combined: dict[str, Any] = {
                    **result,
                    "status": "finished",
                    _SYNTHETIC_INFERRED: _SYNTHETIC_INFER_KEY_60S_EMPTY,
                }
                logger.info(
                    "[Phantombuster] inferred_status=finished no status for %ss (dedupe-ghost) container=%s",
                    _NOSTATUS_DEDUPE_SEC,
                    container_id,
                )
                return _normalize_fetch_result(empty_combined)

            logger.debug("[Phantombuster] Status: %s — waiting (container=%s)...", status, container_id)
            if _now_utc() - started < _EARLY_FAST_POLL_WINDOW:
                time.sleep(_POLL_FAST_SEC)
            else:
                time.sleep(_POLL_SLOW_SEC)

        logger.warning(
            "[Phantombuster] Polling timed out after %s minutes (container=%s).",
            timeout_minutes,
            container_id,
        )
        return synthetic_polling_timeout_result(container_id)

    # ── Fetch the output CSV/JSON from completed agent ────────────────────────────
    def fetch_output(self, container_id: str) -> list[dict]:
        """Download and parse the agent's output data."""
        url = f"{PHANTOMBUSTER_BASE_URL}/containers/fetch-output"
        resp = self.session.get(url, params={"id": container_id})
        if not resp.ok:
            raise PhantombusterHttpError.from_response(resp, context="GET /containers/fetch-output")
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError) as je:
            raise PhantombusterHttpError(
                status_code=resp.status_code,
                body_snippet=f"invalid JSON ({je}); body={_truncate_pb_body(resp.text or '', max_len=800)}",
                context="GET /containers/fetch-output",
                url=str(getattr(resp, 'url', '') or ''),
            ) from je

        # Phantombuster output is in resultObject as JSON array
        result_object = data.get("resultObject", "[]")
        if isinstance(result_object, str):
            try:
                return json.loads(result_object)
            except Exception:
                return []
        return result_object or []

    def run_agent(
        self,
        agent_id: str,
        argument: dict[str, Any],
        *,
        timeout_minutes: int = 45,
        bonus_argument: Optional[dict[str, Any]] = None,
    ) -> tuple[dict, str]:
        """Launch, wait, return (result_object, container_id). Serialized per agent_id."""
        if PHANTOMBUSTER_SERIALIZE_AGENT_LAUNCHES:
            lock = _shared_lock_for_agent(agent_id)
            with lock:
                cid = self._launch_agent_unlocked(agent_id, argument, bonus_argument)
                result = self.wait_for_completion(cid, timeout_minutes=timeout_minutes)
            return result, cid
        cid = self._launch_agent_unlocked(agent_id, argument, bonus_argument)
        result = self.wait_for_completion(cid, timeout_minutes=timeout_minutes)
        return result, cid
