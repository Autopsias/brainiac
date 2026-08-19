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
import os
import sys
from pathlib import Path

if __name__ == "__main__":  # tools/ bootstrap, same as every cos_* tool
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from brain import config, cos, cos_corpus, provenance  # noqa: E402
from brain.lock import WriterLockBusy                 # noqa: E402
from brain.notes import safe_slug, sha256_text        # noqa: E402

EXIT_OK = 0
EXIT_BACKPRESSURE = 3
#: The hourly `brain-nightly` rebuild holds the same lock for up to 90 minutes.
#: That is CONTENTION, not a refused candidate, and it gets its own code so the
#: nightly reports it as what it is instead of dying 18 on an escaped traceback.
EXIT_WRITER_BUSY = 4
EXIT_REFUSED = 5

CONTENT_TEXT = "text"
CONTENT_ATTACHMENTS = "attachments"
CONTENT_BOTH = "both"

#: taxonomy rule `lane` -> content choice (owner rule 2026-08-17 #3).
_LANE_TO_CHOICE = {"text": CONTENT_TEXT, "attachment": CONTENT_ATTACHMENTS,
                   "both": CONTENT_BOTH}

#: Taxonomy absent/off or category unknown -> `text`: the corpus-backed lane,
#: the one whose missing input quarantines loudly instead of routing around.
DEFAULT_CONTENT_CHOICE = CONTENT_TEXT

SCHEMA = "cos_ingest_bridge/v2"

#: The run fails (exit 5) when this many DISTINCT conversations quarantined in
#: one pass. Default 5: the measured per-candidate anomalies of eight review
#: rounds were one or two threads a night, while a systemic cause (an
#: unreadable corpus, a lost manifest file) hits the whole candidate set — the
#: reference nights stage ~8+ candidates (run 59 staged 8) — so 5 lets the odd
#: thread through and still fails a night whose whole input is inconsistent.
QUARANTINE_MAX_ENV = "BRAIN_COS_BRIDGE_QUARANTINE_MAX"
DEFAULT_QUARANTINE_MAX = 5

#: Bridge anomaly-evidence docs in the engine's claim-quarantine store carry
#: this prefix. They are EVIDENCE, never content: every custody/identity scan
#: must exclude them, or a parked anomaly would read as a delivery in custody.
ANOMALY_PREFIX = "cosbridge-anomaly-"


def _quarantine_max() -> int:
    try:
        n = int(os.environ.get(QUARANTINE_MAX_ENV, "") or DEFAULT_QUARANTINE_MAX)
    except ValueError:
        return DEFAULT_QUARANTINE_MAX
    return n if n >= 1 else DEFAULT_QUARANTINE_MAX


def _recon():
    """``cos_reconcile_metrics`` (B3: the counter's one home), reached the same
    way as a script and as `tools.cos_ingest_bridge` — a bare import works only
    on the first, and silently lost the counter on every suite path."""
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import cos_reconcile_metrics                                  # noqa: PLC0415

    return cos_reconcile_metrics


class BridgeRefused(RuntimeError):
    """A fatal input condition. `bridge_run` turns it into a report; the
    ``reason`` slug rides that report so a refusal names its actual cause
    (a damaged ledger is not a missing one, attempt 16)."""

    def __init__(self, msg: str, *, reason: str = "no-ingestion-ledger"):
        super().__init__(msg)
        self.reason = reason


def bridge_ledger_path(vault, run_id: str) -> Path:
    """One row per candidate. Named `_cos_ingest_bridge_`, NOT
    `_cos_ingestion_`: the candidate ledger's six readers anchor on that exact
    prefix and would double-count a second file. Never indexed (INT-03)."""
    return cos.run_ops_dir(vault) / f"_cos_ingest_bridge_{run_id}.jsonl"


def content_choice_for(category: str, taxonomy: dict) -> str:
    """The category's content choice, from its taxonomy rule's lane. With the
    taxonomy unapproved/absent there is no lane fact, so all take the
    default."""
    if taxonomy.get("mode") != "active":
        return DEFAULT_CONTENT_CHOICE
    rule = (taxonomy.get("rules") or {}).get(str(category or "").strip())
    if not isinstance(rule, dict):
        return DEFAULT_CONTENT_CHOICE
    return _LANE_TO_CHOICE.get(str(rule.get("lane") or "both"),
                               DEFAULT_CONTENT_CHOICE)


def _attachment_names(row: dict) -> list[str]:
    """Attachment filenames, basename-guarded (`_safe_basename`, the one
    bare-name rule, INT-05), or []. `attachments` is an ADD-ONLY ledger
    extension no producer writes yet; the caller quarantines rather than
    guess."""
    return [n for n in (cos._safe_basename(str(a.get("filename") or ""))
                        for a in row.get("attachments") or []
                        if isinstance(a, dict)) if n]


def _claims(row: dict, corpus_row: dict | None) -> dict:
    """Provenance CLAIMS. The conversation id comes from the LEDGER ROW — the
    corpus row's `provenance` has sender/sent/subject only, and reading the id
    there sent every drop and manifest line out without one. Never
    `provenance.verified`: only a host parse of an original earns that."""
    prov = (corpus_row or {}).get("provenance") or {}
    cid = str(row.get("conversation_id") or "").strip()
    return {k: prov[k] for k in ("sender", "sent", "subject") if prov.get(k)
            } | ({"conversation_id": cid} if cid else {})


def _manifest_line(run_id: str, row: dict, *, filename: str, tier: str,
                   prov: dict, now: _dt.datetime) -> dict:
    """One manifest line in the sweep's OWN vocabulary. The sweep and the
    INT-04 anchors are untouched — this is only a new WRITER of that shape."""
    cid = str(row.get("conversation_id") or "")
    entry: dict = {
        "msg_key": f"{run_id}:{sha256_text(cid)[:12]}",
        "filename": filename,
        "expected_filename": filename,
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "category": str(row.get("category") or "").strip(),
        "classification": tier,
        "provenance": dict(prov),
    }
    size = next((a.get("approx_size_bytes") for a in row.get("attachments") or []
                 if isinstance(a, dict)
                 and cos._safe_basename(str(a.get("filename") or "")) == filename),
                None)
    if isinstance(size, int) and size > 0:
        entry["approx_size_bytes"] = size
    return entry


