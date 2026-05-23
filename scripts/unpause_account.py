#!/usr/bin/env python3
"""Clear accounts_meta.paused so engagement is not blocked by account_paused_meta."""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.behavior_controller import set_account_paused
from core.db import init_schema
from core.repository import get_connection


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Unpause account(s) in SQLite (accounts_meta.paused=0)."
    )
    parser.add_argument(
        "account_id",
        nargs="?",
        default="",
        help="Account id to unpause (e.g. acc_c)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Unpause every row in accounts_meta",
    )
    args = parser.parse_args()

    init_schema()
    conn = get_connection()
    try:
        if args.all:
            cur = conn.execute("UPDATE accounts_meta SET paused=0")
            conn.commit()
            print(f"Unpaused all accounts ({cur.rowcount} row(s) updated).")
            return
        aid = (args.account_id or "").strip()
        if not aid:
            parser.error("Provide account_id or use --all")
        set_account_paused(conn, aid, False)
        row = conn.execute(
            "SELECT account_id, paused FROM accounts_meta WHERE account_id=?",
            (aid,),
        ).fetchone()
        if not row:
            print(f"Warning: no accounts_meta row for {aid!r} (run engagement once to sync).")
        else:
            print(f"Unpaused {aid} (paused={row['paused']}).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
