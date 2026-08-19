"""The judgment-side grounding facts of `cos_judge` — the per-leg grounded/used counts (batch-2 drain).

Moved verbatim out of `cos_judge` and re-imported by it, so
`cos_judge.grounding_facts`, `load_categories` and `mark_candidates` keep their
module path.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_judge_batches import batch_membership             # noqa: E402


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



def mechanical_disposition(row: dict[str, Any]) -> dict[str, Any] | None:
    """What the DRIVER's own facts already settle — never a judgment.

    Rule 1½ gives every "could not read it" case its own reason, and each of
    them is a fact about the pass, not an opinion about the mail. Deciding them
    in code is the point of the split: the model is never asked a question whose
    answer is already in the ledger.
    """
    if row.get("body_opened"):
        return None
    # RULE 1¾'s PAIRING, written from a driver fact. `category_gate_excluded`
    # says this row was held OUT of the draw because the pre-draw category batch
    # stamped it with an id the owner dispositions `never`. The judgment was the
    # category; this is only its bookkeeping, and asking the model to restate it
    # would be asking for a verdict already on disk. Checked FIRST: an excluded
    # row is also an unopened row, and `over-cap` would be a lie about why.
    if row.get("category_gate_excluded"):
        return {"disposition": "no-substance", "held_reason": "never-category",
                "dedup_check": "not-run"}
    if row.get("read_state") != "read":
        return {"disposition": "held", "held_reason": "unread-read-state-invariant",
                "dedup_check": "not-run"}
    return {"disposition": "held", "held_reason": "over-cap", "dedup_check": "not-run"}


def mark_candidates(accepted: dict[str, dict[str, Any]],
                    ctx_by_id: dict[str, dict[str, Any]]) -> None:
    """Record, on every staged candidate, whether a proposal was actually dropped.

    THIS IS NOT A STAMP, AND THE PREVIOUS VERSION WAS THE WRONG FIX (review
    2026-08-13, round 2). `check_candidate_stamps` has required a `proposal_id`
    on every candidate row since v5.39 and `staging.candidate_stamps` requires
    it plus a digest, and nothing has ever produced either — 10 rows on run 126,
    10 on 129, 12 on 130, each scoring the run INVALID on the same line. The
    round-1 answer was to MINT them here: `f"{run_id}-c{short(cid)}"` and a
    sha256 of the evidence span.

    Both keys are the wrong ones. The join they exist to serve
    (`src/brain/cos/`) keys on the id `brain cos-propose --json` RETURNS for a
    drop and on that proposal's own content digest — neither of which this
    nightly produces, because this nightly drops NO PROPOSAL AT ALL
    (`load_night` sets `proposals_dropped: False`). So the mint moved
    `check_candidate_stamps` from a truthful FAIL to a PASS naming a proposal
    that does not exist, and the day the proposal lane is wired the join would
    answer `no-ledger-row` or `digest-mismatch` and quarantine every candidate.
    An attribution check must never be satisfied with a key that cannot
    attribute.

    So the honest producer writes the FACT instead: this run dropped nothing.
    `staging.candidate_stamps` already reads that fact off the context and skips
    (`_r_stamps`), and `check_candidate_stamps` now reads it off the ledger row
    and reports the control INAPPLICABLE rather than green. When a real drop
    lane exists, the stamps come from ITS returned id and digest, written where
    the drop happens — not here.
    """
    for cid, v in accepted.items():
        if v.get("disposition") != "candidate":
            continue
        v["proposals_dropped"] = bool(
            (ctx_by_id.get(cid) or {}).get("proposals_dropped"))


def _used_block_vocab(cids: list[str], blocks: dict[str, Any],
                      ctx_by_id: dict[str, dict[str, Any]],
                      answers: dict[str, Any]) -> int:
    """Rows that were HANDED vault content and whose answer carries a word from
    it that the mail did not already have.

    WHY THIS NUMBER EXISTS (review 2026-08-15, HIGH). Every rule GRD-03 added
    runs one way: `overlap_hit` REFUSES a verdict that reproduces five
    consecutive words of a context block. Nothing counted the opposite failure —
    a leg handed 258 blocks that read none of them — so the exact thing the
    change exists to prevent left no trace on any artifact, and s06 could not
    tell "the leg used the context" from "the leg ignored it". This is that
    number. It does not gate.

    ITS CEILING, STATED. It is a LOWER BOUND on use, not a measure of it, and
    the error runs in THREE directions, all of them downward:

      1. a leg that used a block and paraphrased it completely shares no
         two-token run and is not counted;
      2. a leg that echoes one distinctive phrase without reasoning from it is;
      3. and — the direction the first version of this docstring omitted — the
         PROJECTION REFUSES outright any row sharing a FIVE-token run with its
         block (`project_row`'s `refused_grounding_overlap`), so the most
         strongly grounded rows never reach this counter at all. The two
         mechanisms read the same shingle space at widths 2 and 5 with OPPOSITE
         consequences: quoting a little is what this counts, quoting a lot is
         what gets the row thrown away. That is why E10 renders
         `refused_grounding_overlap` on the same sentence — without it, a night
         that refused every quoting row is indistinguishable from a night that
         ignored the vault.

    The block's own shingles are subtracted against the row's subject, sender and
    body first, so a phrase the mail already carried never counts. Read it as a
    floor that should not be ZERO on a night with `with_content > 0` — the
    dead-subsystem signal — never as a per-row score.
    """
    cma = _answer_mod()
    if cma is None:
        return 0
    n = 0
    for cid in cids:
        entry = blocks.get(cid) or {}
        if entry.get("status") != "ok":
            continue
        ctx = ctx_by_id.get(cid) or {}
        own = "\n".join(str(ctx.get(k) or "")
                        for k in ("subject", "sender", "text"))
        uniq = cma.block_shingles(str(entry.get("text") or ""), own,
                                  cma.USE_SHINGLE_W)
        answer = answers.get(cid)
        if not uniq or not isinstance(answer, dict):
            continue
        said = cma.shingles(json.dumps(answer, ensure_ascii=False),
                            cma.USE_SHINGLE_W)
        if said & uniq:
            n += 1
    return n


def _answer_mod():
    """`cos_model_answer`, the module that owns the ONE tokenizer. Lazy, and
    `None` rather than a raise if it cannot be loaded: this is a reported
    number, and a judged night must not die because a counter is unavailable."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import cos_model_answer                                  # noqa: PLC0415
        return cos_model_answer
    except Exception:                                            # noqa: BLE001
        return None


def grounding_facts(rows: list[dict[str, Any]],
                    ctx_by_id: dict[str, dict[str, Any]],
                    grounding: Path | None,
                    chunks_dir: Path | None,
                    answers: dict[str, Any] | None = None) -> dict[str, Any]:
    """GROUNDED-VS-UNGROUNDED ROWS, COUNTED PER LEG — the run fact E10 derives
    from (design D5/D7a, GRD-03).

    A run-level `state` word says whether the fetcher was happy. It does not say
    which of the four legs actually received context, and "the draft leg judged
    27 rows with 3 blocks between them" is exactly the shape a state word hides.
    So each leg's population is recomputed from `batch_membership` — the SAME
    function the batches and the fetcher render from — and joined to the map's
    own per-conversation status.

    `covered` counts a `no-vault-content` block too: "the vault knows nothing
    here" IS a grounded answer, and it is the `lookup-failed` rows that are not.
    """
    facts: dict[str, Any] = {"state": None, "reason": "",
                             "legs": {}, "projection": {}}
    answers = answers or {}
    blocks: dict[str, Any] = {}
    if grounding and grounding.exists():
        try:
            payload = json.loads(grounding.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        facts["state"] = payload.get("state")
        facts["reason"] = payload.get("reason", "")
        blocks = payload.get("blocks") or {}
    membership = batch_membership(rows, ctx_by_id)
    for leg, cids in membership.items():
        grounded = sum(1 for c in cids
                       if (blocks.get(c) or {}).get("status")
                       in ("ok", "no-vault-content"))
        with_content = sum(1 for c in cids
                           if (blocks.get(c) or {}).get("status") == "ok")
        facts["legs"][leg] = {
            "rows": len(cids), "grounded": grounded,
            "ungrounded": len(cids) - grounded,
            "with_content": with_content,
            # NAMED FOR WHAT IT IS (review 2026-08-15). It shipped as
            # `used_block_vocab`, and E10 rendered a bare `(used N)` — a
            # number at the report boundary reads as measured usage while the
            # implementation says plainly it is a floor. The field name now
            # carries the qualification wherever it travels.
            "used_block_vocab_lower_bound": _used_block_vocab(
                cids, blocks, ctx_by_id, answers)}
    if chunks_dir and chunks_dir.is_dir():
        # The projection's own counts, summed over the chunks. A rule that starts
        # refusing everything is then ONE number rather than a quietly emptied
        # draft lane.
        agg: dict[str, Any] = {"rows_in": 0, "rows_out": 0, "refused_shape": 0,
                               "refused_oversize_row": 0,
                               "refused_unenumerated_id": 0,
                               "dropped_unknown_keys": {},
                               "refused_grounding_overlap": {},
                               "refused_oversize_field": {}}
        for f in sorted(chunks_dir.glob("chunk-*/projection.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            agg["rows_in"] += d.get("rows_in", 0)
            agg["rows_out"] += d.get("rows_out", 0)
            for scalar in ("refused_shape", "refused_oversize_row",
                           "refused_unenumerated_id"):
                agg[scalar] += d.get(scalar, 0)
            for key in ("dropped_unknown_keys", "refused_grounding_overlap",
                        "refused_oversize_field"):
                for k, v in (d.get(key) or {}).items():
                    agg[key][k] = agg[key].get(k, 0) + v
        facts["projection"] = agg
    return facts