def _fm(key, value):
    # ALWAYS quoted — a JSON string IS a YAML double-quoted scalar, escapes
    # and all. `capture.enforce` RE-PARSES this block before staging it, and
    # unquoted (measured, not assumed) it reads `Q3 update #4` as a comment,
    # `yes`/`on` as bools, `~` as null, `0x1f` as 31 and an ISO date as a
    # date object, while a leading `*`/`&` fails strict YAML outright and
    # drops the whole block to the fallback parser — corrupting exactly the
    # two free-text fields (`title`, `provenance.subject`) an email fills.
    return f"{key}: {json.dumps(str(value), ensure_ascii=False)}"


def _run_day(run_id: str, now: _dt.datetime) -> str:
    """The DROP-CONTENT date: the run id's own date, falling back to the wall
    clock only when the run id carries none. Deliberate (attempt 14): the drop
    bytes are a pure function of (run, candidate, corpus, taxonomy, tier), so
    an idempotent re-run — even across a midnight — produces byte-identical
    content and the ENGINE's replay guard is what suppresses the duplicate."""
    try:
        return _dt.date.fromisoformat(run_id[:10]).isoformat()
    except ValueError:
        return now.date().isoformat()


def _proposal_content(*, run_id: str, row: dict, choice: str, tier: str,
                      text: str | None, corpus_row: dict | None,
                      attachments: list[str], now: _dt.datetime) -> str:
    """The drop for one candidate: provenance CLAIMS, the judged category, the
    content choice, and the SOURCE MAP. Every field is a claim — the host join
    reads the LEDGER row, not this frontmatter (STA-01). DETERMINISTIC per
    run (see `_run_day`): a re-run stages byte-identical content, which is
    exactly what lets the engine's replay guard own duplicate suppression."""
    corpus_row = corpus_row or {}
    prov, cid = _claims(row, corpus_row), str(row.get("conversation_id") or "")
    source_digest = str(corpus_row.get("text_sha256") or "") or None
    # `updated` is set EXPLICITLY (attempt 16, finding 3): `capture.enforce`
    # fills any missing `updated` from the WALL-CLOCK day, so omitting it
    # made the staged bytes differ across midnight — the replay guard binds
    # on byte-identity, and a next-day re-run's unbindable bytes re-asked
    # the owner a question the guard exists to suppress.
    fm: dict[str, object] = {
        "title": prov.get("subject") or f"COS ingestion candidate {run_id}",
        "type": "note", "classification": tier,
        "created": _run_day(run_id, now), "updated": _run_day(run_id, now),
        "content_choice": choice,
        "category": str(row.get("category") or "").strip(), "cos.run": run_id}
    if source_digest:
        fm["cos.source_sha256"] = source_digest
    fm.update({f"provenance.{k}": prov[k] for k in
               ("sender", "sent", "conversation_id", "subject") if prov.get(k)})

    lines = ["---", *[_fm(k, v) for k, v in fm.items()], "---", "",
             "## Source map (cos_ingest_bridge)", "",
             f"- run: {run_id}", f"- conversation_id: {cid}",
             f"- content_choice: {choice}"]
    if source_digest:
        lines.append(f"- corpus text_sha256: {source_digest}")
    lines += [f"- expected artifact: {n} (ingest-manifest lane; owner "
              "acceptance anchor INT-04)" for n in attachments] + [""]
    if text is not None and text.strip():
        lines += ["## Captured message text", "", text.rstrip(), ""]
    elif attachments:
        lines += ["The value of this candidate is its attachment(s); the "
                  "covering message carried no captured text.", ""]
    return "\n".join(lines)


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


def _conv_key(cid: str) -> str:
    """The RUN-INDEPENDENT conversation key every bridge ident ends in —
    ONE definition, the engine's (E16 re-derives it to look settlement
    claims up in the host record, attempt 13)."""
    return cos.bridge_conversation_key(cid)


def _bridge_ident(run_id: str, cid: str) -> str:
    return safe_slug(f"cosbridge-{run_id}-{_conv_key(cid)}")


def _attachment_shape(row: dict) -> tuple:
    """(basename, claimed size) per attachment, sorted. The size rides the
    identity (attempt 16, finding 4): names alone collapsed two rows naming
    the same filenames with DIFFERENT content — exactly what a mail thread
    produces — and silently dropped the second's work. A size claim
    `_manifest_line` would not record (non-int or <= 0) normalizes to 0, so
    the shape and the manifest line agree on what a size claim IS."""
    out = []
    for a in row.get("attachments") or []:
        if not isinstance(a, dict):
            continue
        name = cos._safe_basename(str(a.get("filename") or ""))
        if not name:
            continue
        size = a.get("approx_size_bytes")
        out.append((name, size if isinstance(size, int) and size > 0 else 0))
    return tuple(sorted(out))


def _row_shape(row: dict) -> tuple:
    """What makes two candidate rows for ONE conversation the SAME work —
    THE one rule (attempt 15, Codex :686-690; sizes added attempt 16): the
    fields the drop content and the attachment lane are built from —
    category (which derives the content choice and the tier floor), the
    tier claim, and the attachment set as (basename, claimed size) pairs.
    Two rows agreeing on these are rule-1 idempotency; two rows diverging
    are DIFFERENT work, and collapsing them silently dropped the second."""
    return (str(row.get("category") or "").strip(),
            str(row.get("classification") or "").strip(),
            _attachment_shape(row))


def _anomaly_nid(key: str) -> str:
    """Run-independent, so a standing anomaly is ONE parked doc refreshed per
    (id, code) — never one per night."""
    return safe_slug(f"{ANOMALY_PREFIX}{key}")


#: The three ledger-row settlement CLAIMS E16 reads (`settlement_claim`).
_SETTLEMENT_CLAIM_KEYS = ("bridge_quarantined", "bridge_refused",
                          "bridge_duplicate_of")


