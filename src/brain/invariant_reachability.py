"""Read-only reachability artifact metric for the corpus watchdog."""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import Any


def unreachable_gold(vault: Path) -> dict[str, Any]:
    """Count gold documents no ranking leg reaches.

    The fold reads the newest reachability artifact and never re-runs the
    expensive embedded sweep. A missing or malformed artifact is unavailable,
    not a regression.
    """
    from . import invariants

    patterns = os.environ.get(invariants.REACHABILITY_GLOB_ENV, "").strip()
    globs = ([p for p in patterns.split(":") if p]
             if patterns else [str(vault / ".brain" / "eval" / "s06-reachability*.json")])
    import glob as _glob

    candidates: list[Path] = []
    for pattern in globs:
        candidates.extend(Path(p) for p in _glob.glob(pattern))
    if not candidates:
        return {"value": None, "available": False, "artifact": None,
                "generated": None, "age_days": None, "labels": None}
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        data = json.loads(newest.read_text(encoding="utf-8"))
        labels = data.get("labels") or []
        unreachable = sum(
            1 for rec in labels
            if isinstance(rec, dict)
            and not ((rec.get("reached") or {}).get("default_any_leg"))
        )
    except (OSError, ValueError, AttributeError) as exc:
        return {"value": None, "available": False, "artifact": str(newest),
                "generated": None, "age_days": None, "labels": None,
                "error": f"{type(exc).__name__}: {exc}"}
    generated = str(data.get("generated") or "")
    age_days = None
    try:
        age_days = (datetime.date.today()
                    - datetime.date.fromisoformat(generated[:10])).days
    except ValueError:
        pass
    return {
        "value": unreachable, "available": True, "artifact": str(newest),
        "generated": generated or None, "age_days": age_days,
        "labels": len(labels),
    }
