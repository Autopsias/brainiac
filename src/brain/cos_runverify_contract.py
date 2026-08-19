"""Contract re-execution, plan-binding, candidate-stamp, and degrade checks."""
from __future__ import annotations

import sys
from typing import Any

from . import cos
from . import cos_runverify_planbinding as planbinding
from . import cos_runverify_stamps as stamps

_DROP_STAMP_KEYS = ("proposal_id", "id", "content_sha256", "proposal_sha256",
                    "sha256")


def _drop_stamped(row: dict[str, Any]) -> list[str]:
    return [k for k in _DROP_STAMP_KEYS if str(row.get(k) or "").strip()]


def _cos_mutate():
    """The `tools/cos_mutate.py` module, loaded like the other run checkers, or
    ``None`` when the toolchain is not on disk beside the engine.

    `check_plan_binding` re-hashes the frozen plan the binding names, and the
    ONE definition of that hash is `plan_digest`/`load_frozen_plan` in
    `cos_mutate` — imported, never restated (the engine holds one notion of a
    plan's identity, ENF-04). An absent toolchain means the join cannot be
    re-run, and the caller scores the binding INCONCLUSIVE, never PASS."""
    d = _rv.tools_dir()
    if d is None:
        return None
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))
    try:
        return _load_script(d, "cos_mutate")
    except Exception:                                     # noqa: BLE001
        return None


def check_plan_binding(vault, run_id: str) -> dict[str, Any]:
    """Did the apply dispatch the FROZEN plan, or one it built for itself?

    K1's whole point is that the plan a human-gated rehearsal validated is the
    plan the mailbox receives. `apply_pass` records which of the two it did in
    `plan_binding.source` — and until round 5 (2026-08-13) that field was
    written and read by NOTHING: `rebuilt-by-the-apply` scored exactly the same
    as `frozen`, on every report, forever. A field no verdict reads is a
    comment.

    NO VM-WRITABLE FILE CAN BUY A PASS, OR SKIP THE CHECK. The binding lives
    host-private (`config.cos_run_records_dir`, off the VirtioFS mount since
    gap-05, 2026-08-16) and this control re-JOINS its digest to the frozen
    plan it names — read-to-convict is fine, read-to-acquit is the defect.
    This control does read two VM-writable artifacts — the undo ledger's
    presence, and its rows for the dispatch join — and neither can move the
    verdict toward PASS. The residual, stated rather than papered over:
    REMOVING rows from the undo ledger can hide an unbound dispatch from the
    join; what it cannot do is turn this control off, and `unledgered_mutations`
    and E1 both bite on the same emptying.

    FAIL, not DEGRADE, and that is a deliberate call. `rebuilt-by-the-apply`
    means real mutations reached a real mailbox under a plan no rehearsal ever
    covered — the exact condition the frozen artifact exists to make
    impossible, and the CLI already refuses. A run that reached it did so
    through a path that should not exist, and a DEGRADED verdict still lets its
    candidates be claimed.

    A run that dispatched NOTHING is not scored on this: there was no apply, so
    there is no binding to have, and a read-only night is the ordinary night.
    The UNDO ledger is what says so — it is written only by the mutation lane,
    one row per state transition, and it is absent on every read-only night.
    The ACTION ledger is not: it carries opens and navigations and exists on
    nights nothing mutated, so keying this on it would have demanded a plan
    binding from runs that never applied one.

    The stages and their full round-history prose (round 5-7: H-forge,
    H-disarm, the gap-05 mount move) live in
    :mod:`brain.cos_runverify_planbinding` (s18); the lane module's own
    callables are handed over so their definitions stay single.
    """
    binding_p = cos.run_plan_binding_path(vault, run_id)
    ledger_p = cos.run_ops_dir(vault) / f"_cos_undo_ledger_{run_id}.jsonl"
    row = planbinding._applicability_row(
        vault, run_id, binding_p, ledger_p, mutation_counts=mutation_counts)
    if row:
        return row
    row, doc = planbinding._binding_doc_row(binding_p)
    if row:
        return row
    return planbinding._join_row(
        vault, run_id, doc, ledger_p, mutation_counts=mutation_counts,
        cos_mutate=_cos_mutate, read_jsonl=_read_jsonl)