def _claim_settlement(row: dict, field: str, value: str) -> None:
    """Stamp ONE settlement claim on the ledger row, clearing the others.

    E16 matches the row's claimed KIND against the latest pass's host
    record, and `settlement_claim` reads the fields in a fixed order — so a
    stale claim left over from a pass that settled this row differently
    (quarantined last pass, a duplicate this pass) would out-rank the current
    one, mismatch the recorded kind, and score a genuinely settled row as
    forged. One claim on the row, the one this pass recorded."""
    for k in _SETTLEMENT_CLAIM_KEYS:
        row.pop(k, None)
    row[field] = value


# -- the host-private store (attempt 14: no delivery receipts live here) ------
#
# Attempts 12-13 kept per-conversation DELIVERY RECEIPTS in this store; the
# collapse deleted the delivery verdict they fed, and the receipts with it.
# The store itself stays, because two things that are NOT delivery evidence
# still earn their place in it: the manifest write-dedup records below (a
# planted mount twin must not pre-empt the real manifest line, attempts
# 11/13), and the ENGINE's settlement records (`cos.record_bridge_settlement`,
# E16's stamp exemption). It lives under ``config.host_private_base()`` — the
# ONE definition (INT-05) the approved queue, the attachment acceptance
# anchors, the single-writer lock and the supersede crash journal share —
# resolved and PROVEN off every VM-visible root by ``config.proven_off_mount``.


def receipts_root(vault) -> Path:
    """The bridge's host-private dir for THIS vault, proven off-mount.

    ONE definition, and it lives in the ENGINE (``cos.bridge_receipts_root``):
    E16's settlement exemption reads the settlement records in this same
    store, and a second copy of the path rule is how the first ends up subtly
    weaker. Same construction as the approved queue: ``host_private_base() /
    <dirname> / vault_slug8`` — the per-vault identity is the hash of the
    RESOLVED VAULT PATH, never the mount-resident ``.brain/vault-id`` a VM
    can rewrite. Raises ``config.HostPathUnsafe`` when ``$BRAIN_INDEX_DIR``
    (or a symlink) resolves back onto the mount — the caller REFUSES rather
    than fall back to a VM-reachable location. The name is historical
    (attempt 12's delivery receipts, deleted in attempt 14); what it holds
    now is the manifest write-dedup records and the engine's settlements."""
    return cos.bridge_receipts_root(vault)


def _receipts_ensure(vault) -> Path:
    """Create the host-private dir (0700). Only WRITE paths call this — a read
    must not materialise host state as a side effect (the approved-queue
    precedent, `cos._approved_ensure`)."""
    d = receipts_root(vault)
    d.mkdir(parents=True, exist_ok=True)
    config.secure_file_permissions(d, 0o700)
    return d


def _manifest_record_path(vault, run_id: str) -> Path:
    """The host-private record of the manifest-line content keys THIS bridge
    wrote for one run — the WRITE-DEDUP authority (attempt 12, Codex finding
    :1027-1037). The mount manifest is VM-writable, so deduping against ITS
    lines let a planted twin — identical in every field except ``ts``, the
    field the sweep's stale-namesake rule reads — pre-empt the real write and
    become the only durable attachment intent. Dedup now consults only what
    the host itself recorded writing."""
    return receipts_root(vault) / f"manifest-{safe_slug(run_id)}.jsonl"


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


def _corpus_text(row: dict, crow: dict | None, report: dict,
                 run_id: str) -> tuple[str | None, dict | None]:
    """(text, defect). All three failures quarantine the candidate rather
    than drop header-only content — preserving nothing under a real-looking
    drop is the worse outcome."""
    if crow is None:
        why = ("corpus-row-missing" if not report.get("corpus_error")
               else f"corpus-unreadable ({report['corpus_error']})")
        return None, {"reason": why, "detail": f"run {run_id} has no corpus row "
                      "for this conversation_id — nothing to build a candidate "
                      "from, and never drop header-only content"}
    text = str(crow.get("text") or "")
    if not text.strip():
        return None, {"reason": "corpus-text-empty", "detail": "the corpus row "
                      "exists but carries no body text — a drop built from it "
                      "would preserve nothing"}
    if str(crow.get("text_sha256") or "") != sha256_text(text):
        return None, {"reason": "corpus-digest-mismatch", "detail": "the corpus "
                      "row's text_sha256 does not hash to its own text — the "
                      "corpus is damaged; refusing unverifiable content"}
    return text, None


def _write_manifest_lines(vault, run_id: str, row: dict, crow: dict | None, *,
                          names: list[str], tier: str, path: Path,
                          known_keys: dict[str, set[str]], now: _dt.datetime,
                          outcome: dict | None = None) -> list[str]:
    """ING-02: one manifest line per attachment. The dedup key is still FULL
    content (`_line_content_key`, `ts` aside) so a re-run writes none twice —
    and the dedup SET (attempt 13) is the host-private record of lines the
    bridge itself wrote INTERSECTED with the mount manifest's current lines:
    no planted mount line can pre-empt the real write (attempt 12), and no
    host record can certify a line since deleted from the VM-writable
    manifest — a recorded-but-absent line is RE-WRITTEN, never presumed
    still there. ORDER per line: mount append first,
    host record second — a crash between them re-appends one identical line
    on retry (harmless: the sweep claims per line and the file arrives once);
    the reverse order would record a line that never landed. Given an
    ``outcome``,
    it also records exactly the lines THIS pass wrote on the row and the
    outcome — the recording is the write's own tail, not a second step a
    caller can forget."""
    written: list[str] = []
    for name in names:
        entry = _manifest_line(run_id, row, filename=name, tier=tier,
                               prov=_claims(row, crow), now=now)
        key = _line_content_key(entry)
        if key in known_keys["content"]:
            continue    # HOST-recorded AND still on the mount manifest
        path.parent.mkdir(parents=True, exist_ok=True)
        cos.append_jsonl(path, entry)
        if key not in known_keys["recorded"]:
            _receipts_ensure(vault)
            cos.append_jsonl(_manifest_record_path(vault, run_id),
                             {"key": key})
            known_keys["recorded"].add(key)
        known_keys["content"].add(key)
        written.append(name)
    if outcome is not None:
        if written:
            outcome["manifest_lines"] = written
        if names:
            # EVERY intended name is durably on the manifest now — written
            # this pass or deduped against an earlier pass's line. Recording
            # only `written` lost the crash-retry case: the retry's dedup
            # wrote nothing, the row stayed blank, and a later manifest loss
            # for this candidate could never be detected.
            row["attachment_manifest"] = sorted(
                set(row.get("attachment_manifest") or []) | set(names))
    return written


