"""PEN 3's HOST HALF — join the sent replies to the drafts the porter wrote.

`brain.cos.feedback_sent` decides what a draft-to-sent diff MEANS and files it.
This module is the driver-side join that hands it its two inputs, and it is
separate for the same reason `cos_driver_enumeration.owner_reversals` is: the
library must not know where a run keeps its ledgers.

    which conversations did this lane draft on?   the undo ledger, verb `draft`
    what text did it draft?                       `_cos_drafts_pending_<run>.jsonl`

THE GATE IS THE UNDO LEDGER, NOT THE MAILBOX. A body is fetched only for a sent
message whose conversation carries a LANDED porter draft, so the extra reads are
bounded by what the porter itself did — never by how much the owner sent.

AN UNREACHABLE RECORD DEGRADES THIS PEN, IT DOES NOT KILL THE NIGHT. Same
contract as Pen 2, and for the same reason: `feedback_dir` refuses outright when
`$BRAIN_INDEX_DIR` puts the host-private base back inside a VM-visible root, and
a configuration fact must not take a mailbox read down with it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

#: How many sent bodies one night may fetch. The draw's own body budget is 20
#: for a ~235-thread inbox; a night drafts single digits, so this is a ceiling
#: nothing is expected to reach rather than a quota to spend.
SENT_BODY_CAP = 10


def _ledger_rows(vault: Path) -> list[dict[str, Any]]:
    from brain import cos                                        # noqa: PLC0415
    rows: list[dict[str, Any]] = []
    for p in sorted(cos.run_ops_dir(vault).glob("_cos_undo_ledger_*.jsonl")):
        for line in cos._read_nofollow(p).decode("utf-8", "replace").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    return rows


def porter_drafts(vault: Path) -> dict[str, str]:
    """`{conversation_id: draft text}` for every LANDED porter draft.

    Latest-wins per conversation through `feedback_cli.applied_mutations`, which
    folds on `action_ts` and never on file name — `run99` sorts after `run124`
    inside one day, and that has bitten two other lanes already. The text comes
    from the pending-draft ledger of the run that mutation names, so a
    conversation drafted three times is compared against the draft that is
    actually sitting in the mailbox.
    """
    from brain import cos                                        # noqa: PLC0415
    from brain.cos import feedback_cli                           # noqa: PLC0415

    landed = feedback_cli.applied_mutations(_ledger_rows(vault), verbs=("draft",))
    by_run: dict[str, set[str]] = {}
    for (cid, _verb), row in landed.items():
        by_run.setdefault(str(row.get("run") or ""), set()).add(cid)
    out: dict[str, str] = {}
    ops = cos.run_ops_dir(vault)
    for run, cids in by_run.items():
        p = ops / f"_cos_drafts_pending_{run}.jsonl"
        if not run or not p.exists():
            continue
        for line in cos._read_nofollow(p).decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            cid = str(row.get("conversation_id") or "")
            if cid in cids and str(row.get("text") or "").strip():
                out[cid] = str(row["text"])
    return out


def sent_body_convs(vault: Path) -> list[str]:
    """The conversation ids whose sent bodies this night may fetch.

    NEVER RAISES. This is an optional input to an optional phase, computed
    before the mailbox is read; a feedback lane that could stop a night from
    enumerating would be a worse defect than the one it closes. An empty list
    leaves the phase off, which is exactly what every night before this did.
    """
    try:
        return sorted(porter_drafts(vault))
    except Exception:                                        # noqa: BLE001
        return []


def sent_draft_feedback(vault: Path, run_id: str,
                        capture: dict[str, Any]) -> dict[str, Any]:
    """PEN 3, run over one night's capture. Records nothing it cannot read."""
    from brain import config                                     # noqa: PLC0415
    from brain.cos import feedback_sent                          # noqa: PLC0415

    bodies = capture.get("sent_bodies") or []
    if not bodies:
        return {"state": "no-sent-bodies", "recorded": 0, "rows": [],
                "skipped": [], "classes": {}}
    try:
        return feedback_sent.record_sent_diffs(
            vault, bodies, porter_drafts(vault), run=run_id)
    except (config.HostPathUnsafe, OSError) as exc:
        return {"state": "unreachable", "detail": f"{type(exc).__name__}: {exc}",
                "recorded": 0, "rows": [], "skipped": [], "classes": {}}
