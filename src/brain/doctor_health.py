"""Liveness and invariant doctor checks (embedder, heartbeat, drift, capture)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

def check_embedder_liveness() -> dict:
    """Probe whether the LIVE runtime can produce real semantic embeddings, or
    would silently degrade to the non-semantic HashEmbedder (DV-03, 2026-07-09).

    This is the one health surface older `brain doctor` was structurally blind
    to: version / schema / staging / model-files can ALL read green while
    `search` returns random results because onnxruntime isn't importable in the
    interpreter that actually runs `brain` (the exact Cowork-VM failure that
    lost a retrieval eval to a hash fallback). Note this is distinct from
    ``check_vm_model_cache`` — the model files can be present on disk yet the
    runtime still unable to load them. The cheap import probe stays read-only;
    when it passes, we then exercise a REAL 1-token query embed (a model load,
    no vault/index side effects) so "available" cannot false-green — the exact
    Cowork-VM gap where onnxruntime imported, model files were present, yet
    query-embed died because the model dir wasn't found."""
    from .embed import probe_auto_embedder

    surface = "Semantic embedder (live runtime)"
    state, backend = probe_auto_embedder()
    if state == "real":
        # The import probe only proves onnxruntime/tokenizers load — NOT that the
        # model resolves and embeds. Run an actual query embed to verify.
        try:
            from .embed import get_embedder

            vec = get_embedder("onnx").embed("probe", is_query=True)
            if not vec:
                raise RuntimeError("embed returned an empty vector")
            return _row(surface, CURRENT,
                        f"verified — a live query embed succeeded ({backend}, dim={len(vec)})",
                        raw={"state": state, "backend": backend, "probe_dim": len(vec)})
        except Exception as exc:
            return _row(surface, STALE,
                        f"onnxruntime imports but a REAL query embed FAILED "
                        f"({type(exc).__name__}: {exc}) — semantic search is dead "
                        f"despite the runtime looking present",
                        remediation="set $BRAIN_MODEL_CACHE to the staged model dir "
                                    "(.brain/model with onnx/model.onnx + tokenizer.json) "
                                    "or run `brain warmup`; then re-run `brain doctor`",
                        raw={"state": state, "backend": backend,
                             "error": f"{type(exc).__name__}: {exc}"})
    if state == "explicit-hash":
        # Deliberate offline/test choice — never gates, never alarms.
        return _row(surface, UNMANAGED,
                    "hash embedder selected explicitly ($BRAIN_EMBEDDER=hash) — "
                    "retrieval is non-semantic BY CHOICE, not a fault",
                    raw={"state": state, "backend": backend})
    # implicit-hash — the silent random-search failure. Gate the exit code.
    return _row(surface, STALE,
                "NO real semantic embedder — the auto-path would fall back to the "
                "non-semantic HashEmbedder, so `search` ranks with RANDOM vectors "
                "against a real-model index",
                remediation="install onnxruntime + tokenizers into the interpreter that "
                            "runs `brain` (the 'corporate' extras), or invoke the "
                            "onnxruntime-bundled frozen binary; then re-run `brain status`",
                raw={"state": state, "backend": backend})


def check_vm_maintain_heartbeat(vault: Path) -> dict:
    """VM-readable mirror of ``BrainCore._maintain_heartbeat_summary`` (the VM
    can read the heartbeat file even though only the host ever runs
    ``brain maintain``)."""
    import datetime as _dt

    from . import config

    from . import maintenance as maint

    surface = "Maintain heartbeat (.brain/maintain-state.json)"
    state = _read_json(config.maintain_state_path(vault))
    if not state:
        return _row(surface, NOT_DETECTABLE,
                    "no maintain-state.json yet — brain maintain (host-only ritual) has not run")
    today = _dt.date.today()
    stale, repeated = [], []
    escalated: list[str] = []  # ES-01: liveness + stuck-writer-lock, see maintenance.branch_escalation
    for branch, entry in state.items():
        if str(branch).startswith("_") or not isinstance(entry, dict):
            continue  # marker (e.g. "_retention"), not a branch
        last_run = entry.get("last_run")
        age_hours: Optional[float] = None
        if last_run:
            try:
                age_hours = (today - _dt.date.fromisoformat(last_run)).days * 24
            except ValueError:
                age_hours = None
        if branch == "daily" and (entry.get("failed") or (age_hours is not None and age_hours > 48)):
            stale.append(branch)
        # UNCHANGED gate: >=2 consecutive failures (a PULL surface stays
        # cheaply noisy at the pre-existing threshold — never raised to 3).
        if int(entry.get("consecutive_failures", 0) or 0) >= 2:
            repeated.append(branch)
        # ES-01 ADDITION: liveness (stale last_run) and a leaked writer-lock
        # skip streak — neither is visible to the consecutive_failures counter
        # above, because a process that never runs never increments it.
        esc = maint.branch_escalation(branch, entry, today)
        if esc["escalate"]:
            escalated.append(f"{branch} ({'; '.join(esc['reasons'])})")
    if stale:
        return _row(surface, STALE, f"stale branch(es): {stale}",
                    remediation="brain maintain runs host-side only — check the host's nightly scheduler")
    if repeated:
        return _row(surface, STALE, f"repeated-failure branch(es): {repeated}",
                    remediation="check the host's nightly maintenance logs")
    if escalated:
        return _row(surface, STALE, f"escalated branch(es): {escalated}",
                    remediation="check the host's nightly maintenance logs / a stuck writer lock")
    return _row(surface, CURRENT, f"{len(state)} branch(es) tracked, none stale/repeatedly-failing")