def _anomaly_doc(nid: str, *, run_id: str, row: dict, reason: str,
                 detail: str, now: _dt.datetime) -> str:
    """The parked EVIDENCE for one quarantined candidate: the reason, the
    full ledger row, and how to replay. Shaped as a valid capture note
    (id/type/classification/created) ON PURPOSE: the engine's hourly release
    path re-binds everything in claim-quarantine/, and a doc that fails
    `capture.validate` would be flushed to rejected/ within the hour instead
    of HELD — probed end to end through the real `cos.claim_drops` in the
    suite. Its nid joins no ingestion-ledger row, so the release path holds
    it (no-ledger-row) until a human resolves the anomaly or the bridge
    clears it when the conversation settles."""
    fm_lines = [_fm("id", nid), _fm("title", f"COS ingest bridge anomaly ({reason})"),
                _fm("type", "note"), _fm("classification", "MNPI"),
                _fm("created", now.date().isoformat())]
    return "\n".join([
        "---", *fm_lines, "---", "",
        "## Bridge quarantine evidence", "",
        f"- reason: {reason}", f"- detail: {detail}", f"- run: {run_id}",
        f"- conversation_id: {row.get('conversation_id') or ''}", "",
        "Ledger row at quarantine time:", "", "```json",
        json.dumps(row, ensure_ascii=False, sort_keys=True), "```", "",
        "Replay: fix the recorded cause, then re-run "
        f"`tools/cos_ingest_bridge.py --run-id {run_id}` — a settled "
        "conversation clears this doc automatically.", ""])


def _quarantine_candidate(vault, row: dict, outcome: dict, *, reason: str,
                          detail: str, run_id: str, key: str,
                          now: _dt.datetime, dry_run: bool) -> None:
    """Park ONE candidate: outcome + add-only ledger stamp + evidence doc in
    the engine's claim-quarantine store (reused, never a second quarantine).
    NOT silent skipping: the reason rides the report, the bridge ledger, the
    candidate row and the engine's defect log.

    The evidence is written EVERY time, on purpose. Skipping the write when
    `<nid>.md` already existed lost the second cause: a conversation that
    quarantined for one reason and then failed for a DIFFERENT one kept the
    stale doc and never reached `cos._quarantine_claim`, so the new defect was
    logged nowhere. The engine primitive is already idempotent per (id, code),
    so the same reason twice still writes one defect row — the guard bought
    nothing and cost the second reason."""
    outcome.update({"outcome": "quarantined", "reason": reason,
                    "detail": detail})
    if dry_run:
        return
    # THE HOST-PRIVATE SETTLEMENT RECORD, before the mount-side claim
    # (attempt 13): E16 exempts a stamp-less row only when this record backs
    # the row's `bridge_quarantined` field — the field alone is a VM-writable
    # ledger claim. Record first: a crash between the two leaves a record
    # with no claim (inert) rather than a claim no record backs (a night
    # failing E16 until the re-run re-settles).
    cos.record_bridge_settlement(vault, run_id=run_id, conversation_key=key,
                                 kind="quarantined", detail=reason, now=now)
    _claim_settlement(row, "bridge_quarantined", reason)
    nid = _anomaly_nid(key)
    text = _anomaly_doc(nid, run_id=run_id, row=row, reason=reason,
                        detail=detail, now=now)
    cos._quarantine_claim(vault, nid=nid, text=text,
                          sha=sha256_text(text), code=f"bridge-{reason}",
                          reason=detail, run_id=run_id, now=now,
                          source="cos-ingest-bridge")


def _clear_anomaly(vault, key: str) -> bool:
    """A settled conversation (dropped or delivered) resolves its standing
    anomaly doc — otherwise the quarantine store alerts forever on a fixed
    cause. Only the bridge's OWN evidence docs, never a real parked claim."""
    nid = _anomaly_nid(key)
    qdir = cos.claim_quarantine_dir(vault)
    cleared = False
    for suffix in (".md", ".json"):
        p = qdir / f"{nid}{suffix}"
        if p.is_file():
            p.unlink(missing_ok=True)
            cleared = True
    return cleared


def _never_category(vault, row: dict, outcome: dict, *, category: str,
                    taxonomy: dict, run_id: str, key: str,
                    now: _dt.datetime, dry_run: bool) -> bool:
    """NEVER-CATEGORY: zero drops (owner rule 2026-08-17 #2), reason on the
    row. True = the candidate is settled. The settlement is host-recorded
    BEFORE the row claim (attempt 13) — E16's exemption reads the record,
    never the claim."""
    _cat, disposition = cos.resolve_category(
        vault, category, lane=cos.LANE_TEXT, taxonomy=taxonomy)
    if disposition != cos.DISPOSITION_NEVER:
        return False
    outcome.update({"outcome": "never", "reason": f"never-ingest category "
                    f"{category!r} (overlay cos/ingest.md) — zero drops by "
                    "owner rule 2026-08-17 #2"})
    if not dry_run:
        cos.record_bridge_settlement(
            vault, run_id=run_id, conversation_key=key, kind="never-category",
            detail=f"never-ingest category {category!r}", now=now)
        _claim_settlement(row, "bridge_refused", "never-category")
    return True


