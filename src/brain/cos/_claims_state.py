"""COS claim-state operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._facade import public
from ._attachment_store import _attachment_meta_path
from ._guards import _read_receipt_pairs
from ._io import _write_atomic
from ._layout import _ts, proposals_dir
from ._learning_ledger import log_defect
from ._runs import run_validity
from ._version_links import _version_link_pending

def claim_quarantine_dir(vault=None) -> Path:
    return proposals_dir(vault) / "claim-quarantine"

def quarantined_claims(vault) -> list[dict[str, Any]]:
    """Every candidate waiting on run attribution/validity, newest reason first."""
    return _read_receipt_pairs(claim_quarantine_dir(vault))[0]

def _quarantine_claim(vault, *, nid: str, text: str, sha: str, code: str,
                      reason: str, run_id: str | None, now: _dt.datetime,
                      source: str) -> dict[str, Any]:
    """Park ONE candidate with its reason. Idempotent per (id, reason code):
    the release path re-runs the gate hourly, and a defect row per retry would
    bury the real ones under thousands of copies of the same sentence."""
    qdir = claim_quarantine_dir(vault)
    qdir.mkdir(parents=True, exist_ok=True)
    meta_path = qdir / f"{nid}.json"
    try:
        prior = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prior = {}
    public("_write_atomic")(qdir / f"{nid}.md", text.encode("utf-8"))
    first = not (isinstance(prior, dict) and prior.get("code") == code
                 and prior.get("sha256") == sha)
    rec = {"id": nid, "sha256": sha, "code": code, "reason": scrub(reason),
           "run_id": run_id, "source": source,
           "quarantined": prior.get("quarantined") if not first else _ts(now),
           "last_checked": _ts(now), "first": first}
    public("_write_atomic")(meta_path, (json.dumps(rec, sort_keys=True) + "\n").encode("utf-8"))
    if first:
        log_defect(vault, f"claim-quarantined:{code}", f"{nid}: {reason}",
                   ts=_ts(now))
    return rec

def _quarantined(vault, *, nid: str | None, text: str, sha: str, code: str,
                 reason: str, run_id: str | None, now: _dt.datetime,
                 source: str) -> dict[str, Any]:
    ident = nid or ("unnamed-" + sha[:12])
    rec = _quarantine_claim(vault, nid=ident, text=text, sha=sha, code=code,
                            reason=reason, run_id=run_id, now=now, source=source)
    return {"state": "quarantined", "id": ident, "reason": reason,
            "quarantine": rec}

def _sweep_pending_without_valid_run(vault, now: _dt.datetime) -> list[dict[str, Any]]:
    """STA-02: candidates ALREADY in ``pending/`` whose run is not proven valid.

    Run 59 staged 8 candidates and skipped its entire self-eval, so s03 must be
    able to score it INVALID — and a verdict that does not reach the candidates
    is cosmetic. Re-stamping them would be worse: it would launder the output
    of a provably-uncontrolled run into the trusted pipeline, where an owner
    accept would sign it AND teach the graduation gate. So they leave
    ``pending/`` with the reason recorded, and the content is recovered by
    re-extraction on a run that passes validation.

    A candidate already sitting in an OPEN OWNER BATCH is swept too, on
    purpose: the batch's own proposal-level CAS then refuses to promote it
    (`pending file missing or content drifted`), which is exactly the outcome
    wanted — the owner's answer cannot sign material from an unvalidated run.
    """
    pending = proposals_dir(vault) / "pending"
    out: list[dict[str, Any]] = []
    for m in _pending_metas(vault):
        nid = str(m.get("id"))
        run_id = m.get("run_id")
        if run_id:
            verdict = run_validity(vault, run_id)
            reason = (f"run {run_id} is {verdict['verdict']}: "
                      f"{verdict.get('reason') or 'no reason recorded'}")
            code = f"run-{verdict['verdict'].lower()}"
        else:
            verdict = {"verdict": RUN_INCONCLUSIVE}
            code = "no-run-attribution"
            reason = ("bound before the host derived its own stamps (STA-01): "
                      "no run attribution, so the producing run's validity "
                      "cannot be checked. Recover the content by re-extraction "
                      "on a validated run — never by re-stamping this copy")
        if verdict["verdict"] in CLAIMABLE_VERDICTS:
            continue
        md = pending / f"{nid}.md"
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        rec = _quarantine_claim(vault, nid=nid, text=text,
                                sha=str(m.get("sha256") or sha256_text(text)),
                                code=code, reason=reason, run_id=run_id,
                                now=now, source="pending")
        md.unlink(missing_ok=True)
        (pending / f"{nid}.json").unlink(missing_ok=True)
        out.append(rec)
    return out

def _pending_metas(vault) -> list[dict[str, Any]]:
    return _read_receipt_pairs(proposals_dir(vault) / "pending")[0]

def _bound_meta(vault, nid: str, *, body: str = "") -> dict[str, Any]:
    """The HOST-bound sidecar for one candidate (proposal or attachment).

    Falls back to the candidate's own frontmatter ONLY for a legacy sidecar
    written before LRN-01 — a fresh claim always binds these host-side."""
    for path in (proposals_dir(vault) / "pending" / f"{nid}.json",
                 _attachment_meta_path(vault, nid),
                 _version_link_pending(vault) / f"{nid}.json"):
        try:
            m = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(m, dict) and m.get("id"):
            return m
    meta, _ = frontmatter.parse_text(body) if body else ({}, "")
    return {"id": nid, "lane": LANE_TEXT, "category": CATEGORY_UNCLASSIFIED,
            "pattern": meta.get("pattern"),
            "bundle_version": meta.get("bundle_version"),
            "kind": meta.get("kind")}

__all__ = ['claim_quarantine_dir', 'quarantined_claims', '_quarantine_claim', '_quarantined', '_sweep_pending_without_valid_run', '_pending_metas', '_bound_meta']
