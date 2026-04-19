"""
Dashboard API + optional static SPA (production: serve frontend/dist).
Run from repo root: uvicorn dashboard_app.main:app --reload --port 8080
"""

from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path
from datetime import date
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import ACCOUNT_CONFIG_PATH, DATABASE_PATH
from core.accounts_loader import load_accounts_document
from core.db import get_connection, init_schema

# Optional env (set in .env via python-dotenv when config loads)
DASHBOARD_API_TOKEN = os.getenv("DASHBOARD_API_TOKEN", "").strip()
DEFAULT_CSV = ROOT / "output" / "leads_lookup_merged.csv"
FALLBACK_CSV = ROOT / "output" / "apollo_leads.csv"
DASHBOARD_CSV_PATH = Path(os.getenv("DASHBOARD_CSV_PATH", str(DEFAULT_CSV))).resolve()
FRONTEND_DIST = ROOT / "frontend" / "dist"
MAX_PAGE_SIZE = 200


def _resolve_csv_path() -> Path:
    p = DASHBOARD_CSV_PATH
    if p.is_file():
        return p
    if FALLBACK_CSV.is_file():
        return FALLBACK_CSV
    return p


def verify_dashboard_token(request: Request) -> None:
    if not DASHBOARD_API_TOKEN:
        return
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = auth[7:].strip()
    if token != DASHBOARD_API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")


OptionalAuth = Depends(verify_dashboard_token)