def check_corpus_invariants(vault: Path) -> dict:
    """WAT-01 dead-man's switch, lane 1: is the corpus-invariants watchdog
    actually alive, and is anything regressing?

    A dead fold cannot report its own death, so this reads the persisted
    maintain-state row from OUTSIDE the nightly — `brain doctor` is run ad
    hoc, by `brain health-report`, and by the weekly synthesis watchdog.
    STALE (the gating status) when the row is missing on a vault whose other
    branches run, when it has gone older than
    ``$BRAIN_INVARIANTS_MAX_AGE_DAYS`` (default 3), or when the last run
    recorded a regression past a metric's ratcheted floor."""
    from . import config
    from . import invariants as inv

    surface = "Corpus invariants watchdog (WAT-01)"
    state = _read_json(config.maintain_state_path(vault))
    if not state:
        return _row(surface, NOT_DETECTABLE,
                    "no maintain-state.json yet — brain maintain has not run here")
    live = inv.liveness_finding(state)
    if live:
        return _row(surface, STALE, live[1],
                    remediation="brain maintain   # then re-check; if the row stays "
                                "missing, this engine build predates WAT-01 — restage it",
                    raw={"age_days": inv.invariants_age_days(state),
                         "max_age_days": inv.max_age_days()})
    regs = inv.state_regressions(state)
    if regs:
        return _row(surface, STALE,
                    "; ".join(str(r.get("summary")) for r in regs),
                    remediation="brain health-report   # 'Corpus invariants' section",
                    raw={"regressions": regs})
    entry = state.get(inv.STATE_KEY) if isinstance(state.get(inv.STATE_KEY), dict) else {}
    metrics = entry.get("metrics") if isinstance(entry.get("metrics"), dict) else {}
    values = inv.metric_values(metrics)

    def _fmt(k: str, v: Any) -> str:
        # F2 (2026-08-18): a READ-not-recomputed metric must not sit under the
        # fold's own "last run <today>" stamp as if measured today — suffix
        # its own measurement date (`unreachable_gold=54@2026-08-10`).
        m = metrics.get(k) if isinstance(metrics.get(k), dict) else {}
        gen = str(m.get("generated") or "")[:10]
        return f"{k}={v}@{gen}" if gen else f"{k}={v}"

    return _row(surface, CURRENT,
                f"last run {entry.get('last_run', '?')}, no regression "
                f"({', '.join(_fmt(k, v) for k, v in sorted(values.items())) or 'no values yet'})",
                raw={"values": values, "floors": entry.get("floors")})


