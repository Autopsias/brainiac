"""The night reader and writer of `cos_judge` — `load_night`, `write_night` (batch-2 drain).

Moved verbatim out of `cos_judge` and re-imported by it: the tests monkeypatch
`cos_judge.load_night` / `cos_judge.write_night`, and the parent-side callers
(`judge_night`, `main`) resolve both through the parent's globals at CALL time,
so those patches keep governing exactly as before.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cos_signals                                              # noqa: E402
from cos_judge_brief import render_png  # noqa: E402
from cos_judge_rules import _age_days  # noqa: E402

# ---------------------------------------------------------------------------
# a night: batches out, verdicts in
# ---------------------------------------------------------------------------
def _ledger(vault: Path, run_id: str) -> Path:
    from brain import cos                                        # noqa: PLC0415
    return cos.run_ops_dir(vault) / f"_cos_ingestion_ledger_{run_id}.jsonl"


def load_categories(path: Path | None) -> dict[str, str]:
    """The pre-draw category batch's answer, `{conversation_id: category}`.

    ONE PARSER, IMPORTED, AND IT RAISES. `cos_driver.load_categories` holds the
    only schema; a second copy here is how the driver and the judge come to
    disagree about which rows were excluded, which is the difference between an
    armed gate and a decorative one. A malformed file raises out of here rather
    than degrading to `{}`: the nightly validates the answer BEFORE either leg
    is handed `--categories`, so a file that reaches this point and does not
    parse means the two legs were given different inputs.
    """
    if path is None:
        return {}
    import cos_driver                                             # noqa: PLC0415
    return cos_driver.load_categories(path)


def load_night(vault: Path, run_id: str,
               categories: dict[str, str] | None = None) -> dict[str, Any]:
    """The driver's own output, plus the per-row context the rules need.

    `typed_fields_available` is the load-bearing field. A row whose sender and
    subject this run never persisted CANNOT be triaged — Phase 1.5 judges from
    typed fields and nothing else (INJ-03) — so it is reported as such rather
    than bucketed from its timestamp.
    """
    from brain import cos, cos_corpus                            # noqa: PLC0415

    rows = [json.loads(x) for x in
            _ledger(vault, run_id).read_text(encoding="utf-8").splitlines() if x.strip()]
    corpus = {r["conversation_id"]: r for r in cos_corpus.read_corpus(vault, run_id)}
    taxonomy = (cos.ingest_taxonomy(vault) or {}).get("rules") or {}
    # DERIVED, NOT DECLARED (review 2026-08-13, round 2, K2). This was the
    # literal `False` below, so nothing in production could ever make it True —
    # and `check_candidate_stamps` short-circuits on it BEFORE inspecting a
    # single proposal id or digest, which means the day a real drop lane exists,
    # a producer that forgets to flip it hides duplicate ids and digest
    # mismatches behind "does not apply". `cos.run_proposal_drops` is the ONE
    # definition of the fact — the host's own pending metas and quarantined
    # claims for this run — and the verifier reads the same one, so the two legs
    # cannot disagree about the same night.
    proposals_dropped = cos.run_proposal_drops(vault, run_id) > 0
    # GAP-04 (s11, 2026-08-16). The five context facts below used to be written
    # by NOTHING but a test fixture, so every rule that reads them graded a
    # night against a permanent `False`. `cos_signals` is their HOST producer:
    # it reads this run's own ledger rows and capture-corpus text and never
    # touches a model answer. The spine is read ONCE per night, not per row.
    signal_now = cos_signals.run_date(run_id)
    signal_commitments = cos_signals.open_commitments(vault)
    senders: dict[str, int] = {}
    for c in corpus.values():
        s = (c.get("provenance") or {}).get("sender")
        if s:
            senders[s] = senders.get(s, 0) + 1
    ctx_by_id = {}
    for row in rows:
        cid = row["conversation_id"]
        c = corpus.get(cid) or {}
        prov = c.get("provenance") or {}
        text = c.get("text") or ""
        ctx_by_id[cid] = {
            "sender": prov.get("sender"),
            "subject": prov.get("subject"),
            "typed_fields_available": bool(prov.get("subject") or prov.get("sender")),
            "read_state": row.get("read_state"),
            # ONLY THE UNAMBIGUOUS CHIP ASSERTS A TIER (DOCTRINE v7 §4.1).
            # `P3 · Read` is written for `read`/P2, `read`/P3 AND `act`/P3, so
            # feeding it to `triage.tier_vocabulary` as "this thread IS P3"
            # would reject tonight's honest `read`/P2 verdict as contradicting
            # a chip that never claimed a tier. The driver stamps which kind of
            # chip the tier came from (`cos_driver._tier_source`); anything
            # else — including a row from a pre-v7 ledger, which carries the
            # priority-chip source verbatim — behaves exactly as before.
            "chip_tier": (row.get("tier")
                          if row.get("tier_source") != "outlook-read-chip"
                          else None),
            "priority_map": {},
            "body_opened": bool(row.get("body_opened")),
            "body_chars": int(row.get("body_chars") or 0),
            "text": text,
            "text_len": len(text),
            "taxonomy": taxonomy,
            "sender_rows_this_run": senders.get(prov.get("sender"), 0),
            # The PRE-DRAW stamp, carried through as an input to every later
            # batch and as the verdict field itself. Absent (`None`) when no
            # category batch ran, which is the honest feature-off shape.
            "category": (categories or {}).get(cid) or None,
            "drafts_inventory": [],
            "cos_draft_convids": [],
            "proposals_dropped": proposals_dropped,
            "ask_age_days": _age_days(row.get("received")),
        }
        ctx_by_id[cid].update(cos_signals.signals_for_row(
            row, c, now=signal_now, open_commitments=signal_commitments))
    return {"rows": rows, "corpus": corpus, "taxonomy": taxonomy,
            "ctx_by_id": ctx_by_id}





def _short(value: str) -> str:
    """A conversation id as its 16-hex SHA-256 prefix.

    Evidence written into this repository never carries a mailbox id, not even a
    fragment: set equality and per-row attribution are fully checkable from the
    digest, and this tree is a public-export source (s01/s02 precedent).
    """
    import hashlib                                               # noqa: PLC0415
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _now_iso() -> str:
    import datetime as _dt                                       # noqa: PLC0415
    return _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