def create_app() -> FastAPI:
    init_schema()

    app = FastAPI(title="Leadgen Dashboard", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=os.getenv("DASHBOARD_CORS_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173").split(","),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health(_: None = OptionalAuth) -> dict[str, Any]:
        db_path = Path(DATABASE_PATH)
        ok = db_path.is_file()
        return {
            "ok": True,
            "database_path": str(db_path),
            "database_exists": ok,
            "csv_path": str(_resolve_csv_path()),
            "csv_exists": _resolve_csv_path().is_file(),
        }

    @app.get("/api/stats/overview")
    def stats_overview(_: None = OptionalAuth) -> dict[str, Any]:
        conn = get_connection()
        try:
            by_status = conn.execute(
                "SELECT status, COUNT(*) AS c FROM leads GROUP BY status ORDER BY c DESC"
            ).fetchall()
            total_leads = conn.execute("SELECT COUNT(*) AS c FROM leads").fetchone()["c"]

            actions = conn.execute(
                """
                SELECT action_type, dry_run, status, COUNT(*) AS c
                FROM action_log
                GROUP BY action_type, dry_run, status
                """
            ).fetchall()

            since_24h = conn.execute(
                """
                SELECT COUNT(*) AS c FROM action_log
                WHERE created_at >= datetime('now', '-1 day')
                  AND status = 'error' AND dry_run = 0
                """
            ).fetchone()["c"]

            metrics_7d = conn.execute(
                """
                SELECT day, account_id, metric, SUM(value) AS v
                FROM metrics_daily
                WHERE day >= date('now', '-7 days')
                GROUP BY day, account_id, metric
                ORDER BY day ASC, account_id, metric
                """
            ).fetchall()

            return {
                "total_leads": total_leads,
                "leads_by_status": [dict(r) for r in by_status],
                "action_log_breakdown": [dict(r) for r in actions],
                "errors_last_24h_real": since_24h,
                "metrics_daily_last_7d": [dict(r) for r in metrics_7d],
            }
        finally:
            conn.close()

    @app.get("/api/leads")
    def list_leads(
        _: None = OptionalAuth,
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
        q: str = "",
        status: str = "",
        account_id: str = "",
        campaign_id: str = "",
        created_from: str = "",
        created_to: str = "",
    ) -> dict[str, Any]:
        offset = (page - 1) * limit
        where: list[str] = ["1=1"]
        params: list[Any] = []

        if q.strip():
            term = f"%{q.strip()}%"
            where.append(
                "(full_name LIKE ? OR company_name LIKE ? OR title LIKE ? OR linkedin_url LIKE ?)"
            )
            params.extend([term, term, term, term])
        if status.strip():
            where.append("status = ?")
            params.append(status.strip())
        if account_id.strip():
            where.append("account_id = ?")
            params.append(account_id.strip())
        if campaign_id.strip():
            where.append("campaign_id = ?")
            params.append(campaign_id.strip())
        if created_from.strip():
            where.append("created_at >= ?")
            params.append(created_from.strip())
        if created_to.strip():
            where.append("created_at <= ?")
            params.append(created_to.strip())

        wh = " AND ".join(where)
        conn = get_connection()
        try:
            total = conn.execute(f"SELECT COUNT(*) AS c FROM leads WHERE {wh}", params).fetchone()["c"]
            rows = conn.execute(
                f"""
                SELECT * FROM leads WHERE {wh}
                ORDER BY updated_at DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
            return {
                "page": page,
                "limit": limit,
                "total": total,
                "items": [dict(r) for r in rows],
            }
        finally:
            conn.close()

    @app.get("/api/leads/{lead_id}")
    def get_lead(lead_id: str, _: None = OptionalAuth) -> dict[str, Any]:
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Lead not found")
            d = dict(row)
            raw_hist = d.get("message_history_json") or "[]"
            try:
                d["message_history"] = json.loads(raw_hist) if isinstance(raw_hist, str) else raw_hist
            except json.JSONDecodeError:
                d["message_history"] = []
            return d
        finally:
            conn.close()

    @app.get("/api/actions")
    def list_actions(
        _: None = OptionalAuth,
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
        action_type: str = "",
        account_id: str = "",
        dry_run: Optional[int] = None,
        created_from: str = "",
        created_to: str = "",
    ) -> dict[str, Any]:
        offset = (page - 1) * limit
        where = ["1=1"]
        params: list[Any] = []
        if action_type.strip():
            where.append("action_type = ?")
            params.append(action_type.strip())
        if account_id.strip():
            where.append("account_id = ?")
            params.append(account_id.strip())
        if dry_run is not None:
            where.append("dry_run = ?")
            params.append(1 if dry_run else 0)
        if created_from.strip():
            where.append("created_at >= ?")
            params.append(created_from.strip())
        if created_to.strip():
            where.append("created_at <= ?")
            params.append(created_to.strip())
        wh = " AND ".join(where)
        conn = get_connection()
        try:
            total = conn.execute(f"SELECT COUNT(*) AS c FROM action_log WHERE {wh}", params).fetchone()["c"]
            rows = conn.execute(
                f"""
                SELECT * FROM action_log WHERE {wh}
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
            return {"page": page, "limit": limit, "total": total, "items": [dict(r) for r in rows]}
        finally:
            conn.close()

    @app.get("/api/accounts")
    def list_accounts(_: None = OptionalAuth) -> dict[str, Any]:
        conn = get_connection()
        try:
            meta_rows = conn.execute("SELECT * FROM accounts_meta ORDER BY account_id").fetchall()
            meta_by_id = {r["account_id"]: dict(r) for r in meta_rows}
        finally:
            conn.close()

        try:
            doc = load_accounts_document(ACCOUNT_CONFIG_PATH)
        except (FileNotFoundError, ValueError) as e:
            return {"campaign_id": None, "accounts": [], "config_error": str(e), "accounts_meta": list(meta_by_id.values())}

        out: list[dict[str, Any]] = []
        today = date.today().isoformat()
        conn = get_connection()
        try:
            for a in doc.accounts:
                row = conn.execute(
                    "SELECT * FROM daily_usage WHERE account_id=? AND day=?",
                    (a.account_id, today),
                ).fetchone()
                usage = dict(row) if row else {"connects": 0, "dms": 0, "replies": 0}
                pb_connect = a.phantombuster_connect_agent_id or os.getenv("PHANTOMBUSTER_CONNECT_AGENT_ID", "")
                pb_dm = a.phantombuster_dm_agent_id or os.getenv("PHANTOMBUSTER_DM_AGENT_ID", "")
                meta_row = meta_by_id.get(a.account_id) or {}
                ua_meta = str(meta_row.get("user_agent") or "").strip()
                out.append(
                    {
                        "account_id": a.account_id,
                        "linkedin_profile": a.linkedin_profile,
                        "user_agent": ua_meta or (a.user_agent or None),
                        "schedule_start": a.schedule_start,
                        "schedule_end": a.schedule_end,
                        "primary_strategy": a.primary_strategy,
                        "delay_min_sec": a.delay_min_sec,
                        "delay_max_sec": a.delay_max_sec,
                        "steady_connect_cap": a.steady_connect_cap,
                        "steady_dm_cap": a.steady_dm_cap,
                        "steady_reply_cap": a.steady_reply_cap,
                        "weekend_actions": a.weekend_actions,
                        "phantombuster_connect_agent_id": pb_connect or None,
                        "phantombuster_dm_agent_id": pb_dm or None,
                        "daily_usage_today": usage,
                        "accounts_meta_row": meta_by_id.get(a.account_id),
                    }
                )
        finally:
            conn.close()

        return {"campaign_id": doc.campaign_id, "accounts": out}

    @app.get("/api/metrics/daily")
    def metrics_daily(
        _: None = OptionalAuth,
        days: int = Query(30, ge=1, le=365),
        metric: str = "",
        account_id: str = "",
    ) -> dict[str, Any]:
        conn = get_connection()
        try:
            where = ["day >= date('now', ?)"]
            params: list[Any] = [f"-{int(days)} days"]
            if metric.strip():
                where.append("metric = ?")
                params.append(metric.strip())
            if account_id.strip():
                where.append("account_id = ?")
                params.append(account_id.strip())
            wh = " AND ".join(where)
            rows = conn.execute(
                f"SELECT day, account_id, metric, value FROM metrics_daily WHERE {wh} ORDER BY day ASC",
                params,
            ).fetchall()
            return {"items": [dict(r) for r in rows]}
        finally:
            conn.close()

    @app.get("/api/csv/leads")
    def csv_leads(
        _: None = OptionalAuth,
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
        q: str = "",
    ) -> dict[str, Any]:
        path = _resolve_csv_path()
        if not path.is_file():
            return {
                "page": page,
                "limit": limit,
                "total": 0,
                "items": [],
                "fieldnames": [],
                "csv_path": str(path),
                "error": "file_not_found",
            }

        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = list(reader.fieldnames or [])
            rows = list(reader)

        if q.strip():
            qt = q.strip().lower()
            rows = [
                r
                for r in rows
                if any(qt in str(v).lower() for v in r.values() if v)
            ]

        total = len(rows)
        offset = (page - 1) * limit
        page_rows = rows[offset : offset + limit]

        return {
            "page": page,
            "limit": limit,
            "total": total,
            "items": page_rows,
            "fieldnames": list(fieldnames),
            "csv_path": str(path),
        }

    @app.get("/api/csv/file")
    def csv_file_download(_: None = OptionalAuth) -> FileResponse:
        """Serve the active dashboard CSV as a download (full file on disk)."""
        path = _resolve_csv_path()
        if not path.is_file():
            raise HTTPException(status_code=404, detail="csv_not_found")
        return FileResponse(
            path,
            media_type="text/csv; charset=utf-8",
            filename=path.name,
        )

    if FRONTEND_DIST.is_dir() and (FRONTEND_DIST / "index.html").is_file():
        assets_dir = FRONTEND_DIST / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/")
        def spa_index() -> FileResponse:
            """SPA entry (use HashRouter in frontend so /#/routes work without a catch-all)."""
            return FileResponse(FRONTEND_DIST / "index.html")

    return app


app = create_app()
