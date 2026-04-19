"""Parse Apollo organization headcount into a numeric upper bound for ICP checks."""

from __future__ import annotations

import re
from typing import Any


def parse_employee_upper_bound(raw: Any, fallback_org: dict | None = None) -> int | None:
    """
    Return an approximate max headcount for the org, or None if unknown.
    Apollo may return integers, strings like '1-10', '11-50', or fields on organization.
    """
    candidates: list[Any] = []
    if raw is not None and raw != "":
        candidates.append(raw)
    if fallback_org:
        for key in ("estimated_num_employees", "organization_headcount", "num_employees"):
            v = fallback_org.get(key)
            if v is not None and v != "":
                candidates.append(v)

    for c in candidates:
        n = _coerce_upper_bound(c)
        if n is not None:
            return n
    return None


def _coerce_upper_bound(val: Any) -> int | None:
    if val is None:
        return None
    if isinstance(val, int):
        return max(0, val)
    s = str(val).strip().lower().replace(",", "")
    if not s or s in ("unknown", "n/a", "na"):
        return None
    if s.endswith("+"):
        s = s[:-1].strip()
    m = re.match(r"^(\d+)\s*[-–]\s*(\d+)$", s)
    if m:
        return int(m.group(2))
    m = re.match(r"^(\d+)\s*[-–]\s*(\d+)\s*employees?$", s)
    if m:
        return int(m.group(2))
    if s.isdigit():
        return int(s)
    return None
