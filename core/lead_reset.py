"""Safe wipe of leads (and optional related tables) for post-text campaign reset."""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from config import DATABASE_PATH


def reset_leads_database(
    conn: sqlite3.Connection,
    *,
    leads_only: bool = False,
    backup: bool = True,
) -> Path | None:
    """
    Delete all leads. By default also clears action_log (orphaned lead_id references).
    Optionally backs up DATABASE_PATH first.
    Returns backup path if created.
    """
    db_path = Path(DATABASE_PATH)
    backup_path: Path | None = None
    if backup and db_path.is_file():
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        backup_path = db_path.parent / f"{db_path.stem}.bak-{ts}{db_path.suffix}"
        shutil.copy2(db_path, backup_path)

    if not leads_only:
        conn.execute("DELETE FROM action_log")
    conn.execute("DELETE FROM leads")
    conn.commit()
    return backup_path