def check_candidate_stamps(vault, run_id: str,
                           rows: list[dict[str, Any]]) -> dict[str, Any]:
    """E16's stamp clause, checked where v5.39 put it: the LEDGER.

    The host joins a claimed drop back to its producing run by proposal id AND
    full content digest, so a candidate row with no id, a duplicate id, a
    malformed digest or an invented category is a candidate the host either
    cannot attribute or must refuse.

    AND A RUN THAT DROPPED NO PROPOSAL CANNOT BE CHECKED (review 2026-08-13,
    round 2). The two stamps are read OFF A DROP: the id is the one
    `brain cos-propose --json` returns and the digest is that proposal's own
    content digest, which is what `cos.py`'s claim-time join keys on. A run that
    staged candidates but dropped nothing has neither, and the round-1 answer —
    mint an id from the run and hash the evidence span — produced keys the join
    does not use, turning a truthful FAIL into a PASS naming a proposal that
    does not exist. So a candidate row that says so (`proposals_dropped: false`,
    the same fact `staging.candidate_stamps` skips on) makes this control
    DEGRADED-and-inapplicable rather than green: the run is still valid, and the
    missing producer stays visible on every report until the lane exists.
    """
    candidates = [r for r in rows if r.get("disposition") == "candidate"]
    if not candidates:
        return _row("candidate_stamps", PASS,
                    "no candidate rows in this run's ledger — nothing to stamp",
                    reexecuted=True)
    # A ROW THE BRIDGE SETTLED WITHOUT A DROP IS A LEGITIMATE STAMP-LESS SHAPE
    # (s03 change 2): quarantined, never-category and duplicate-of rows have
    # no proposal to attribute, and demanding an id from them turned one odd
    # thread into RUN_INVALID — stranding the night's genuinely good drops.
    # THE EXEMPTION IS KEYED ON THE HOST-PRIVATE SETTLEMENT RECORD (attempt
    # 13): the row's own settlement fields are VM-writable ledger claims, so
    # a claim no `cos.bridge_settlements` record backs stays fully scored,
    # and a row carrying ANY drop stamp is never settled either
    # (`stamps.bridge_settled`).
    settlements = cos.bridge_settlements(vault, run_id)
    settled: dict[str, int] = {}
    live: list[dict[str, Any]] = []
    for r in candidates:
        why = stamps.bridge_settled(r, drop_stamped=_drop_stamped,
                                    settlements=settlements)
        if why is None:
            live.append(r)
        else:
            settled[why] = settled.get(why, 0) + 1
    unbacked = sum(1 for r in live
                   if not _drop_stamped(r) and stamps.settlement_claim(r))
    unbacked_note = ("" if not unbacked else
                     f"; {unbacked} row(s) claim a bridge settlement no "
                     "host-private settlement record backs — the claim is a "
                     "VM-writable ledger field and earns no exemption "
                     "(scored in full; records are count-bound, each "
                     "exempting at most one row)")
    settled_note = ("" if not settled else
                    "; " + str(sum(settled.values())) + " row(s) settled by "
                    "the bridge without a drop ("
                    + ", ".join(f"{k}: {n}" for k, n in sorted(settled.items()))
                    + ") carry no stamps by design")
    if not live:
        return _row("candidate_stamps", DEGRADED,
                    f"{len(candidates)} candidate row(s), every one settled "
                    "by the bridge without a drop ("
                    + ", ".join(f"{k}: {n}" for k, n in sorted(settled.items()))
                    + ") — there is no proposal to attribute, so this control "
                    "does not apply. Not a pass: the settlements themselves "
                    "are answered for elsewhere (the bridge report, the "
                    "defect log and the claim-quarantine store)",
                    reexecuted=True)
    # The applicability decision and the per-row scoring live in
    # :mod:`brain.cos_runverify_stamps` (s18); this module's own callables and
    # vocabularies are handed over so their definitions stay single.
    row = stamps.applicability_row(
        live, vault, run_id, drop_stamped=_drop_stamped)
    if row is not None:
        row["detail"] += unbacked_note
        return row
    row = stamps.score_row(
        live, sha256_re=_SHA256_RE,
        placeholder_categories=_PLACEHOLDER_CATEGORIES)
    if settled_note and row.get("status") in (PASS, DEGRADED):
        row["detail"] += settled_note
    row["detail"] += unbacked_note
    return row


