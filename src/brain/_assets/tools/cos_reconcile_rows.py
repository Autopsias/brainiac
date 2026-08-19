"""The one JSONL row reader shared by every `cos_reconcile_metrics` join (batch-2 drain).

`DATE_RE`, `_rows` and `_date_of` moved verbatim out of `cos_reconcile_metrics`
so the parent, the guard and the append lane all read ledgers through ONE
definition; the parent re-imports them, so `cos_contract`'s
`from cos_reconcile_metrics import _rows` keeps its path.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _rows(path: Path) -> list[dict]:
    out: list[dict] = []
    if not path.exists():  # an absent ledger is zero rows, not a crash
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _date_of(path: Path) -> str | None:
    m = DATE_RE.search(path.name)
    return m.group(1) if m else None