def _same_run_duplicate(vault, row: dict, outcome: dict, prior: dict, *,
                        run_id: str, key: str, now: _dt.datetime,
                        dry_run: bool) -> None:
    """SAME-RUN DUPLICATE ROW (owner ruling): two candidate rows for one
    conversation inside ONE run is rule-1 idempotency — one conversation,
    one settlement, and the second row mirrors the first's verdict. Only
    SAME-WORK rows reach here (attempt 15): `_decide_candidate`'s shape gate
    sends a diverging duplicate to quarantine instead of this mirror.

    BOTH branches settle the row as a DUPLICATE — record + ledger claim
    (attempt 16, finding 1). The quarantined-prior branch used to set
    neither: E16 then saw a stamp-less row claiming nothing, `score_row`
    FAILed it on the missing proposal_id, and the duplicate of a quarantined
    conversation took the night to RUN_INVALID — the quarantine breaking the
    night it exists to save, returned through the duplicate path."""
    if prior.get("quarantined"):
        outcome.update({"outcome": "quarantined",
                        "reason": prior.get("reason") or "quarantined",
                        "detail": "duplicate row of a conversation this pass "
                        "already quarantined — counted once",
                        "duplicate_of": prior["ident"]})
    else:
        outcome.update({"outcome": "already-dropped", "ident": prior["ident"],
                        "prior_drop": prior.get("prior_drop", "this-run"),
                        "reason": "a duplicate candidate row for this "
                        "conversation — this run already settled it earlier "
                        "in this pass; one conversation, one drop "
                        "(idempotent skip)"})
        if prior.get("sha256"):
            outcome["proposal_sha256"] = prior["sha256"]
    if not dry_run:
        # honest ledger record of the skip, add-only key; never the drop
        # stamps — those belong to the ONE row the drop was made from.
        # Host settlement record first (attempt 13): E16's exemption for
        # this claim reads the record, never the claim.
        cos.record_bridge_settlement(
            vault, run_id=run_id, conversation_key=key, kind="duplicate",
            detail=f"same-run duplicate row of {prior['ident']}", now=now)
        _claim_settlement(row, "bridge_duplicate_of", prior["ident"])


def _candidate_content(run_id: str, row: dict, *, choice: str,
                       crow: dict | None, report: dict
                       ) -> tuple[str | None, list[str], dict | None]:
    """(text, attachment names, defect). A defect quarantines the candidate:
    corpus failures for text-carrying choices, and a file-carrying choice
    whose row names no attachment filename (the bridge refuses to guess a
    file the manifest would claim)."""
    text: str | None = None
    if choice in (CONTENT_TEXT, CONTENT_BOTH):
        text, defect = _corpus_text(row, crow, report, run_id)
        if defect:
            return None, [], defect
    wants_files = choice in (CONTENT_ATTACHMENTS, CONTENT_BOTH)
    attachments = _attachment_names(row) if wants_files else []
    if wants_files and not attachments:
        return None, [], {
            "reason": "attachment-names-missing",
            "detail": f"content_choice {choice!r} but the ledger row names no "
            "attachment filename — the bridge refuses to guess a file the "
            "manifest would claim"}
    return text, attachments, None


def _decide_candidate(vault, run_id: str, row: dict, outcome: dict, *,
                      taxonomy: dict, corpus: dict, report: dict,
                      manifest_path: Path, known_keys: set[str],
                      handled: dict[str, dict], consumed: set[str],
                      flush, now: _dt.datetime, dry_run: bool) -> None:
    """Decide and (unless dry-run) EXECUTE one candidate, in place on
    ``outcome`` and the ledger ``row``. Every exit sets `outcome["outcome"]`,
    so the tally and the bridge ledger agree by construction.

    NO DELIVERY VERDICT (attempt 14). The only ways a candidate does NOT
    produce a fresh drop are decided from THIS pass's own input: the
    never-category rule, a SAME-WORK duplicate row of a conversation this
    same pass already settled (``handled``, in-memory — a duplicate row that
    names DIFFERENT work quarantines as ambiguity instead of being absorbed,
    attempt 15), and a content defect. No recorded stamp, outcome, custody
    or drop file is consulted for any of it. The ONE recorded thing read —
    ``consumed``, the claims ledger's sha set, AFTER the drop is written —
    can only turn a drop the engine will delete unseen into a loud
    quarantine (`_execute_fresh_drop`); it can never make this function skip
    silently or report a delivery."""
    cid = str(row.get("conversation_id") or "")
    category = str(row.get("category") or "").strip()
    ident = _bridge_ident(run_id, cid)
    key = _conv_key(cid)

    if _never_category(vault, row, outcome, category=category,
                       taxonomy=taxonomy, run_id=run_id, key=key, now=now,
                       dry_run=dry_run):
        return

    choice = content_choice_for(category, taxonomy)
    outcome["content_choice"] = choice
    shape = _row_shape(row)

    prior = handled.get(key)
    if prior is not None:
        if prior.get("shape") == shape:
            _same_run_duplicate(vault, row, outcome, prior, run_id=run_id,
                                key=key, now=now, dry_run=dry_run)
        else:
            # DIVERGING DUPLICATE ROWS (attempt 15, Codex :686-690): two rows
            # for one conversation naming DIFFERENT work — another category,
            # another tier claim, other attachments — are not idempotency,
            # and mirroring the first verdict silently dropped the second.
            # The bridge cannot know which one the judge meant, so the
            # ambiguity is PARKED with both shapes in the evidence; the row
            # already settled stays settled.
            _quarantine_candidate(
                vault, row, outcome, reason="duplicate-rows-diverge",
                detail=("a second candidate row for this conversation names "
                        "DIFFERENT work than the row this pass settled as "
                        f"{prior.get('ident')}: (category, classification, "
                        f"attachments) {prior.get('shape')!r} vs {shape!r} — "
                        "absorbing one into the other silently drops it; "
                        "fix the judgment ledger and replay"),
                run_id=run_id, key=key, now=now, dry_run=dry_run)
        return

    crow = corpus.get(cid)
    text, attachments, defect = _candidate_content(
        run_id, row, choice=choice, crow=crow, report=report)
    if defect is not None:
        _quarantine_candidate(vault, row, outcome, reason=defect["reason"],
                              detail=defect["detail"], run_id=run_id,
                              key=key, now=now, dry_run=dry_run)
        handled[key] = {"ident": ident, "quarantined": True,
                        "reason": defect["reason"], "shape": shape}
        return

    # THE TIER, from the one engine classifier: MNPI default; the row's claim
    # and the category floor can only RAISE. The bridge cannot lower a tier.
    tier, _why = provenance.email_classification(
        vault, proposed=row.get("classification"), category=category)
    outcome["tier"] = tier

    if dry_run:
        # The rehearsal reports drop INTENT. It does not predict the sweep's
        # replay verdict: that needs the STAGED bytes' sha, which only the
        # real `cos.propose` (via `capture.enforce`) computes — a rehearsal
        # duplicating that pipeline read-only would be a second copy of the
        # path rule, the exact debt these reviews keep killing.
        outcome.update({"outcome": "dropped (dry-run)", "ident": ident,
                        "manifest_lines": list(attachments)})
        handled[key] = {"ident": ident, "sha256": None,
                        "prior_drop": "this-run", "shape": shape}
        return

    _execute_fresh_drop(
        vault, run_id, row, outcome, choice=choice, tier=tier, text=text,
        crow=crow, attachments=attachments, ident=ident, key=key,
        manifest_path=manifest_path, known_keys=known_keys,
        handled=handled, consumed=consumed, shape=shape, flush=flush, now=now)