def degrade_evidence(vault, run_id: str, block: dict[str, Any] | None,
                     row: dict[str, Any] | None, rows: list[dict[str, Any]],
                     recon: Any) -> dict[str, Any]:
    """Is this run a TOTAL, INTERNALLY CONSISTENT degrade — or a run pretending?

    Every signal here except the mail-leg enumeration comes from a different
    artifact, and ``host_received`` comes from the HOST's own records. A degrade
    that is real shows up in all of them at once; a marker copied onto a run
    that did substantive work contradicts the rest."""
    ops = cos.run_ops_dir(vault)
    enumerated = (recon.mail_leg_enumerated(ops, run_id)[0]
                  if recon is not None else
                  len((block or {}).get("enumerated") or []))
    marker = [r for r in rows if r.get("disposition") == _MARKER_DISPOSITION]
    counted = ledger_counts(rows)
    reported = {k: int((row or {}).get(k) or 0) for k in counted}
    host_received = host_received_candidates(vault, run_id)

    total = (enumerated == 0 and host_received == 0
             and all(v == 0 for v in counted.values())
             and all(v == 0 for v in reported.values()))

    inconsistencies: list[str] = []
    if marker:
        if counted["ingestion_candidates"] or counted["ingestion_in_scope"]:
            inconsistencies.append(
                f"a `{_MARKER_DISPOSITION}` degrade marker sits beside "
                f"{counted['ingestion_in_scope']} in-scope row(s) and "
                f"{counted['ingestion_candidates']} staged candidate(s) in the "
                "SAME ledger")
        if any(v for v in reported.values()):
            inconsistencies.append(
                f"a `{_MARKER_DISPOSITION}` degrade marker sits beside a metrics "
                f"row reporting {reported} ingestion work")
        if enumerated:
            inconsistencies.append(
                f"a `{_MARKER_DISPOSITION}` degrade marker sits beside a mail leg "
                f"that enumerated {enumerated} thread(s)")
        if host_received:
            inconsistencies.append(
                f"a `{_MARKER_DISPOSITION}` degrade marker sits beside "
                f"{host_received} candidate(s) the HOST actually received from "
                "this run")
    return {"total": total, "inconsistencies": inconsistencies,
            "enumerated": enumerated, "host_received": host_received,
            "ledger_counts": counted, "reported_counts": reported,
            "marker_rows": len(marker)}


def check_degrade_consistency(evidence: dict[str, Any]) -> dict[str, Any]:
    if evidence["inconsistencies"]:
        return _row("degrade_consistency", FAIL,
                    "the run claims a degrade its own artifacts contradict — "
                    + "; ".join(evidence["inconsistencies"]),
                    reexecuted=True)
    if evidence["marker_rows"] and evidence["total"]:
        return _row("degrade_consistency", PASS,
                    "the degrade is TOTAL and consistent across every artifact "
                    "(0 enumerated, 0 ledgered, 0 reported, 0 candidates "
                    "received by the host)",
                    reexecuted=True)
    return _row("degrade_consistency", PASS,
                "no degrade marker to corroborate", reexecuted=True)


