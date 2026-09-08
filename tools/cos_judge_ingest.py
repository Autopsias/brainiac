"""INGEST-01 — the `ingest` field: relevance and content lane, decided from SUBSTANCE alone.

WHY THIS FILE EXISTS. Until now "does this thread get ingested?" had no field
of its own: the bridge read `disposition == "candidate"` off the ledger and
every other lane read the bucket, the archive claim or the draft. Nothing said,
in one place, that ingestion is INDEPENDENT of what the porter DOES with the
thread — archive it, draft a reply to it, or hold it. A rule with no field is a
rule that holds only while nobody writes the branch that breaks it.

Measured on run 188 (2026-08-24, `_cos_ingestion_ledger_2026-08-24-run188.jsonl`
joined to `_cos_ingest_bridge_2026-08-24-run188.jsonl`): 44 `act` threads and 13
`read` threads dropped, and all 5 threads that carried a reply draft reached the
bridge. So the independence held on that night — this file is what makes it
CHECKABLE rather than true by luck.

THE FIELD IS DERIVED, NEVER ACCEPTED, exactly like `hold_category`. The model is
never offered `ingest` and cannot claim one; `cos_judge_verdicts._prepared_verdict`
attaches it and `cos_judge_rules._r_ingest` RECOMPUTES it and refuses a
disagreement — belt 1 of the three (`.._r_ingest` here, the planner's mark-lane
blindness check in `cos_mutate_plan_marks`, and `cos_runverify_checks
.check_ingest_independence` over the artifact alone).

ponytail: the content lane is not re-decided here. `choice_for_candidate` is
the ONE definition (owner ruling 2026-08-23: a lane is a FLOOR on what a
candidate ingests, never a ceiling) and it is imported, not restated — a second
copy of that rule is the "one rule, one rumour" state the bridge's own comments
already argue against.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# THE `tools.` PATH, exactly as `cos_ingest_bridge` and its two siblings import
# it. Bare (`import cos_ingest_bridge_content`) would bind a SECOND module
# object for the same file, and two import paths for one module is a defect
# that only shows up in suite order.
from tools.cos_ingest_bridge_content import (  # noqa: E402  the ONE definition
    CONTENT_ATTACHMENTS, CONTENT_BOTH, CONTENT_TEXT, choice_for_candidate)

#: The closed content vocabulary. `None` is legal only on a non-relevant row.
CONTENT_LANES = (CONTENT_TEXT, CONTENT_ATTACHMENTS, CONTENT_BOTH)

#: The `ingest` of a row no verdict reached, and of a row whose staging pass
#: found no substance: not relevant, no lane, and SAID SO. An absent field is
#: what commit f270700 shipped — 110 read-but-unowed threads with no lane at
#: all — so "no lane" is written down rather than inferred from a missing key.
NOT_RELEVANT: dict[str, Any] = {"relevant": False, "content": None}


def ingest_field(v: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """The explicit `ingest` for ONE verdict.

    RELEVANCE IS THE STAGING PASS'S SUBSTANCE ANSWER AND NOTHING ELSE. It reads
    `disposition` — "did rule 2 find a decision, a commitment, a counterparty
    position or a key number, with a span to prove it" — and it reads no bucket,
    no `auto_archive`, no `noise_signal` and no draft. Those four are what the
    porter DOES with the thread, and an archived thread's substance is worth
    exactly what a held thread's is.
    """
    if v.get("disposition") != "candidate":
        return dict(NOT_RELEVANT)
    row = {"category": v.get("category"),
           "attachments": ctx.get("attachments") or []}
    return {"relevant": True,
            "content": choice_for_candidate(str(v.get("category") or ""),
                                            ctx.get("taxonomy_doc") or {}, row)}


def ingest_relevant(row: dict[str, Any]) -> bool:
    """The BRIDGE'S ONE candidate predicate, over a ledger row.

    Reads the explicit field where the row carries one. A row that does not —
    a ledger written before INGEST-01, which the bridge is still asked to
    replay — falls back to the disposition it was written under. The fallback
    is named and bounded rather than silent: a FRESH run whose rows carry no
    `ingest` is caught by `cos_runverify_checks.check_ingest_independence`,
    which fails the run instead of quietly ingesting on the old rule.
    """
    ing = row.get("ingest")
    if isinstance(ing, dict):
        return ing.get("relevant") is True
    return row.get("disposition") == "candidate"


def ingest_content(row: dict[str, Any]) -> str | None:
    """The content lane the judge named for this row, or None (pre-INGEST-01)."""
    ing = row.get("ingest")
    return ing.get("content") if isinstance(ing, dict) else None
