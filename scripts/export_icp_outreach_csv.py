#!/usr/bin/env python3
"""
Export strict ICP outreach-ready CSV from Apollo Web3 extract output.

Usage (from repo root):
  python scripts/export_icp_outreach_csv.py

Reads ICP_OUTREACH_INPUT_CSV (default: APOLLO_WEB3_CSV_PATH from config).
Writes ICP_OUTREACH_OUTPUT_CSV (default: output/icp_outreach_ready.csv).
"""

from __future__ import annotations

import csv
import logging
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import APOLLO_WEB3_CSV_PATH, BASE_DIR
from lead_extraction.outreach_ready_filter import dedupe_rows, outreach_ready_reject_reason

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

OUT_COLS = [
    "full_name",
    "linkedin_url",
    "email",
    "title",
    "company_name",
    "company_size",
    "industry",
    "location",
]


def main() -> int:
    inp = Path(os.getenv("ICP_OUTREACH_INPUT_CSV", APOLLO_WEB3_CSV_PATH)).resolve()
    outp = Path(
        os.getenv("ICP_OUTREACH_OUTPUT_CSV", str(BASE_DIR / "output" / "icp_outreach_ready.csv"))
    )
    if not inp.is_file():
        logger.error("Input CSV not found: %s", inp)
        return 1

    geo_strict = os.getenv("ICP_GEO_STRICT", "").lower() in ("1", "true", "yes", "on")
    require_email_status = os.getenv("ICP_REQUIRE_APOLLO_EMAIL_STATUS", "").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )

    with inp.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    if require_email_status and "email_status" not in fieldnames:
        logger.warning(
            "ICP_REQUIRE_APOLLO_EMAIL_STATUS is enabled but input CSV has no 'email_status' column "
            "(%s). Re-run `python main.py --apollo-web3 --target …` to regenerate apollo_leads.csv, "
            "or disable ICP_REQUIRE_APOLLO_EMAIL_STATUS until the file is refreshed.",
            inp,
        )

    reasons: Counter[str] = Counter()
    passing: list[dict[str, str]] = []
    for row in rows:
        rr = outreach_ready_reject_reason(row, geo_strict=geo_strict)
        if rr:
            reasons[rr] += 1
        else:
            passing.append({k: str(row.get(k) or "").strip() for k in OUT_COLS})

    before_dedupe = len(passing)
    passing = dedupe_rows(passing)
    dupes = before_dedupe - len(passing)

    logger.info("Input rows: %s", len(rows))
    for k, v in reasons.most_common():
        logger.info("  rejected [%s]: %s", k, v)
    logger.info("  passing (pre-dedupe): %s", before_dedupe)
    logger.info("  duplicates removed: %s", dupes)
    logger.info("  final rows: %s", len(passing))

    outp.parent.mkdir(parents=True, exist_ok=True)
    passing.sort(key=lambda r: (r["company_name"].lower(), r["email"].lower()))
    with outp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUT_COLS)
        w.writeheader()
        w.writerows(passing)
    logger.info("Wrote %s rows → %s", len(passing), outp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
