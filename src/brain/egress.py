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

import contextvars as _contextvars
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
    "diagnose",        # per-target rank trace; target fields are gated separately
    "grep",            # lexical scan
    "bases-query",     # structured frontmatter view
    "graph-expand",    # wikilink-BFS + PPR discovery candidates
    "get",             # one note by id
    "read",            # alias of get
    "recent",          # recently-updated list
    # CUT-03 maintenance rituals that surface note-id+classification listings
    # (curate's unclassified-notes lint, integrity's near-dup pairs, promote-
    # scan's raw/ candidates) route through the SAME egress.apply_gate
    # chokepoint as the read verbs above — brain-cli-gaps.md G1 explicitly
    # requires both members of a near-dup pair to be gated before surfacing.
    "curate", "integrity", "promote-scan",
    # UX-02 summary surfaces (H-1) — brief/digest build their note list from
    # the SAME recent() feed as `recent` above, so they route through the
    # SAME apply_gate chokepoint before that list is assembled into the
    # brief/digest structure. See BrainCore.brief / BrainCore.digest.
    "brief", "digest",
    # GRF-01 (ADR-0003 Ruling 6/(a)) — graphify's INFERRED link candidates
    # name note ids the same way graph-expand's discovery candidates do;
    # gated the SAME way (both endpoints of a candidate pair must clear the
    # cap) before they reach the CLI output or a maintain hot-queue entry.
    "graphify",
)

