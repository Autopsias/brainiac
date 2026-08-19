"""COS ledger-join operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._io import _read_jsonl
from ._runs import run_ops_dir

def _first(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for k in keys:
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""

def ledger_index(vault) -> dict[str, list[dict[str, str]]]:
    """``{proposal_id: [{"run_id", "digest", "category"}]}`` over EVERY run
    ledger in the ops dir. Built once per claim pass, not once per candidate."""
    idx: dict[str, list[dict[str, str]]] = {}
    d = run_ops_dir(vault)
    if not d.is_dir():
        return idx
    for path in sorted(d.glob(_LEDGER_GLOB)):
        m = _LEDGER_RUN_RE.match(path.name)
        if not m:
            continue
        run_id = m.group(1)
        for row in _read_jsonl(path):
            pid = _first(row, _LEDGER_ID_KEYS)
            if not pid:
                continue
            idx.setdefault(pid, []).append({
                "run_id": run_id,
                "digest": _first(row, _LEDGER_DIGEST_KEYS).lower(),
                "category": str(row.get("category") or "").strip(),
            })
    return idx

def join_ledger_category(idx: dict[str, list[dict[str, str]]],
                         proposal_id: str, sha: str) -> dict[str, Any]:
    """Which run produced this exact content, and what category did it assign?

    ``{"status": "joined"|"no-ledger-row"|"collision"|"no-digest"|
    "digest-mismatch", "run_id", "category", "reason"}``. Every non-``joined``
    status is a quarantine — never a silent default."""
    rows = idx.get(proposal_id) or []
    if not rows:
        return {"status": "no-ledger-row", "reason":
                f"no run's ingestion ledger carries a row for {proposal_id!r} — "
                "the host cannot tell which run produced it, or what category "
                "that run assigned"}
    runs = sorted({r["run_id"] for r in rows})
    if len(runs) > 1:
        return {"status": "collision", "reason":
                f"{proposal_id!r} is claimed by {len(runs)} runs ({', '.join(runs)}) "
                "— one run / one proposal id is required; refusing to pick one"}
    run_id = runs[0]
    digests = sorted({r["digest"] for r in rows})
    if len(digests) > 1:
        return {"status": "collision", "run_id": run_id, "reason":
                f"run {run_id} published {len(digests)} different content digests "
                f"for {proposal_id!r} — refusing to prefer one row over another"}
    digest = digests[0]
    if not digest:
        return {"status": "no-digest", "run_id": run_id, "reason":
                f"run {run_id}'s ledger row for {proposal_id!r} carries no content "
                "digest, so it proves nothing about THESE bytes"}
    if not _SHA256_RE.match(digest) or digest != sha:
        return {"status": "digest-mismatch", "run_id": run_id, "reason":
                f"run {run_id}'s ledger row for {proposal_id!r} names content "
                f"{digest[:12]}… but the claimed drop hashes to {sha[:12]}…"}
    cats = sorted({r["category"] for r in rows})
    if len(cats) > 1:
        return {"status": "collision", "run_id": run_id, "reason":
                f"run {run_id} assigned {proposal_id!r} {len(cats)} different "
                f"categories ({', '.join(c or '(empty)' for c in cats)})"}
    return {"status": "joined", "run_id": run_id, "category": cats[0],
            "reason": f"joined to run {run_id} by id + full content digest"}

__all__ = ['_first', 'ledger_index', 'join_ledger_category']
