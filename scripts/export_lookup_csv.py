#!/usr/bin/env python3
"""
Merge Apollo pipeline CSVs into one lookup-friendly file.

Usage (from repo root):
  python scripts/export_lookup_csv.py

Reads (if present): output/1_raw_leads.csv, output/apollo_leads.csv
Writes: output/leads_lookup_merged.csv

Rows must have non-empty email and linkedin_url. Duplicates (same normalized
LinkedIn URL, else same id) collapse to one row; apollo_web3 wins over
main_pipeline_raw, then higher score.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lead_extraction.normalize import normalize_linkedin_url

OUTPUT_DIR = ROOT / "output"
MERGED = OUTPUT_DIR / "leads_lookup_merged.csv"

LOOKUP_COLUMNS = [
    "source",
    "id",
    "full_name",
    "linkedin_url",
    "email",
    "title",
    "company_name",
    "company_size",
    "industry",
    "location",
    "years_experience",
    "score",
    "extracted_at",
]


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _row_key(row: dict[str, str]) -> str:
    li = normalize_linkedin_url(row.get("linkedin_url"))
    if li:
        return li
    rid = str(row.get("id") or "").strip()
    return f"id:{rid}" if rid else ""


def _source_rank(source: str) -> int:
    return 1 if source == "apollo_web3" else 0


def _score_int(row: dict[str, str]) -> int:
    try:
        return int(str(row.get("score") or "0").strip() or "0")
    except ValueError:
        return 0


def _row_better(a: dict[str, str], b: dict[str, str]) -> dict[str, str]:
    ra, rb = _source_rank(a["source"]), _source_rank(b["source"])
    if ra != rb:
        return a if ra > rb else b
    return a if _score_int(a) >= _score_int(b) else b


def main() -> int:
    sources: list[tuple[str, Path]] = [
        ("main_pipeline_raw", OUTPUT_DIR / "1_raw_leads.csv"),
        ("apollo_web3", OUTPUT_DIR / "apollo_leads.csv"),
    ]
    rows: list[dict[str, str]] = []
    for label, path in sources:
        for raw in _read_csv(path):
            row = {c: "" for c in LOOKUP_COLUMNS}
            row["source"] = label
            for c in LOOKUP_COLUMNS:
                if c == "source":
                    continue
                row[c] = str(raw.get(c) or "").strip()
            rows.append(row)

    with_contact = [
        r
        for r in rows
        if str(r.get("email") or "").strip() and str(r.get("linkedin_url") or "").strip()
    ]
    deduped: dict[str, dict[str, str]] = {}
    for r in with_contact:
        k = _row_key(r)
        if not k:
            continue
        if k not in deduped:
            deduped[k] = r
        else:
            deduped[k] = _row_better(deduped[k], r)

    out_rows = sorted(deduped.values(), key=_score_int, reverse=True)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with MERGED.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOOKUP_COLUMNS)
        w.writeheader()
        w.writerows(out_rows)

    print(
        f"Wrote {len(out_rows)} rows → {MERGED} "
        f"(from {len(rows)} merged, {len(with_contact)} with email+LinkedIn)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