def _execute_fresh_drop(vault, run_id: str, row: dict, outcome: dict, *,
                        choice: str, tier: str, text: str | None,
                        crow: dict | None, attachments: list[str], ident: str,
                        key: str, manifest_path: Path, known_keys: set[str],
                        handled: dict[str, dict], consumed: set[str],
                        shape: tuple, flush, now: _dt.datetime) -> None:
    """Write the drop for one candidate, in this order: flush pending stamps,
    MANIFEST LINE, drop, REPLAY OBSERVATION, stamps."""
    # PERSIST PENDING STAMPS BEFORE THIS DROP HITS DISK: with the ledger
    # rewrite batched (O(N), not once per candidate), this is what keeps the
    # crash window at today's size — at any instant at most ONE drop on disk
    # lacks its persisted stamp, and an unstamped drop is recovered by
    # RE-DROPPING onto the same ident filename, never by adopting its bytes.
    flush()
    # ATTACHMENT INTENT BEFORE THE DROP PUBLISHES (attempt 10, finding 2).
    # The manifest line is the durable record the ingest sweep consumes, so
    # it lands FIRST: a crash after `cos.propose` used to leave a published
    # drop with no durable attachment intent — the retry adopted the drop as
    # already-delivered, detected no missing line (the row's
    # attachment_manifest was never persisted), exited clean, and the
    # attachment silently never entered the sweep. Written before the drop,
    # every crash ordering retries to a complete state: line-without-drop
    # re-drops (the line dedups), drop-consumed-without-stamp skips with the
    # line already durable.
    _write_manifest_lines(vault, run_id, row, crow, names=attachments,
                          tier=tier, path=manifest_path, outcome=outcome,
                          known_keys=known_keys, now=now)
    # ONE atomic write at the deterministic ident: a leftover there — crash
    # residue, a prior pass's still-in-flight drop, or a forgery — is simply
    # overwritten by the bridge's own bytes. No attempt counter, no
    # replay-avoidance loop (attempt 14): the content is deterministic per
    # run, and whether these exact bytes were already claimed, rejected or
    # expired is the ENGINE's replay guard's call at sweep time, not ours.
    res = cos.propose(vault, _proposal_content(
        run_id=run_id, row=row, choice=choice, tier=tier, text=text,
        corpus_row=crow, attachments=attachments, now=now), ident=ident)
    # THE REPLAY OBSERVATION (attempt 15). The guard's call is still the
    # engine's — but the bridge reads what that call WILL BE, from the same
    # authority the sweep reads (`_claim_text_drops`: any claims-ledger entry
    # with this sha, whatever its disposition), and refuses to exit clean
    # over a write the engine deletes unseen. Deterministic bytes made that
    # deletion PERMANENT: no later pass ever stages different bytes, so the
    # candidate was silently gone forever. It quarantines instead — reason,
    # ident and digest in the evidence, counted toward the threshold. ONE
    # carve-out, verified on bytes: the engine's own claim quarantine parked
    # these exact bytes as HELD custody (`claim-quarantine/<ident>.md`, the
    # crash-between-drop-and-stamp recovery). Those bytes are waiting for
    # the very stamps this pass writes — the hourly release delivers them —
    # so quarantining here would strand a healthy heal. A planted held copy
    # buys an attacker nothing suppressive: it routes the SAME bytes to the
    # owner through the release path instead.
    if res["sha256"] in consumed:
        held = cos.claim_quarantine_dir(vault) / f"{res['id']}.md"
        try:
            held_ok = held.is_file() and sha256_text(
                held.read_text(encoding="utf-8")) == res["sha256"]
        except OSError:
            held_ok = False
        if not held_ok:
            _quarantine_candidate(
                vault, row, outcome, reason="replay-rejected",
                detail=(f"the claims ledger already holds these exact bytes "
                        f"(drop {res['id']}, sha256 {res['sha256']}) and no "
                        "held claim-quarantine copy awaits release: the "
                        "engine's claim sweep will replay-reject and delete "
                        "this drop unseen, so this pass's write cannot reach "
                        "the owner — parked loudly instead of lost silently"),
                run_id=run_id, key=key, now=now, dry_run=False)
            handled[key] = {"ident": ident, "quarantined": True,
                            "reason": "replay-rejected", "shape": shape}
            return
    # THE STAMPS E16 READS AND THE CLAIM JOIN NEEDS. Without
    # `proposals_dropped: true` the verifier sees the flag deny a drop the host
    # took delivery of (hard FAIL); without id+digest the claim cannot
    # attribute it to this run.
    row.update({"proposal_id": res["id"], "content_sha256": res["sha256"],
                "proposals_dropped": True, "content_choice": choice})
    for k in _SETTLEMENT_CLAIM_KEYS:
        row.pop(k, None)
    outcome.update({"outcome": "dropped", "ident": res["id"],
                    "proposal_sha256": res["sha256"]})
    if _clear_anomaly(vault, key):
        outcome["anomaly_cleared"] = True
    handled[key] = {"ident": res["id"], "sha256": res["sha256"],
                    "prior_drop": "this-run", "shape": shape}


