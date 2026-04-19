"""Campaign intelligence aggregates (PRD §14, §17)."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path


def export_intelligence(conn: sqlite3.Connection, out_dir: str = "output") -> dict[str, str]:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}

    rows = conn.execute(
        """
        SELECT day, account_id, metric, SUM(value) AS v
        FROM metrics_daily
        GROUP BY day, account_id, metric
        ORDER BY day DESC, account_id, metric
        """
    ).fetchall()
    p = Path(out_dir) / "metrics_daily.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["day", "account_id", "metric", "value"])
        for r in rows:
            w.writerow([r["day"], r["account_id"], r["metric"], r["v"]])
    paths["metrics_daily"] = str(p)

    rows2 = conn.execute(
        """
        SELECT account_id, strategy_used, action_type,
               COUNT(*) AS n,
               SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS okn
        FROM action_log
        WHERE dry_run=0
        GROUP BY account_id, strategy_used, action_type
        """
    ).fetchall()
    p2 = Path(out_dir) / "intelligence_by_strategy.csv"
    with p2.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["account_id", "strategy_used", "action_type", "count", "ok_count"])
        for r in rows2:
            w.writerow(
                [r["account_id"], r["strategy_used"], r["action_type"], r["n"], r["okn"]]
            )
    paths["intelligence_by_strategy"] = str(p2)

    funnel = conn.execute(
        """
        SELECT status, COUNT(*) AS c FROM leads GROUP BY status ORDER BY c DESC
        """
    ).fetchall()
    p3 = Path(out_dir) / "funnel_by_status.json"
    p3.write_text(
        json.dumps([dict(r) for r in funnel], indent=2),
        encoding="utf-8",
    )
    paths["funnel"] = str(p3)
    return paths
