"""Off-host anchoring of the Ed25519 audit-chain head (SEC-03).

The audit chain (brain.audit) is only as strong as the single key that signs it:
whoever holds the key can rewrite history and re-sign it silently. Nothing
EXTERNAL attests the chain head. This module periodically publishes the signed
chain-head hash to an INDEPENDENT, append-only store so a post-hoc rewrite
becomes DETECTABLE — a later ``verify`` recomputes the head as-of each anchored
entry-count and compares it to what was independently recorded; a rewritten
prefix yields a head that no longer matches the anchor.

The anchor store MUST live somewhere an attacker who compromised the host + key
cannot also rewrite: a separate private repo, a different machine/volume, or
(strongest) an RFC-3161 timestamp token from a third-party TSA. Anchoring into
the vault tree itself buys nothing — keep it OFF-HOST.

The head value matches exactly what brain.audit would use as the next entry's
``prev_hash``: sha256(last stripped chain-entry line). We reuse AuditChain's own
helpers so the two can never diverge.

Ported FROM SCRATCH from the owner vault's ``_chain_anchor.py`` *pattern* — no
import of vault code.
"""
from __future__ import annotations

import json
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audit import NULL_PREV_HASH, AuditChain, _sha256

ANCHOR_LOG_NAME = "chain_anchor.log"  # JSONL, append-only, in the OFF-HOST dir


def _entry_lines(chain: AuditChain) -> list[str]:
    """Ordered, stripped chain-entry lines — the SAME predicate the chain uses."""
    return [s for line in chain._lines() if (s := line.strip()) and chain._is_entry(s)]


def head_as_of(lines: list[str], count: int) -> str:
    """sha256 of the ``count``-th entry line (1-based) — i.e. the head after
    ``count`` entries, == the prev_hash the (count+1)-th entry would carry.
    ``count==0`` is the empty-chain head."""
    if count <= 0:
        return NULL_PREV_HASH
    if count > len(lines):
        raise ValueError(f"anchor records {count} entries but live chain has only {len(lines)}")
    return _sha256(lines[count - 1])


def current_head(chain: AuditChain) -> tuple[str, int]:
    """(head_hash, entry_count) of the live chain right now."""
    lines = _entry_lines(chain)
    return (head_as_of(lines, len(lines)), len(lines))


def anchor(chain_log: Path, anchor_dir: Path, *, tsa_token: bytes | None = None) -> dict[str, Any]:
    """Append the current signed chain-head to the OFF-HOST anchor log.

    Re-anchoring an unchanged head simply appends a fresh dated record — the
    timeline of attestations is itself the evidence. Returns the appended record.
    """
    chain = AuditChain(Path(chain_log))
    head, count = current_head(chain)
    anchor_dir = Path(anchor_dir)
    anchor_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host": socket.gethostname(),
        "chain_log": str(Path(chain_log).resolve()),
        "entry_count": count,
        "head": head,
        "tsa": bool(tsa_token),
    }
    log = anchor_dir / ANCHOR_LOG_NAME
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n")
    if tsa_token:  # pragma: no cover - external TSA round-trip
        (anchor_dir / f"head-{count}.tsr").write_bytes(tsa_token)
    return {"anchored": True, "record": rec, "anchor_log": str(log)}


def verify_against_anchor(chain_log: Path, anchor_dir: Path) -> dict[str, Any]:
    """Recompute the head as-of every anchored entry-count and compare.

    A DIVERGENCE means the live chain's prefix differs from what was
    independently recorded off-host — a possible silent history rewrite. Returns
    a verdict dict; ``status`` is one of: ok | divergence | no-anchor.
    """
    chain = AuditChain(Path(chain_log))
    lines = _entry_lines(chain)
    log = Path(anchor_dir) / ANCHOR_LOG_NAME
    if not log.is_file():
        return {"status": "no-anchor", "anchor_log": str(log), "checked": 0, "divergences": []}

    checked = 0
    divergences: list[dict[str, Any]] = []
    for raw in log.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        rec = json.loads(raw)
        count = int(rec["entry_count"])
        checked += 1
        if count > len(lines):
            divergences.append({"entry_count": count, "error": "chain_shorter_than_anchor",
                                "live_entries": len(lines)})
            continue
        recomputed = head_as_of(lines, count)
        if recomputed != rec["head"]:
            divergences.append({"entry_count": count, "error": "head_mismatch",
                                "anchored": rec["head"][:16], "recomputed": recomputed[:16],
                                "anchored_ts": rec.get("ts")})
    return {
        "status": "divergence" if divergences else "ok",
        "anchor_log": str(log),
        "checked": checked,
        "live_entries": len(lines),
        "divergences": divergences,
    }
