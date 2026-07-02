"""The single egress chokepoint (SEC-01).

Every content-returning surface — the CLI subcommands and the optional MCP
adapter — funnels its results through ONE helper here before stdout, so the
deny-by-default classification gate cannot be silently bypassed by a *new*
subcommand that forgets to filter. This is the "force all integration through the
gated boundary" leg of the SEC-01 hardening (r2-codex): a later
``graph-expand`` / ``bases-query`` path must not surface a tier a sibling path
already withholds.

This module is an egress *decision* mechanism, NOT containment — a file-capable
harness reads the Markdown directly and bypasses it entirely (proven by
tests/test_direct_file_read.py). Real containment of sensitive tiers is
workspace *projection* (brain.projection) + the host/VM trust split. Per the
vault's own C-3 doctrine, a CLI/prompt-layer filter is defence-in-depth, never
the gate. See docs/operations/egress-provider-posture.md.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from . import classification as cls

# CANONICAL enumeration of every content-returning subcommand (r2-codex).
# Egress/classification-gate test coverage MUST cover each of these — a content
# path that is not on this list and not gated is a posture gap. ``rerank`` is not
# a separate command: ``search --rerank`` re-orders the SAME hits, then the same
# gate fires (covered explicitly in the test).
CONTENT_RETURNING_SUBCOMMANDS: tuple[str, ...] = (
    "search",          # fused RRF BM25+dense
    "hybrid-search",   # alias of search
    "grep",            # lexical scan
    "bases-query",     # structured frontmatter view
    "graph-expand",    # wikilink-BFS + PPR discovery candidates
    "get",             # one note by id
    "read",            # alias of get
    "recent",          # recently-updated list
)

# Host-broker / maintenance commands return STATUS, never note bodies — they are
# intentionally NOT gated by classification (nothing to leak) and so are NOT in
# the list above. Listed here for the audit so the split is explicit.
NON_CONTENT_SUBCOMMANDS: tuple[str, ...] = (
    "draft-capture", "rebuild", "sync", "snapshot", "status", "project",
    "write", "verify-audit", "anchor", "verify-anchor", "backup", "restore",
)


def apply_gate(
    items: Iterable[dict], max_tier: str = cls.DEFAULT_MAX_TIER,
    key: str = "classification",
) -> tuple[list[dict], dict]:
    """THE chokepoint: deny-by-default filter + honest redaction report.

    Returns ``(surfaced, egress_report)``. Used by the CLI for every
    content-returning subcommand and by the MCP adapter — one code path, no
    second egress surface to keep in sync.
    """
    items = list(items)
    flt = cls.ClassificationFilter(max_tier=max_tier)
    return flt.filter(items, key=key), flt.redaction_report(items, key=key)


# --------------------------------------------------------------------------
# Trusted-harness allowlist (SEC-01, HARDENED:claude / r2-claude)
# --------------------------------------------------------------------------
# Reconciles openness vs control. "Openness" does NOT mean "any app" — it means
# any harness that PASSES the vendor-posture bar (no-train/ZDR scope covering
# tool-call/API egress of vault content + Secret). The bar is owned here as a
# gating checklist; val-03's cross-harness set must EQUAL this allowlist. The
# register itself is data (docs/harness-allowlist.json) so it is reviewable and
# diffable; this loader is the typed accessor + invariants.
ALLOWLIST_PATH = Path(__file__).resolve().parents[2] / "docs" / "harness-allowlist.json"

_POSTURE_STATES = frozenset({"VERIFIED", "PENDING", "REJECTED"})


def load_allowlist(path: Path | None = None) -> dict[str, Any]:
    """Load the trusted-harness allowlist register. Raises on a malformed file
    (fail-closed: a register we cannot parse must not be treated as 'allow all')."""
    p = Path(path) if path else ALLOWLIST_PATH
    data = json.loads(p.read_text(encoding="utf-8"))
    if "harnesses" not in data or not isinstance(data["harnesses"], list):
        raise ValueError(f"allowlist {p} missing a 'harnesses' list")
    for h in data["harnesses"]:
        missing = {"id", "vendor", "posture_status", "verification_step", "owner"} - set(h)
        if missing:
            raise ValueError(f"allowlist entry {h.get('id')!r} missing keys: {sorted(missing)}")
        if h["posture_status"] not in _POSTURE_STATES:
            raise ValueError(
                f"allowlist entry {h['id']!r} has posture_status "
                f"{h['posture_status']!r}; expected one of {sorted(_POSTURE_STATES)}"
            )
    return data


def is_allowed(harness_id: str, path: Path | None = None) -> bool:
    """True iff ``harness_id`` is on the allowlist AND its vendor posture is
    VERIFIED. PENDING/REJECTED => default-deny (the posture bar is not met).

    NOTE: the brain CLI cannot reliably identify its caller, so this is a
    GOVERNANCE gate (consumed by val-03 + the cyber review + deployment policy),
    not a runtime per-request gate. The runtime control is the classification
    gate (apply_gate) + projection. Until a vendor's no-train/ZDR scope is
    contractually VERIFIED, the harness is not 'allowed' and must run only
    against a projected (sensitive-tier-free) workspace.
    """
    data = load_allowlist(path)
    for h in data["harnesses"]:
        if h["id"] == harness_id:
            return h["posture_status"] == "VERIFIED"
    return False


def posture_summary(path: Path | None = None) -> dict[str, Any]:
    """Counts by posture_status for the evidence table / CSF profile."""
    data = load_allowlist(path)
    out: dict[str, int] = {s: 0 for s in _POSTURE_STATES}
    for h in data["harnesses"]:
        out[h["posture_status"]] += 1
    return {"total": len(data["harnesses"]), "by_status": out,
            "verified_ids": [h["id"] for h in data["harnesses"]
                             if h["posture_status"] == "VERIFIED"]}