def check_audit_content_drift(vault: Path) -> dict:
    """INT-02: notes whose bytes changed after the audit chain signed them.

    HOST surface, and deliberately key-free: ``content_drift`` only re-hashes
    files against hashes already in the log, so this row costs one vault read
    and never resolves the signing key (``verify-audit`` does that separately).

    Only UNEXPLAINED drift gates. Drift a human triaged into the disposition
    file is reported in the detail and subtracted from the verdict — pinned to
    the bytes it was ruled on, so the same file changing again comes straight
    back as unexplained. That file lives in the HOST-PRIVATE app-data dir since
    2026-08-07 (``config.audit_drift_dispositions_path``); on the old
    ``.brain/`` path a Cowork VM could write it and zero out this very row."""
    from . import audit as _audit
    from . import config

    surface = "Audit content drift (signed notes vs disk)"
    log_path = config.index_dir(vault) / "audit_chain.jsonl"
    if not log_path.is_file():
        return _row(surface, NOT_DETECTABLE, f"no audit chain at {log_path}")
    try:
        summary = _audit.drift_summary(Path(vault), _audit.AuditChain(log_path))
    except Exception as exc:  # noqa: BLE001 — an unreadable chain is a surface gap, not a crash
        return _row(surface, UNKNOWN, f"could not check drift: {type(exc).__name__}: {exc}",
                    remediation="brain verify-audit --check-content --json")
    total, unexplained = summary["total"], summary["unexplained"]
    explained = total - unexplained
    if unexplained:
        return _row(
            surface, STALE,
            f"{unexplained} signed note(s) changed after signing with no recorded "
            f"disposition ({explained} triaged, {total} total)",
            remediation="brain verify-audit --check-content --json  # then triage into "
                        "the host-private disposition file (brain doctor --json shows "
                        "its path) or restore the note",
            raw={"total": total, "unexplained": unexplained})
    detail = ("no drift — every signed note matches its signed bytes" if not total
              else f"0 unexplained ({explained} triaged historical drift record(s))")
    return _row(surface, CURRENT, detail, raw={"total": total, "unexplained": 0})


#: Quarantine buckets whose cause is an operator action on THIS host, not a
#: judgement call about the file. Named in the row so the fix is one step away.
_RECOVERABLE_BUCKETS = {
    "pdf_no_text_layer": "scanned PDF — needs the local OCR engine",
    "empty_or_low_text_density": "no text found — often a picture-only deck, needs OCR",
    "pdf_encrypted": "needs a password (a permissions-only file now opens by itself)",
}


def check_ingest_capability() -> dict:
    """Can this host actually READ the formats dropped into `inbox/`?

    A missing handler or a missing local OCR engine does not fail loudly at
    ingest time — the file is quarantined and the drop zone looks empty. This
    row makes the capability visible BEFORE anything is dropped, on a new
    vault as much as an old one."""
    from .ingest.handlers import capability_report
    from .ingest.handlers.base import ocr_lang

    surface = "Ingestion capability (handlers + local OCR)"
    try:
        caps = capability_report()
    except Exception as exc:  # noqa: BLE001 — a probe failure is a surface gap
        return _row(surface, UNKNOWN, f"could not probe handlers: {type(exc).__name__}: {exc}")
    missing = sorted({c["dependency"] for c in caps.values() if not c["available"]})
    lang = ocr_lang()
    raw = {"handlers_missing": missing, "ocr_languages": lang}
    if missing:
        return _row(surface, STALE,
                    f"{len(missing)} extraction dependency missing: {', '.join(missing)} — "
                    "files of those types will be quarantined, not ingested",
                    remediation=f"pip install {' '.join(missing)}  # into the engine's venv",
                    raw=raw)
    if lang is None:
        # Reported on every run, never silent — but NOT gating. OCR is an
        # optional LOCAL engine, so a host without it is unconfigured, not
        # broken, and gating here would paint every fresh install red before
        # a single document is dropped. What gates is real loss: the
        # "Quarantined drops" row above fires the moment a scan is actually
        # refused, and names this engine as the remedy.
        return _row(surface, UNMANAGED,
                    "no local OCR engine — a scanned PDF or a picture-only deck "
                    "cannot be read, and would be quarantined instead of ingested",
                    remediation="brew install tesseract tesseract-lang  # or the distro "
                                "package; then `pip install pytesseract` into the "
                                "engine's venv",
                    raw=raw)
    return _row(surface, CURRENT,
                f"every handler available; local OCR reads {lang}", raw=raw)