def _line_content_key(entry: dict) -> str:
    """FULL-CONTENT identity of one manifest line, ``ts`` aside — the WRITE
    dedup's key (attempt 11, Codex finding: forged manifest collision). The
    manifest file is VM-writable, so a planted line sharing only the
    (msg_key, filename) name key must never stop the bridge's real line from
    landing — the attacker-authored line would then be the only durable
    attachment intent the sweep consumes. Untrusted bytes may CONFIRM the
    exact line the bridge is about to write, never substitute a different
    one: dedup fires only on a line identical in every field except `ts`."""
    return json.dumps({k: v for k, v in entry.items() if k != "ts"},
                      ensure_ascii=False, sort_keys=True)


def _known_manifest_keys(vault, run_id: str, path: Path) -> dict[str, set[str]]:
    """The key sets for this run, each from the source that can be trusted
    for ITS job (attempt 12; dedup re-keyed attempt 13):

    - ``recorded`` is the HOST-PRIVATE record of lines the bridge itself
      wrote (`_manifest_record_path`).
    - ``content`` (WRITE DEDUP) is ``recorded`` INTERSECTED with the lines
      currently on the mount manifest. The host record is NECESSARY — a
      mount line alone must never fire the dedup (attempt 11: a planted twin
      identical in every field except ``ts`` pre-empted the real write,
      leaving the attacker's forged ``ts`` as the only durable one) — and
      mount presence is also necessary, because a record that a line was
      WRITTEN is not evidence the line IS THERE (attempt 13: a line deleted
      from the VM-writable manifest after the record was made was certified
      forever, and the attachment never travelled). Each side of the pair
      can only force a RE-WRITE, never suppress one.
    """
    out: dict[str, set[str]] = {"content": set(), "recorded": set()}
    rpath = _manifest_record_path(vault, run_id)
    if rpath.is_file():
        for x in rpath.read_text(encoding="utf-8").splitlines():
            if x.strip():
                try:
                    key = json.loads(x).get("key")
                except ValueError:
                    continue          # a torn record line is not a write
                if key:
                    out["recorded"].add(str(key))
    if not path.is_file():
        return out
    mount_content: set[str] = set()
    # THE MOUNT MANIFEST IS UNTRUSTED INPUT (attempt 16, finding 5): a torn
    # or hostile line here used to escape `_bridge_locked` as a raw
    # ValueError and kill the nightly as "the ingest bridge refused the run"
    # — false, and a VM-writable denial of service. A line that does not
    # parse to a dict is not a manifest line: it cannot CONFIRM a recorded
    # write, so the dedup fails toward RE-WRITING (the documented safe
    # direction — worst case one duplicate identical line the sweep claims
    # once). An unreadable file is the same: no confirmations, re-write all.
    try:
        mount_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for x in mount_lines:
        if not x.strip():
            continue
        try:
            entry = json.loads(x)
        except ValueError:
            continue
        if isinstance(entry, dict):
            mount_content.add(_line_content_key(entry))
    # THE DEDUP SET IS THE INTERSECTION (attempt 13, Codex :812-831). A host
    # record that a line was WRITTEN is not evidence the line IS THERE: the
    # mount manifest is VM-writable, so a line deleted after the record was
    # made would be certified forever and the attachment would never travel.
    # Host-recorded AND currently on the mount -> skip; recorded but absent
    # -> RE-WRITE (fail toward re-writing, worst case one duplicate identical
    # line the sweep claims once); present but never host-recorded -> still
    # write (attempt 11: a planted twin must not pre-empt the real write).
    out["content"] = out["recorded"] & mount_content
    return out


def _consumed_shas(vault) -> set[str]:
    """THE CLAIM PATH'S REPLAY AUTHORITY, read once per pass exactly as the
    sweep builds it (`_claim_text_drops`: every `sha256` in the claims
    ledger, whatever its disposition): a drop whose exact bytes are in this
    set is deleted unseen at sweep time ("replay-rejected"). Read only to
    turn that silent deletion into a loud quarantine after the drop is
    written (`_execute_fresh_drop`) — never to skip one."""
    consumed = {str(e.get("sha256") or "")
                for e in cos._read_jsonl(cos._claims_path(vault))}
    consumed.discard("")
    return consumed


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


def _finish_report(report: dict, outcomes: list[dict]) -> tuple[dict, int]:
    """Tally the pass and derive status + exit code, threshold included."""
    def _n(*kinds: str) -> int:
        return sum(1 for o in outcomes if o.get("outcome") in kinds)

    refused = [{"conversation_id": o["conversation_id"], "reason": o["reason"],
                "detail": o.get("detail", "")}
               for o in outcomes if o.get("outcome") == "refused"]
    quarantines = [{"conversation_id": o["conversation_id"],
                    "reason": o.get("reason", ""),
                    "detail": o.get("detail", "")}
                   for o in outcomes if o.get("outcome") == "quarantined"]
    # the threshold unit is the CONVERSATION — a duplicate row of a
    # quarantined conversation must not double it toward the limit
    q_keys = {_conv_key(str(o.get("conversation_id") or ""))
              for o in outcomes if o.get("outcome") == "quarantined"}

    qmax = _quarantine_max()
    report.update({
        "refused": refused, "dropped": _n("dropped", "dropped (dry-run)"),
        "never": _n("never"), "already_dropped": _n("already-dropped"),
        "quarantined": len(q_keys), "quarantines": quarantines,
        "quarantine_max": qmax,
        "manifest_lines": sum(len(o.get("manifest_lines") or [])
                              for o in outcomes)})
    if len(q_keys) >= qmax and q_keys:
        # MASS INCONSISTENCY IS A DIFFERENT EVENT from one odd thread: at or
        # over the threshold the run fails loudly (the nightly dies before the
        # mutation lane). Everything already settled this pass stays settled —
        # drops are real, stamped and idempotent.
        report["status"] = "quarantine-overflow"
        report["error"] = (f"{len(q_keys)} conversation(s) quarantined — at or "
                           f"over the {QUARANTINE_MAX_ENV} threshold ({qmax}); "
                           "the input is inconsistent at a scale one odd "
                           "thread cannot explain. The bridge refused to call "
                           "this night clean")
        return report, EXIT_REFUSED
    if refused:
        report["status"] = "refused"
        return report, EXIT_REFUSED
    report["status"] = "quarantined" if q_keys else "ok"
    return report, EXIT_OK


