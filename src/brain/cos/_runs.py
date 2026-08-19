"""COS run-record operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._facade import public
from ._io import _write_atomic
from ._layout import _ts, _utcnow, host_dir, shared_dir

def runs_dir(vault=None) -> Path:
    """Run manifests, validity verdicts and plan bindings — OFF THE MOUNT.

    Host-private by PLACEMENT since gap-05 (2026-08-16), not by contract: this
    returned ``<vault>/.brain/cos/host/runs`` until then, inside the VirtioFS
    workspace, while this docstring and two others said "never VM-writable".
    ``config.cos_run_records_dir`` carries the reasoning and the fallback;
    :func:`migrate_run_records` carries the historical records forward.

    RESOLUTION ONLY — it does not create the directory, for the same reason
    ``config.host_lock_dir`` does not: a name lookup that materialises host
    state runs on every read path that merely wanted the name."""
    return config.cos_run_records_dir(vault)

def legacy_runs_dir(vault=None) -> Path:
    """Where run records lived until 2026-08-16: ON the Cowork mount.

    Read by :func:`migrate_run_records` (once) and by
    :func:`run_record_intruders` (forever, as EVIDENCE). Nothing else may
    consult it again — a reader here is a VM-writable authority, which is the
    whole defect gap-05 closed."""
    return host_dir(vault) / "runs"

def run_ops_dir(vault=None) -> Path:
    """Where the RUN writes its own artifacts — ``<vault>/cos-ops``.

    VM-writable by nature (the run authors its report, its ledgers and its
    metrics row there), which is exactly why nothing read from it is treated as
    authority: the ledger join below makes the category TAMPER-EVIDENT and
    single-sourced, not host-authoritative."""
    return config.vault_root(vault) / "cos-ops"

def run_manifest_path(vault, run_id: str) -> Path:
    return runs_dir(vault) / f"{_checked_run_id(run_id)}.json"

def run_validity_path(vault, run_id: str) -> Path:
    return runs_dir(vault) / f"{_checked_run_id(run_id)}.validity.json"

def run_plan_binding_path(vault, run_id: str) -> Path:
    """Where the APPLY records WHICH plan it dispatched — ONE name, host-private.

    The writer (`tools/cos_mutate.plan_binding_path`) and the reader
    (`cos_runverify.check_plan_binding`) each spelled this literal for
    themselves until round 7. A name held in two files is not one fact: the
    round-6 move out of the VM-writable `cos-ops` zone had to be made twice,
    and a third caller would have had to guess. It sits beside the manifest and
    the validity verdict because those are what the validator already trusts.

    NOT `_checked_run_id`, unlike the two siblings above, and deliberately: this
    file is the PER-RUN ARTIFACT pair of `_cos_undo_ledger_<run>.jsonl`, which
    `run_ops_dir` composes with the same unchecked id. Rejecting a run id here
    that the ledger path accepts would make one artifact of a run reachable and
    the other not — and the guard would be theatre anyway while its sibling is
    open. The id is checked where it is MINTED (`write_run_manifest`).
    """
    return runs_dir(vault) / f"_cos_plan_binding_{run_id}.json"

def _checked_run_id(run_id: Any) -> str:
    rid = str(run_id or "").strip()
    if not RUN_ID_RE.match(rid):
        raise ValueError(
            f"run id must look like <YYYY-MM-DD>-run<N>, got {rid!r}")
    # The regex accepts an UNBOUNDED digit suffix, and `next_run_id` derives
    # that suffix from VM-writable `cos-ops` directory names.
    if len(rid.encode("utf-8")) > MAX_SLUG_BYTES:
        raise ValueError(
            f"run id {rid[:40]!r}… is {len(rid.encode('utf-8'))} encoded bytes, "
            f"over the {MAX_SLUG_BYTES}-byte path-component limit")
    return rid

def next_run_id(vault, now: _dt.datetime | None = None) -> str:
    """``<today>-run<N+1>``, N being the highest run number on disk.

    The run number is a monotonic counter across the whole deployment (run 59
    followed run 58 on the same day), so it is derived from every artifact that
    names one — the run's own ops dir AND the manifests already written."""
    now = now or _utcnow()
    highest = 0
    for d in (run_ops_dir(vault), runs_dir(vault)):
        try:
            names = [p.name for p in d.iterdir()]
        except OSError:
            continue
        for name in names:
            m = _RUN_NUMBER_RE.search(name)
            # An absurd run number in a VM-writable dir name would make the
            # host CHOOSE an id it then cannot write. Ignore it rather than
            # inherit it — the counter is ours, the directory names are not.
            if m and len(m.group(1)) <= MAX_RUN_DIGITS:
                highest = max(highest, int(m.group(1)))
    return f"{now.strftime('%Y-%m-%d')}-run{highest + 1}"

def current_run_path(vault=None) -> Path:
    """VM-READABLE pointer at the run id the host assigned (id + start only)."""
    return shared_dir(vault) / "current-run.json"

def record_run_validity(vault, run_id: str, verdict: str, *, reason: str = "",
                        detail: Any = None,
                        ts: str | None = None) -> dict[str, Any]:
    """Record the host validator's verdict for ONE run (INS-01 / s03 writes it).

    Re-validation is legitimate (a run's artifact set can complete after a
    first look), so this OVERWRITES — the verdict of record is the newest one,
    and the claim gate re-reads it on every hourly pass."""
    if verdict not in RUN_VERDICTS:
        raise ValueError(f"verdict must be one of {RUN_VERDICTS}, got {verdict!r}")
    rid = _checked_run_id(run_id)
    rec: dict[str, Any] = {"run_id": rid, "verdict": verdict,
                           "reason": reason, "recorded": ts or _ts()}
    if detail is not None:
        rec["detail"] = detail
    p = run_validity_path(vault, rid)
    p.parent.mkdir(parents=True, exist_ok=True)
    # ATOMIC: the claim gate re-reads this file on every hourly pass, and a torn
    # read is indistinguishable from "no verdict recorded" — which would silently
    # quarantine a valid run's candidates.
    public("_write_atomic")(p, (json.dumps(rec, sort_keys=True) + "\n").encode("utf-8"))
    return rec

def run_validity(vault, run_id: Any) -> dict[str, Any]:
    """The host validator's verdict for ``run_id`` — ABSENT MEANS INCONCLUSIVE."""
    try:
        p = run_validity_path(vault, run_id)
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rec = None
    if isinstance(rec, dict) and rec.get("verdict") in RUN_VERDICTS:
        return rec
    return {"run_id": str(run_id), "verdict": RUN_INCONCLUSIVE, "recorded": None,
            "reason": "no host validation verdict recorded for this run — an "
                      "unvalidated run is INCONCLUSIVE, and INCONCLUSIVE never "
                      "permits claiming (there is no unvalidated interim)"}

__all__ = ['runs_dir', 'legacy_runs_dir', 'run_ops_dir', 'run_manifest_path', 'run_validity_path', 'run_plan_binding_path', '_checked_run_id', 'next_run_id', 'current_run_path', 'record_run_validity', 'run_validity']