def check_quarantine(vault: Path) -> dict:
    """Documents the owner dropped in and did NOT get.

    A quarantined file is not in the vault and is not retrievable, and until
    2026-08-17 nothing said so within the month it happened. Any live item
    gates this row; anything filed under a hand-triaged ``_resolved/`` subtree
    is a decision already taken and is reported separately, never as debt."""
    surface = "Quarantined drops (dropped in, not ingested)"
    qdir = Path(vault) / "inbox" / "_quarantine"
    if not qdir.is_dir():
        return _row(surface, CURRENT, "nothing quarantined", raw={"live": 0})
    live: dict[str, int] = {}
    resolved = 0
    try:
        for f in qdir.rglob("*"):
            if not f.is_file() or f.name.endswith(".reason.txt"):
                continue
            if f.name in {".DS_Store", "DISPOSITION.md"} or f.name.endswith(".RESOLVED.txt"):
                continue
            if any(p.name.startswith("_resolved") for p in f.relative_to(qdir).parents):
                resolved += 1
                continue
            live[f.relative_to(qdir).parts[0]] = live.get(f.relative_to(qdir).parts[0], 0) + 1
    except OSError as exc:
        return _row(surface, UNKNOWN, f"could not read {qdir}: {exc}")
    total = sum(live.values())
    raw = {"live": total, "by_reason": live, "triaged": resolved}
    if not total:
        detail = "nothing quarantined"
        if resolved:
            detail += f" ({resolved} item(s) hand-triaged under _resolved/)"
        return _row(surface, CURRENT, detail, raw=raw)
    parts = ", ".join(f"{n}x {r}" for r, n in sorted(live.items(), key=lambda kv: -kv[1]))
    hints = [f"`{r}`: {_RECOVERABLE_BUCKETS[r]}" for r in live if r in _RECOVERABLE_BUCKETS]
    return _row(surface, STALE,
                f"{total} dropped file(s) never reached the vault — {parts}",
                remediation=("; ".join(hints) + "; " if hints else "")
                            + f"inspect {qdir} and its .reason.txt sidecars, fix the "
                              "cause, then move each file back to `inbox/` and run "
                              "`brain sync`",
                raw=raw)


def check_query_capture(vault: Path) -> dict:
    """Host-only ADR-0008 S04 ledger liveness without reading query content.

    ``querylog.status`` uses file metadata and bounded line counts only.  It
    returns before resolving the host location on a VM, but this check is
    intentionally wired only into the host doctor surface: a VM must neither
    read nor claim to verify the raw-query ledger.
    """
    from . import querylog

    surface = "Host query capture ledger"
    info = querylog.status(vault, role="host")
    state = info.get("state")
    ledger = info.get("ledger") if isinstance(info.get("ledger"), dict) else {}
    failures = int(info.get("failures", 0) or 0)
    consecutive = int(info.get("consecutive_failures", 0) or 0)
    if state == "disabled":
        return _row(surface, UNMANAGED,
                    "capture disabled by BRAIN_QUERY_CAPTURE_ENABLED=0",
                    raw={"capture": info})
    if state in {"error", "stale"} or consecutive:
        reason = info.get("reason") or info.get("last_failure_code") or state
        # TWO faults, ONE instruction until 2026-08-17: `stale` means only "no
        # host query in N days" (an idle vault says it of itself), while
        # `error`/failures mean capture BROKE. Sending an idle vault's owner
        # after containment/permissions is a hunt for a fault that is not there.
        if state == "stale" and not failures and not consecutive:
            fix = (f"no host query captured for this vault in "
                   f"{info.get('stale_after_days')} day(s) — this is inactivity, "
                   "NOT a broken ledger. Run a host retrieval query against it, "
                   "or raise $BRAIN_QUERY_CAPTURE_STALE_DAYS if it is meant to "
                   "sit idle.")
        else:
            fix = ("fix host query-log containment/owner-only permissions, then run "
                   "three host retrieval queries; the VM never owns this ledger")
        return _row(
            surface, STALE,
            f"state={state}; files={ledger.get('files', 0)}; bytes={ledger.get('bytes', 0)}; "
            f"records={ledger.get('records', 0)}; "
            f"age_seconds={ledger.get('age_seconds')}; failures={failures}; reason={reason}",
            remediation=fix,
            raw={"capture": info},
        )
    if state == "idle":
        return _row(surface, NOT_DETECTABLE,
                    "no host query has been captured yet (no traffic to assess)",
                    raw={"capture": info})
    if state == "active":
        return _row(
            surface, CURRENT,
            f"files={ledger.get('files', 0)}; bytes={ledger.get('bytes', 0)}; "
            f"records={ledger.get('records', 0)}; age_seconds={ledger.get('age_seconds')}; "
            f"historical_failures={failures}",
            raw={"capture": info},
        )
    return _row(surface, UNKNOWN, f"unrecognised capture state: {state!r}", raw={"capture": info})


# Host-only surfaces the VM leg structurally cannot check (never gate, never
# claimed as checked — ADR-0005 Ruling 2/4: a NOT_DETECTABLE row here, not a
# fake-green or a crash).

# Parent-namespace binds, deferred past this module's own defs.
from .doctor import (  # noqa: E402
    CURRENT as CURRENT,
    NOT_DETECTABLE as NOT_DETECTABLE,
    STALE as STALE,
    UNMANAGED as UNMANAGED,
    UNKNOWN as UNKNOWN,
    _read_json as _read_json,
    _row as _row,
)