def check_contract(vault, run_id: str, contract: Any, recon: Any,
                   evidence: dict[str, Any]) -> tuple[dict[str, Any], dict | None]:
    """(d) The contract verdict is present, checker-produced, and RE-DERIVABLE.

    This is the one control that fully re-executes: the same deterministic
    checker, over the same raw PRE/POST snapshots and ledgers the run handed it,
    compared against the block the run recorded. A block that does not survive
    re-execution was not produced by the checker over those artifacts, whatever
    `verdict_source` it carries."""
    ops = cos.run_ops_dir(vault)
    block_path = ops / f"cos_contract_block_{run_id}.json"
    block = _load_json(block_path)
    if not isinstance(block, dict):
        return _row("contract", FAIL,
                    f"no readable OUTCOME CONTRACT block at {block_path.name} — "
                    "the run recorded no verdict for the host to check",
                    reexecuted=False), None
    if str(block.get("verdict")) not in ("PASS", "FAILED"):
        return _row("contract", FAIL,
                    f"the contract block carries verdict {block.get('verdict')!r}, "
                    "which the checker never emits",
                    reexecuted=False), block
    source = str(block.get("verdict_source") or "")
    if not source.startswith("tools/cos_contract.py@"):
        return _row("contract", FAIL,
                    f"the contract block's verdict_source is {source!r} — a "
                    "hand-composed verdict, not one the checker produced",
                    reexecuted=False), block
    if contract is None:
        return _row("contract", INCONCLUSIVE,
                    "the OUTCOME CONTRACT checker is not available host-side, so "
                    f"the recorded {block.get('verdict')} could not be "
                    "re-derived from the raw artifacts",
                    reexecuted=False), block

    pre = _load_json(ops / f"cos_contract_pre_{run_id}.json")
    post = _load_json(ops / f"cos_contract_post_{run_id}.json")
    if not isinstance(pre, dict) or not isinstance(post, dict):
        return _row("contract", DEGRADED,
                    "the run's raw PRE/POST snapshots are not both readable, so "
                    f"the recorded {block.get('verdict')} cannot be re-executed "
                    "— it is taken on the run's word",
                    reexecuted=False), block
    profile = (pre.get("run_profile") or block.get("run_profile") or "full")
    try:
        recomputed = contract.evaluate(pre, post, ops, _run_number(run_id), profile)
    except Exception as exc:                               # noqa: BLE001
        return _row("contract", FAIL,
                    f"the run's own contract inputs do not survive the checker "
                    f"({type(exc).__name__}: {exc}) — a block claiming "
                    f"{block.get('verdict')} over inputs the checker refuses is "
                    "not a verdict",
                    reexecuted=True), block

    if (recomputed["verdict"] != block.get("verdict")
            or sorted(recomputed["verdict_reasons"])
            != sorted(block.get("verdict_reasons") or [])):
        return _row("contract", FAIL,
                    f"re-executing the checker over this run's raw artifacts "
                    f"yields {recomputed['verdict']} "
                    f"({', '.join(recomputed['verdict_reasons']) or 'no clauses'}) "
                    f"but the recorded block says {block.get('verdict')} "
                    f"({', '.join(block.get('verdict_reasons') or []) or 'no clauses'})",
                    reexecuted=True), block

    if recomputed["verdict"] == "PASS":
        return _row("contract", PASS,
                    "the recorded PASS is reproduced exactly by re-executing the "
                    "checker over the run's raw PRE/POST snapshots and ledgers",
                    reexecuted=True), block
    clauses = ", ".join(recomputed["verdict_reasons"]) or "no clauses"
    if evidence["total"]:
        return _row("contract", DEGRADED,
                    f"the contract honestly FAILED ({clauses}) and the degrade is "
                    "TOTAL and consistent across every artifact — correctly "
                    "reported degradation, not a validator failure",
                    reexecuted=True), block
    return _row("contract", FAIL,
                f"the contract FAILED ({clauses}) on a run that did substantive "
                f"work — {evidence['enumerated']} thread(s) enumerated, "
                f"{evidence['ledger_counts']['ingestion_candidates']} candidate(s) "
                f"staged, {evidence['host_received']} received by the host. A "
                "failed contract is exempt only when the degrade is total",
                reexecuted=True), block


# -- the verdict ---------------------------------------------------------------

def _verdict_from(checks: list[dict[str, Any]]) -> tuple[str, str]:
    states = [c["status"] for c in checks]
    if FAIL in states:
        bad = [c for c in checks if c["status"] == FAIL]
        return cos.RUN_INVALID, "; ".join(
            f"{c['check']}: {c['detail']}" for c in bad)
    if INCONCLUSIVE in states:
        bad = [c for c in checks if c["status"] == INCONCLUSIVE]
        return cos.RUN_INCONCLUSIVE, "; ".join(
            f"{c['check']}: {c['detail']}" for c in bad)
    if DEGRADED in states:
        bad = [c for c in checks if c["status"] == DEGRADED]
        return cos.RUN_VALID_DEGRADED, "; ".join(
            f"{c['check']}: {c['detail']}" for c in bad)
    return cos.RUN_VALID, "every control re-executed clean"

# Parent/IO/touch binds, deferred past this module's own defs.
from .cos_runverify import (  # noqa: E402
    DEGRADED as DEGRADED,
    FAIL as FAIL,
    INCONCLUSIVE as INCONCLUSIVE,
    PASS as PASS,
    _MARKER_DISPOSITION as _MARKER_DISPOSITION,
    _PLACEHOLDER_CATEGORIES as _PLACEHOLDER_CATEGORIES,
    _SHA256_RE as _SHA256_RE,
    _row as _row,
)
from .cos_runverify_io import (  # noqa: E402
    _load_json as _load_json,
    _load_script as _load_script,
    _read_jsonl as _read_jsonl,
    _run_number as _run_number,
    host_received_candidates as host_received_candidates,
    ledger_counts as ledger_counts,
    tools_dir as tools_dir,
)
from .cos_runverify_touch import mutation_counts as mutation_counts  # noqa: E402
from . import cos_runverify as _rv  # noqa: E402