# Host-broker / maintenance commands return STATUS, never note bodies — they are
# intentionally NOT gated by classification (nothing to leak) and so are NOT in
# the list above. Listed here for the audit so the split is explicit.
NON_CONTENT_SUBCOMMANDS: tuple[str, ...] = (
    "draft-capture", "rebuild", "sync", "snapshot", "status", "project",
    "write", "verify-audit", "audit-pubkey", "vm-egress-tier", "anchor",
    "verify-anchor", "backup", "restore",
    "check", "health", "maintain",
    # TMP-02: two note ids + a status/audit summary — no note bodies returned.
    "supersede",
    # SUI-02: per-client wiring status (diff/confirm/write report) — no note
    # bodies, no BrainCore construction at all. Host-only (refused at the
    # VM_ALLOWED gate in cli.py before this command ever dispatches).
    "connect",
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
    surfaced = flt.filter(items, key=key)
    _mark_content_trust(surfaced)
    report = flt.redaction_report(items, key=key)
    _tally(len(surfaced), int(report.get("withheld", 0) or 0))
    return surfaced, report


# --------------------------------------------------------------------------
# Read-volume tally (SEC-06). The chokepoint is the only place that knows HOW
# MUCH a command surfaced; the dispatch point is the only place that knows
# WHICH command and for whom. So the gate counts here and `brain.read_log`
# flushes once per invocation — which means a content verb written next year
# is covered the day it routes through apply_gate, exactly like the gate
# itself.
#
# A ContextVar, NOT a threading.local(). The MCP adapter serves calls
# concurrently, but its concurrency is ASYNCIO TASKS ON ONE THREAD: the
# low-level server does `tg.start_soon(self._handle_message, ...)` per incoming
# message (mcp 1.28.1, `mcp/server/lowlevel/server.py`). `threading.local()`
# gives ZERO isolation between coroutines on the same thread: two overlapping
# `tools/call` requests would share one tally, call B's reset zeroing call A's
# counts mid-flight, and whichever finished first consuming the merged counts
# while the other got `None` — a SEC-06 record reading `surfaced: 0` for a read
# that surfaced Restricted content.
#
# STATE IT IN THE RIGHT TENSE, because the difference decides what a later
# session may safely add: that corruption is LATENT TODAY, not observed. Every
# tool the seam registers is a sync `def`, and mcp 1.28.1 runs a sync body
# inline on the loop (`fastmcp/utilities/func_metadata.py:96`, `return
# fn(**arguments_parsed_dict)` — the `await` is line 94, the async branch), so
# there is no yield point anywhere between `mediate.__enter__` and
# `mediate.__exit__` and two tasks cannot interleave inside it. Measured
# 2026-08-28: with `threading.local()` restored, two REAL concurrent
# `call_tool` coroutines still recorded correctly. The first `async def` seam
# tool — or the first one offloaded to a thread — is what activates the hazard.
# The storage is fixed NOW rather than then because that later session will not
# be looking here, and because a ContextVar is per-TASK under asyncio/anyio
# (each task runs in its own copied Context) AND per-thread (a new thread starts
# from an empty Context), so it is strictly stronger than what it replaces at no
# cost. Written prospectively by closed-stacks S02 after adversarial review
# caught the original wording claiming an incident this surface cannot produce.
# --------------------------------------------------------------------------
_TALLY: _contextvars.ContextVar[dict[str, int] | None] = _contextvars.ContextVar(
    "brain_egress_tally", default=None,
)


def _tally(surfaced: int, withheld: int) -> None:
    cur = _TALLY.get()
    if cur is None:
        cur = {"gates": 0, "surfaced": 0, "withheld": 0}
        _TALLY.set(cur)
    cur["gates"] += 1
    cur["surfaced"] += surfaced
    cur["withheld"] += withheld


def gated() -> bool:
    """Has the gate fired in this invocation yet? Does NOT reset the tally.

    ``take_tally`` is destructive by design. The CLI needs a non-destructive
    read to know whether the output it is about to write is gated content
    (``brain.cli_read_record``), and consuming the tally there would empty it
    before the SEC-06 record is flushed.
    """
    return bool(_TALLY.get())


def take_tally() -> dict[str, int] | None:
    """Return and RESET the counts accumulated since the last take.

    ``None`` when nothing gated — a status verb that returned no note bodies
    has nothing to log, and logging a zero would bury the real reads.
    """
    cur = _TALLY.get()
    _TALLY.set(None)
    return cur


#: What a hit's ``zone`` means for the READER of that hit. ``vault/raw/`` is a
#: pile of documents other people wrote; ``vault/brain/`` is the owner's own
#: reasoning. Both look identical in a JSON result today, so a model reading a
#: source note has no signal that the text is DATA rather than instruction.
#: Marking it is the retrieval-time half of SEC-05 (the write-time half is
#: brain.injection_scan) and follows the MCP hardening consensus: "mark
#: untrusted tool output as untrusted".
#:
#: Deliberately NOT the ``provenance.trust`` frontmatter key: that key is the
#: capture stamp for DRAFTS, and the maintenance folds read ``untrusted`` there
#: as "a swept working memo". Overloading it would make every ingested source
#: look like a draft to the folds. This is a derived, read-only field.
TRUST_BY_ZONE: dict[str, str] = {
    "raw": "untrusted-source",
    "brain": "curated",
}
CONTENT_TRUST_KEY = "content_trust"


def _mark_content_trust(items: list[dict]) -> None:
    """Stamp each hit that carries a ``zone`` with what its zone implies.

    In place, and only where ``zone`` is known — the chokepoint also gates rows
    that are not notes at all (the deliverables census, COS people/companies,
    graph nodes), and inventing a trust level for those would be a lie.
    """
    for item in items:
        if not isinstance(item, dict) or CONTENT_TRUST_KEY in item:
            continue
        trust = TRUST_BY_ZONE.get(str(item.get("zone", "")).strip())
        if trust:
            item[CONTENT_TRUST_KEY] = trust


def gate_dossier_tensions(decisions: list[dict], surfaced_sources: list[dict]) -> None:
    """Re-gate each decision's ``tensions`` against the POST-EGRESS sources.

    ``core.dossier`` builds ``tensions`` from its UNGATED candidate pool
    (RET-10), so a decision that survives :func:`apply_gate` still names a
    withheld note's id/date/type/identity unless the list is re-filtered
    against the same surfaced set. It lives here, beside the chokepoint,
    because BOTH transports gate the two dossier layers separately and would
    otherwise each need their own copy of this rule.
    """
    surfaced_ids = {s["id"] for s in surfaced_sources}
    for d in decisions:
        d["tensions"] = [t for t in d.get("tensions", []) if t["id"] in surfaced_ids]


# --------------------------------------------------------------------------
# Trusted-harness allowlist (SEC-01, HARDENED:claude / r2-claude)
# --------------------------------------------------------------------------
# Reconciles openness vs control. "Openness" does NOT mean "any app" — it means
# any harness that PASSES the vendor-posture bar (no-train/ZDR scope covering
# tool-call/API egress of vault content + MNPI). The bar is owned here as a
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


def posture_summary(path: Path | None = None) -> dict[str, Any]:
    """Counts by posture_status for the evidence table / CSF profile."""
    data = load_allowlist(path)
    out: dict[str, int] = {s: 0 for s in _POSTURE_STATES}
    for h in data["harnesses"]:
        out[h["posture_status"]] += 1
    return {"total": len(data["harnesses"]), "by_status": out,
            "verified_ids": [h["id"] for h in data["harnesses"]
                             if h["posture_status"] == "VERIFIED"]}
