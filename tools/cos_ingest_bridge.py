#!/usr/bin/env python3
"""The ingestion bridge: judged candidates -> cos-propose drops (ING-01/02/03).

The judge stages candidates in ``_cos_ingestion_ledger_<run>.jsonl`` and, until
this bridge, nothing picked them up — a night triaged and archived mail while
the vault gained none of it. One unsigned ``cos-propose`` drop per candidate
(HOST context) plus one ingest-manifest line per attachment, so the FILE
travels the existing acceptance lane instead of having its bytes pasted into a
note. The broker's one-open-batch gate and the signed drain are untouched.

Rules it cannot break: the LEDGER CARRIES NO TEXT, so each candidate joins its
cos-corpus row by conversation_id (CAP-01/02); BACKPRESSURE IS FATAL — one open
batch aborts the whole set and reports what it did not drop (E3/B4); TIERS ONLY
RISE, through the one host classifier; NEVER-CATEGORY rows drop zero.

THE COLLAPSE (attempt 14 — an owner-approved DESIGN COLLAPSE, superseding the
delivery designs of attempts 8-13 whole): THE BRIDGE NO LONGER DECIDES
DELIVERY. Six review rounds each found a way for the untrusted Cowork leg to
forge the "already delivered, skip it" verdict — drop-dir filenames, cos-ops
ledger stamps, mount-resident ``host/proposals`` custody, ledger row fields,
bare custody stems, and finally the outcomes/claims join behind a host-private
receipt. Every round closed the hole it was given and the next review found
the same class one field over. The CAPABILITY was the defect: a permissive
skip computed in a repo tool from state the untrusted side can mostly write is
not defensible field by field. So the verdict is deleted, not better-guarded:

- EVERY ELIGIBLE CANDIDATE IS PROPOSED, every pass — the bridge consults no
  recorded state to decide WHETHER to write its drop, and it still reads no
  delivery evidence (no receipts, no outcomes scans, no custody checks, no
  drop-dir reads). Nothing the mount holds can make it skip a candidate
  SILENTLY — the earlier "make it SKIP" absolute overstated this, and a
  confident wrong safety claim in this file has bitten twice: AFTER the
  drop is written, the bridge reads the claims ledger once (the exact set
  the engine's replay guard reads) and when the ledger has ALREADY CONSUMED
  those bytes the candidate is QUARANTINED with the reason, ident and
  digest (attempt 15), because the sweep will delete that drop unseen and
  exiting clean over it is the silent loss six rounds fought. Recorded
  state can therefore turn a doomed drop into a LOUD quarantine; it can
  never produce a skip, a "delivered" verdict, or bytes the bridge did not
  write. The other non-drop settlements stay decided from this pass's own
  input: the never-category rule, a same-pass duplicate row (same work
  only — rows diverging in category/classification/attachments quarantine
  as ambiguity instead of collapsing, attempt 15) and the content-defect
  quarantines below.
- DUPLICATE SUPPRESSION IS THE ENGINE'S JOB, through machinery it already
  owns: ``cos.propose`` writes atomically at the deterministic ident
  ``cosbridge-<run>-<conv-key>`` (a same-run re-drop lands ON TOP of any
  leftover — crash residue or forgery — so one conversation is one drop
  file), the claim sweep's replay guard rejects any drop whose exact bytes
  the claims ledger has already seen (the drop content is DETERMINISTIC per
  run — ``created`` derives from the run id's own date — so an idempotent
  re-run's drop is byte-identical and can never re-ask a claimed, rejected
  or expired question), and the one-open-batch gate holds everything else
  back. (``created`` AND ``updated`` both derive from the run id's date —
  an omitted ``updated`` let ``capture.enforce`` fill the wall-clock day
  and broke byte-identity across midnight, attempt 16.) OWNER-ACCEPTED
  COST, stated: a DIFFERENT run re-judging the same
  conversation proposes it again (its bytes differ, so the replay guard
  cannot bind them) and the owner may see a duplicate ask. A bounded
  duplicate ask is cheaper than a forgeable skip, which is silent loss of
  the owner's mail. NEVER reintroduce a tool-side "already delivered"
  check to reduce duplicates. The bridge only OBSERVES the replay guard's
  verdict over its own just-written bytes and reports it (the quarantine
  above) — it never routes around the guard, and a forged claims line now
  costs the attacker a visible quarantine (past the threshold, the whole
  run) instead of buying a silent skip. STATED RESIDUAL, the engine's, not
  ours: the claims ledger is mount-resident and unauthenticated, so a sha
  line forged AFTER this pass and before the sweep still suppresses the
  drop with nothing to observe it — filed as the separate engine plan.
- WHAT REMAINS in the host-private store (``cos.bridge_receipts_root``,
  INT-05, proven off every VM-visible root): the manifest WRITE-DEDUP
  records (a planted mount twin must not pre-empt the real manifest line,
  and a recorded-but-deleted line is re-written — attempts 11/13) and the
  engine's settlement records (``cos.record_bridge_settlement``), which
  back E16's stamp exemption for the three genuinely stamp-less row shapes.
  Neither is delivery evidence; neither can suppress a proposal.
- INCONSISTENT STATE FOR ONE CANDIDATE NO LONGER KILLS THE RUN. A content
  defect (a missing/unreadable corpus row, an empty or digest-mismatched
  corpus text, a file-carrying choice naming no filename) QUARANTINES that
  candidate — evidence
  parked through the engine's own claim-quarantine primitives, the reason on
  the ledger row and in the report — and the rest keep moving. The run fails
  loudly only when the quarantined-conversation count reaches
  ``$BRAIN_COS_BRIDGE_QUARANTINE_MAX`` (default 5): mass inconsistency is a
  different event from one odd thread.

Exit 0 ok (including quarantines under the threshold) · 3 backpressure abort,
NOTHING dropped · 4 the writer lock is busy (contention, NOTHING dropped,
re-run) · 5 refused (no ingestion ledger, or the quarantine threshold).
Nothing here touches a mailbox, signs, indexes or opens WAL. Full reasoning
and the known-positive probe transcript: `_evidence/backfill/s03-*`.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import json
import sys
from pathlib import Path

if __name__ == "__main__":  # tools/ bootstrap, same as every cos_* tool
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# `provenance` is bound here for its MODULE identity, not for a call: the
# suite patches `bridge.provenance.email_classification`, and the caller
# (`cos_ingest_bridge_candidate`) reads that attribute off the same shared
# `brain.provenance` object, so the patch still lands.
from brain import config, cos, cos_corpus, provenance  # noqa: E402,F401
from brain.lock import WriterLockBusy                 # noqa: E402

# -- the ratchet split (2026-08-20): four descriptive siblings, moved VERBATIM
# and re-imported so every name below keeps its `tools.cos_ingest_bridge`
# module path. The suite imports ~20 of these privates off this module, the
# nightly runs this file as a script, and nothing else in the repo imports the
# bridge at all — so the facade IS the contract. Imported AFTER the bootstrap
# above, which is what puts the repo root on `sys.path` for a script run.
from tools.cos_ingest_bridge_content import (  # noqa: E402,F401
    ANOMALY_PREFIX, CONTENT_ATTACHMENTS, CONTENT_BOTH, CONTENT_TEXT,
    DEFAULT_CONTENT_CHOICE, _anomaly_doc, _anomaly_nid, _attachment_names,
    _claims, _fm, _line_content_key, _LANE_TO_CHOICE, _manifest_line,
    _proposal_content, _run_day, content_choice_for)
from tools.cos_ingest_bridge_store import (  # noqa: E402,F401
    _SETTLEMENT_CLAIM_KEYS, _attachment_shape, _bridge_ident,
    _claim_settlement, _consumed_shas, _conv_key, _known_manifest_keys,
    _manifest_record_path, _receipts_ensure, _row_shape, _write_manifest_lines,
    bridge_ledger_path, receipts_root)
from tools.cos_ingest_bridge_candidate import (  # noqa: E402,F401
    _candidate_content, _clear_anomaly, _corpus_text, _decide_candidate,
    _execute_fresh_drop, _never_category, _quarantine_candidate,
    _same_run_duplicate)
from tools.cos_ingest_bridge_report import (  # noqa: E402,F401
    DEFAULT_QUARANTINE_MAX, EXIT_BACKPRESSURE, EXIT_OK, EXIT_REFUSED,
    EXIT_WRITER_BUSY, QUARANTINE_MAX_ENV, _finish_report, _observed_dropped,
    _quarantine_max, _recon, _update_metrics)

SCHEMA = "cos_ingest_bridge/v2"


class BridgeRefused(RuntimeError):
    """A fatal input condition. `bridge_run` turns it into a report; the
    ``reason`` slug rides that report so a refusal names its actual cause
    (a damaged ledger is not a missing one, attempt 16)."""

    def __init__(self, msg: str, *, reason: str = "no-ingestion-ledger"):
        super().__init__(msg)
        self.reason = reason


def _ledger_path(vault, run_id: str) -> Path:
    return cos.run_ops_dir(vault) / f"_cos_ingestion_ledger_{run_id}.jsonl"


def _read_ledger(vault, run_id: str) -> list[dict]:
    path = _ledger_path(vault, run_id)
    if not path.is_file():
        raise BridgeRefused(
            f"no ingestion ledger for {run_id} — the judgment pass writes it "
            "before anything can be bridged; run the judgment leg first")
    out: list[dict] = []
    for i, x in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not x.strip():
            continue
        # The ledger sits under `cos.run_ops_dir` — VM-writable, untrusted
        # INPUT (attempt 16, finding 5's principle). A torn line used to
        # escape as a raw ValueError and kill the nightly as a false
        # "refused". It IS a refusal — the primary input is damaged and a
        # skipped row would be a silently lost candidate — so refuse
        # HONESTLY, naming the line, through the report path.
        try:
            row = json.loads(x)
        except ValueError as exc:
            raise BridgeRefused(
                f"ingestion ledger line {i} for {run_id} is not parseable "
                f"JSON ({exc}) — the judgment ledger is damaged and a "
                "skipped row would be a silently lost candidate; repair the "
                "ledger (or re-run the judgment leg), then re-run. NOTHING "
                "was bridged", reason="ingestion-ledger-damaged") from exc
        if not isinstance(row, dict):
            raise BridgeRefused(
                f"ingestion ledger line {i} for {run_id} is not a JSON "
                "object — the judgment ledger is damaged; repair it (or "
                "re-run the judgment leg), then re-run. NOTHING was bridged",
                reason="ingestion-ledger-damaged")
        out.append(row)
    return out


def _rewrite_ledger(vault, run_id: str, rows: list[dict]) -> None:
    """Persist the ADD-ONLY stamps. Nothing renamed or repurposed — the six
    other readers of this ledger read known keys only."""
    cos._write_atomic(_ledger_path(vault, run_id), "".join(
        json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
        for r in rows).encode("utf-8"))


def bridge_run(vault, run_id: str, *, now: _dt.datetime | None = None,
               dry_run: bool = False) -> tuple[dict, int]:
    """Bridge one run's candidates -> (report, exit_code), on EVERY path
    including no-ledger: the code is derived from the report, so a caller
    reading only JSON still sees every count and every reason."""
    vault = Path(vault)
    now = now or _dt.datetime.now(_dt.timezone.utc)
    try:
        rows = _read_ledger(vault, run_id)
    except BridgeRefused as exc:
        return ({"schema": SCHEMA, "run": run_id, "status": "refused",
                 "vault": str(vault), "candidates": 0, "dropped": 0,
                 "manifest_lines": 0, "never": 0, "already_dropped": 0,
                 "quarantined": 0, "quarantines": [],
                 "refused": [{"conversation_id": None, "detail": str(exc),
                              "reason": exc.reason}],
                 "error": str(exc)}, EXIT_REFUSED)
    candidates = [r for r in rows if r.get("disposition") == "candidate"]

    report: dict = {"schema": SCHEMA, "run": run_id,
                    "dry_run": dry_run, "candidates": len(candidates),
                    "vault": str(vault)}

    # SINGLE-WRITER (CC-01/CC-02): `cos.claim_drops` takes this same lock on
    # the hourly fold. Unserialized, a claim landing between a drop and its
    # ledger stamp sees a drop no row joins — the candidate is quarantined and
    # goes invisible. A dry run writes nothing, so it never takes the lock.
    try:
        lock = (contextlib.nullcontext() if dry_run else
                cos.vault_writer_lock(vault, verb="cos-ingest-bridge"))
        with lock:
            return _bridge_locked(vault, run_id, rows, candidates, report,
                                  now=now, dry_run=dry_run)
    except config.HostPathUnsafe as exc:
        # The receipts root resolved INSIDE a VM-visible root (a misconfigured
        # $BRAIN_INDEX_DIR, or a symlink landing back on the mount). Delivery
        # can neither be trusted nor recorded there, so the run REFUSES —
        # never a fallback to another mount-side location (that design failed
        # three times) and never an escaped traceback.
        report.update({"status": "refused", "dropped": 0, "manifest_lines": 0,
                       "never": 0, "already_dropped": 0, "quarantined": 0,
                       "quarantines": [],
                       "candidates_not_dropped": len(candidates),
                       "refused": [{"conversation_id": None,
                                    "reason": "receipts-root-unsafe",
                                    "detail": str(exc)}],
                       "error": str(exc)})
        return report, EXIT_REFUSED
    except WriterLockBusy as exc:
        # NOT a refusal: the hourly `brain-nightly` rebuild legitimately holds
        # this lock for up to 90 minutes. Escaping as a traceback made the
        # nightly die 18 reporting "the bridge refused candidate(s)" — the one
        # message that sends an operator looking at the mail instead of at the
        # clock. Nothing was dropped; re-running is the whole fix.
        report.update({"status": "writer-busy", "dropped": 0,
                       "manifest_lines": 0, "never": 0, "already_dropped": 0,
                       "quarantined": 0, "quarantines": [],
                       "candidates_not_dropped": len(candidates),
                       "refused": [], "error": str(exc),
                       "reason": f"the vault writer lock is held ({exc}) — "
                       "NOTHING was dropped and nothing is wrong with the "
                       "candidates; re-run once the holder finishes"})
        return report, EXIT_WRITER_BUSY


def _bridge_locked(vault: Path, run_id: str, rows: list[dict],
                   candidates: list[dict], report: dict, *,
                   now: _dt.datetime, dry_run: bool) -> tuple[dict, int]:
    # -- 1. BACKPRESSURE FIRST — before a single drop is written (E3/B4) ----
    open_batches = cos.open_batches(vault)
    if open_batches:
        ids = ", ".join(str(b.get("batch_id")) for b in open_batches)
        report.update({
            "status": "backpressure-abort", "candidates_not_dropped": len(candidates),
            "open_batches": [b.get("batch_id") for b in open_batches],
            "dropped": 0, "manifest_lines": 0, "never": 0, "already_dropped": 0,
            "quarantined": 0, "quarantines": [],
            "reason": (f"a proposal batch is already open ({ids}) — the bridge "
                       f"ABORTED all {len(candidates)} candidate(s) rather than "
                       "queue behind it. Answer or expire the open batch, then "
                       "re-run; drops are never silently held back")})
        return report, EXIT_BACKPRESSURE

    # -- 2. taxonomy + corpus (the join the ledger cannot do alone) ----------
    # `log=` gated on the pass being real (attempt 10, finding 4): with a
    # malformed rule set, `log=True` reaches `log_defect`, which appends to
    # the host defects ledger — and a REHEARSAL mutating persistent state
    # (duplicating the same operational finding on every rehearsal) breaks
    # the one promise `--dry-run` makes. The parse verdict is identical
    # either way; only the recording is suppressed.
    taxonomy = cos.ingest_taxonomy(vault, log=not dry_run)
    corpus: dict[str, dict] = {}
    try:
        corpus = {str(r["conversation_id"]): r
                  for r in cos_corpus.read_corpus(vault, run_id)}
    except Exception as exc:                                   # noqa: BLE001
        # Unreadable and absent are the same for the join; naming it once here
        # keeps the report from saying "no corpus" when it means "unreadable".
        report["corpus_error"] = f"{type(exc).__name__}: {exc}"[:300]
    report["corpus_rows"] = len(corpus)

    mpath = cos.ingest_manifest_dir(vault) / f"manifest-{run_id}.jsonl"
    known_keys = _known_manifest_keys(vault, run_id, mpath)
    consumed = _consumed_shas(vault)
    outcomes: list[dict] = []
    handled: dict[str, dict] = {}   # conv key -> settled THIS pass
    pending = {"dirty": False}

    def flush() -> None:
        """Persist accumulated row stamps in ONE atomic rewrite. Called
        before each drop and once at pass end — not once per candidate, which
        rewrote every row N times (O(N²)). The crash window stays at most one
        drop-without-stamp: `_decide_candidate` flushes right before every
        `cos.propose`, so every EARLIER drop's stamp is on disk first, and the
        one unstamped drop is recovered by the next pass RE-DROPPING onto the
        same ident. Pending quarantine/never stamps a crash loses
        are re-derived identically on the re-run — no drop backs them, so no
        partial state."""
        if pending["dirty"]:
            _rewrite_ledger(vault, run_id, rows)
            pending["dirty"] = False

    for row in candidates:
        outcome = {"run": run_id, "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "conversation_id": str(row.get("conversation_id") or ""),
                   "category": str(row.get("category") or "").strip()}
        outcomes.append(outcome)
        before = None if dry_run else json.dumps(row, sort_keys=True)
        _decide_candidate(vault, run_id, row, outcome, taxonomy=taxonomy,
                          corpus=corpus, report=report, manifest_path=mpath,
                          known_keys=known_keys, handled=handled,
                          consumed=consumed, flush=flush, now=now,
                          dry_run=dry_run)
        if not dry_run:
            cos.run_ops_dir(vault).mkdir(parents=True, exist_ok=True)
            cos.append_jsonl(bridge_ledger_path(vault, run_id), outcome)
            if json.dumps(row, sort_keys=True) != before:
                pending["dirty"] = True
    flush()

    # -- 3. persist the metrics row ------------------------------------------
    if not dry_run:
        # the drops THIS pass wrote, keyed by conversation, with the sha
        # `cos.propose` returned — held in host memory, so the observation
        # below verifies the bridge's own writes without reading any
        # VM-writable record as evidence (attempt 11)
        written = {_conv_key(str(o.get("conversation_id") or "")):
                   {"ident": o.get("ident"), "sha256": o.get("proposal_sha256")}
                   for o in outcomes if o.get("outcome") == "dropped"}
        # `- written`: a conversation whose row 1 DROPPED (verified above)
        # while a diverging duplicate row quarantined (attempt 15) is a real
        # delivery of this pass — excluding it read `reported` below truth
        # and rang the shortfall on a night that delivered.
        report["metrics"] = _update_metrics(
            vault, run_id, now=now, candidates=candidates, written=written,
            excluded={_conv_key(str(o.get("conversation_id") or ""))
                      for o in outcomes
                      if o.get("outcome") in ("refused", "quarantined")
                      } - set(written))
    return _finish_report(report, outcomes)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--vault", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--json", action="store_true", dest="as_json")
    p.add_argument("--dry-run", action="store_true",
                   help="decide every candidate and report, write nothing")
    args = p.parse_args(argv)
    report, code = bridge_run(args.vault, args.run_id, dry_run=args.dry_run)
    if args.as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return code
    print(f"ingest bridge {args.run_id}: {report.get('status', '?')} — "
          f"{report.get('dropped', 0)} dropped, "
          f"{report.get('quarantined', 0)} quarantined, "
          f"{len(report.get('refused') or [])} refused, "
          f"{report.get('never', 0)} never, "
          f"{report.get('already_dropped', 0)} already dropped, "
          f"{report.get('manifest_lines', 0)} manifest line(s)")
    for r in report.get("refused") or []:
        print(f"  REFUSED {r.get('reason')}: {r.get('detail', '')}")
    for r in report.get("quarantines") or []:
        print(f"  QUARANTINED {r.get('reason')}: {r.get('detail', '')}")
    if report.get("error"):
        print(f"  ERROR {report['error']}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
