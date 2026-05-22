#!/usr/bin/env python3
"""
Remove LinkedIn company-page rows from a post-text lead CSV before import.

Standard headers: Name, Headline, Profile Url, Post text, Post url, occupation

Usage:
  python scripts/clean_leads.py input.csv output.csv
"""

from __future__ import annotations

import csv
import sys


def clean_csv(input_file: str, output_file: str) -> None:
    print(f"Reading from {input_file}...")

    with open(input_file, mode="r", encoding="utf-8-sig") as infile, open(
        output_file, mode="w", encoding="utf-8", newline=""
    ) as outfile:
        reader = csv.DictReader(infile)

        if not reader.fieldnames or "Profile Url" not in reader.fieldnames:
            print("Error: Could not find 'Profile Url' column. Check your headers.")
            sys.exit(1)

        writer = csv.DictWriter(outfile, fieldnames=reader.fieldnames)
        writer.writeheader()

        kept = 0
        removed = 0

        for row in reader:
            url = (row.get("Profile Url") or "").lower()
            if "/company/" in url:
                removed += 1
            else:
                writer.writerow(row)
                kept += 1

    print("\nCleanup complete.")
    print(f"Removed: {removed} company pages")
    print(f"Kept: {kept} valid personal profiles")
    print(f"Output saved to: {output_file}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python scripts/clean_leads.py <input.csv> <output.csv>")
        sys.exit(1)

    clean_csv(sys.argv[1], sys.argv[2])