def _observed_dropped(vault, run_id: str, candidates: list[dict],
                      excluded: frozenset[str] | set[str] = frozenset(),
                      written: dict[str, dict] | None = None) -> int:
    """DISTINCT CONVERSATIONS among this run's candidates whose drop THIS
    PASS itself wrote, read back off disk against the sha `cos.propose`
    returned — held in this process's memory, so it is a host observation of
    a host action, never a record anything else could plant. Deliberately
    blind to the bridge ledger: this is the `reported` side of the reconcile
    join, and the join's `ledgered` side is `counts_bridge_dropped` over that
    ledger, so reading the count off the same file made `reported ==
    ledgered` by construction and the shortfall structurally zero. Two
    independent observations can genuinely disagree — a ledger row claiming a
    drop that is on no disk is exactly the shortfall the join must see.

    Since attempt 14 there is no delivered-resolution leg: every pass
    re-proposes, so a PRE-CLAIM idempotent re-run still observes N through
    its OWN writes — a truthful N is never superseded by a 0 there. A
    POST-CLAIM re-run is different since attempt 15: its re-drops are
    replay-quarantined (excluded below), it observes fewer, and the
    superseding row plus the reconcile shortfall NAME that — the same loud
    treatment a degraded (quarantining) re-run always got, pointing at the
    replay-rejected evidence. Any drop-dir file this pass did not write
    (planted, leftover, another pass's) counts nothing.

    The unit is the CONVERSATION because the ledgered side counts distinct
    conversations too (owner rule, attempt 5 #5). ``excluded`` is the
    conversation keys THIS PASS refused or quarantined: whatever file exists
    for one of those is the partial state that was parked, not a delivery of
    this pass — counting it pushed `reported` above `ledgered` and masked a
    genuinely lost drop behind `max(0, …)`."""
    written = written or {}
    seen: set[str] = set()
    for row in candidates:
        ident = _bridge_ident(run_id, str(row.get("conversation_id") or ""))
        key = ident.rsplit("-", 1)[-1]
        if key in excluded:
            continue
        w = written.get(key)
        if not (w and w.get("ident") == ident and w.get("sha256")):
            continue
        p = cos.proposal_drop_dir(vault) / f"{ident}.md"
        try:
            if (p.is_file() and sha256_text(p.read_text(encoding="utf-8"))
                    == w["sha256"]):
                seen.add(key)
        except OSError:
            pass
    return len(seen)


def _update_metrics(vault, run_id: str, *, now: _dt.datetime,
                    candidates: list[dict], excluded: set[str],
                    written: dict[str, dict] | None = None) -> dict:
    """Record `ingestion_dropped` on the run's metrics row (B3: defined in
    tools/cos_reconcile_metrics.py) by APPENDING a row naming the one it
    supersedes (REP-02). Best-effort: a missing prior row is the judgment leg's
    failure to surface, not this function's to synthesize.

    THE COUNT IS THIS RUN'S TOTAL, OBSERVED ON DISK (`_observed_dropped`), not
    what this invocation happened to drop and NOT what the bridge ledger says:
    the join counts the ledgered side off that ledger, so a reported side read
    from the same file could never disagree with it. A PRE-CLAIM idempotent
    re-run re-proposes and so still observes N through its own verified
    writes — a truthful N is never superseded by a 0 there. What this counter
    can detect: a bridge ledger asserting drops that do not exist
    (shortfall), and a pass whose candidates degraded — a quarantined re-run,
    including a POST-CLAIM one whose re-drops replay-quarantined (attempt
    15), observes fewer and the superseding row names it.
    """
    recon = _recon()
    ops = cos.run_ops_dir(vault)
    path = ops / "_cos_metrics.jsonl"
    if not path.is_file():
        return {"updated": False, "reason": "no metrics row to extend"}
    dropped = _observed_dropped(vault, run_id, candidates,
                                excluded=excluded, written=written)
    prior = recon._rows(path)
    day, n = run_id[:10], run_id.rsplit("run", 1)[-1]
    # by run_id, else by the (date, run) key the row itself carries
    mine = ([r for r in prior if r.get("run_id") == run_id]
            or [r for r in prior
                if str(r.get("date")) == day and str(r.get("run")) == n])
    if not mine:
        return {"updated": False, "reason": "no metrics row for this run"}
    # The metrics row is MOUNT-RESIDENT input (attempt 16, finding 6): a
    # non-numeric `ingestion_dropped` used to raise out of this best-effort
    # function AFTER the pass wrote its drops, killing the run as a false
    # "refused". A value that is not a number is simply not "already
    # recorded" — fall through and supersede it with the observed truth.
    try:
        recorded = int(mine[-1]["ingestion_dropped"] or 0)
    except (KeyError, TypeError, ValueError):
        recorded = None
    if recorded == dropped:
        # Nothing changed — a re-run does not need a superseding row, and
        # appending one every night would churn the file for no new fact.
        return {"updated": False, "reason": "already recorded",
                "ingestion_dropped": dropped}
    row = dict(mine[-1], ingestion_dropped=dropped)
    row["run_ts"] = (now.strftime("%Y-%m-%dT%H:%M:%S.")
                     + f"{now.microsecond // 1000:03d}Z")
    row[recon.SUPERSEDES] = str(mine[-1].get("run_ts"))
    try:
        return {"updated": True, "append": recon.append_metric(ops, row)}
    except ValueError as exc:
        return {"updated": False, "reason": str(exc)[:300]}


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
