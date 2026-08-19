"""COS batch-consumption operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._facade import public
from ._approval import approved_staged, stage_approved
from ._attachment_acceptance import _accept_attachment
from ._attachment_store import _discard_attachment, attachment_metas
from ._batches import _batches_path, _write_batches, batch_digest, parse_batch_answer
from ._claims_state import _bound_meta
from ._corrections import record_correction
from ._guards import _safe_meta_id
from ._io import _fsync_dir, _read_jsonl, _write_atomic
from ._layout import _parse_ts, _ts, _utcnow, host_dir, proposals_dir
from ._learning_ledger import _record_verdict, log_defect
from ._spine import _spine_ingest_commitment
from ._version_apply import _apply_version_link
from ._version_links import version_link_metas
from ._batch_apply import _apply_batch_decision
from ._answer_consume import _consume_batch_answers, _consume_corrections

def _consume_journal_path(vault) -> Path:
    return proposals_dir(vault) / _CONSUME_JOURNAL

def _clear_consume_journal(vault) -> None:
    """Durably forget the journal: an unlink that never reached the disk brings
    a finished decision back on the next boot."""
    path = _consume_journal_path(vault)
    path.unlink(missing_ok=True)
    _fsync_dir(path.parent)

def _write_consume_journal(vault, record: dict[str, Any]) -> None:
    """Journal the decision DURABLY before any side effect (B4). fsync, not
    just write: a decision that only reached the page cache is a decision the
    recovery path cannot resume, and by then files have already moved."""
    path = _consume_journal_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    public("_write_atomic")(path, json.dumps(record, sort_keys=True).encode("utf-8"))

def _cas_batch(vault, batch_id: str, *, expect_state: str, expect_gen: int,
               updates: dict[str, Any]) -> bool:
    """Compare-and-set ONE batch record's state, re-read from disk."""
    batches = _read_jsonl(_batches_path(vault))
    for b in batches:
        if b.get("batch_id") != batch_id:
            continue
        if b.get("state") != expect_state or int(b.get("generation", 0)) != expect_gen:
            return False
        b.update(updates)
        b["generation"] = expect_gen + 1
        _write_batches(vault, batches)
        return True
    return False

def _answer_beat_the_deadline(entry: dict[str, Any], batch: dict[str, Any]) -> bool:
    """Timeliness is judged on the DURABLE answer timestamp, never on when the
    consumer happens to run: an owner who answered inside the window must not
    lose the answer because the next maintain fire landed after expiry."""
    answered = _parse_ts(str(entry.get("answered_at") or entry.get("answered") or ""))
    expires = _parse_ts(str(batch.get("expires", "")))
    return bool(answered and expires and answered <= expires)


def _verified_decision(vault, b: dict[str, Any], entry: dict[str, Any] | None,
                       ) -> tuple[list[str] | None, str, str]:
    """THE verification routine: signed batch + owner answer -> accepted set.

    ONE implementation, used by the normal consumer AND by crash recovery.
    They used to differ: recovery replayed the journal's own ``accepted`` list
    with no signature check and no owner answer at all, so a forged
    ``consume-pending.json`` plus a forged ``batches.jsonl`` row — both on the
    shared mount — got their contents staged and signed. Any second
    implementation of this is the bug, not the fix.

    Returns ``(accepted_ids | None, answer_mode, reason)``.
    """
    batch_id = str(b.get("batch_id", ""))
    if not isinstance(entry, dict) or entry.get("status") != "answered":
        return None, "", _NO_ANSWER
    # The answer must be the answer to THIS question. The key is DERIVED from
    # the batch id, never read off the batch row: `batch_digest` covers only
    # (batch_id, created, schema, candidates), so a row's `answer_key` is
    # unsigned — appending one to a legitimately signed open batch used to let
    # a forged journal borrow any other answered "accept all" entry.
    if str(entry.get("key", "")) != f"{BROKER_KEY_PREFIX}{batch_id}":
        return None, "", "answer does not belong to this batch"
    if b.get("state") == "expired" and not _answer_beat_the_deadline(entry, b):
        return None, "", "late answer (post-expiry)"
    digest = batch_digest(batch_id, b.get("created", ""), b.get("candidates", []))
    if digest != b.get("digest"):
        return None, "", _SIG_FAILED
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        from .. import audit

        pub = load_pem_public_key(audit.public_key_pem())
        pub.verify(bytes.fromhex(str(b.get("sig", ""))), digest.encode("utf-8"))
    except Exception:  # noqa: BLE001 — unverifiable is unusable
        return None, "", _SIG_FAILED
    batch_ids = [c["id"] for c in b.get("candidates", [])]
    accepted_ids, outcome = parse_batch_answer(str(entry.get("answer", "")), batch_ids)
    if accepted_ids is None:
        return None, "", outcome          # includes subset validation failures
    # `accept all` over a large batch is approval fatigue, not agreement — the
    # mode travels with every verdict so the evidence gate can exclude it.
    answer_mode = {"accept-all": "accept-all", "reject-all": "reject-all"}.get(
        outcome, "itemized")
    return accepted_ids, answer_mode, outcome

def _recover_consume_journal(core, report: dict[str, Any], now: _dt.datetime,
                             answered: dict[str, Any]) -> None:
    """RESUME an interrupted apply — never discard it (B4).

    The journal is written and fsynced before the batch is CASed into the
    ``applying`` generation, and the ``applying`` generation is entered before
    any file moves. So a journal on disk means one of exactly two things, and
    both are finished here rather than abandoned:

    - batch still ``open``/``expired``  -> the crash beat the applying CAS;
      take it now, then apply.
    - batch already ``applying``        -> the crash landed mid-apply; re-apply
      (every step is idempotent) and close it out.

    A journal whose batch is already ``consumed`` is the third window — the
    crash fell between the closing CAS and the journal unlink — and needs only
    the unlink.

    NOTHING IN THE JOURNAL IS TRUSTED except which batch was in flight. The
    journal and ``batches.jsonl`` both live under ``host/proposals/`` — on the
    shared mount — so the decision is RE-DERIVED here through the same
    ``_verified_decision`` the normal path uses: valid host signature over the
    batch, a real answered owner-inbox entry, subset-validated. A journal that
    does not survive that is discarded with a defect, never applied.
    """
    vault = core.vault
    path = _consume_journal_path(vault)
    if not path.exists():
        return
    try:
        journal = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _clear_consume_journal(vault)
        return
    batch_id = journal.get("batch_id")
    b = next((x for x in _read_jsonl(_batches_path(vault))
              if x.get("batch_id") == batch_id), None)
    if b is not None and b.get("state") in ("applying", "open", "expired"):
        state, gen = str(b.get("state")), int(b.get("generation", 0))
        key = f"{BROKER_KEY_PREFIX}{batch_id}"      # DERIVED, never read off the row
        accepted_ids, answer_mode, reason = _verified_decision(
            vault, b, answered.get(key))
        if accepted_ids is None:
            report["invalid"].append({
                "batch_id": batch_id,
                "reason": f"journal recovery REFUSED (nothing applied): {reason}"})
            log_defect(vault, "consume-journal-refused",
                       f"{batch_id}: {reason}", ts=_ts(now))
            # ...and do not leave the batch stuck in `applying` forever: an
            # unverifiable decision closes the batch as invalid, so the queue
            # is not wedged behind it.
            if state == "applying":
                _cas_batch(vault, batch_id, expect_state="applying",
                           expect_gen=gen,
                           updates={"state": "invalid", "consumed_at": _ts(now)})
            _clear_consume_journal(vault)
            return
        entered = True
        if state != "applying":
            entered = _cas_batch(vault, batch_id, expect_state=state,
                                 expect_gen=gen,
                                 updates={"state": "applying",
                                          "applying_at": _ts(now)})
            gen += 1
        if entered:
            _apply_batch_decision(core, b, accepted_ids, answer_mode, now, report)
            _cas_batch(vault, batch_id, expect_state="applying", expect_gen=gen,
                       updates={"state": "consumed", "outcome": reason,
                                "consumed_at": _ts(now)})
            report.setdefault("journal_recovered", []).append(batch_id)
    _clear_consume_journal(vault)

def consume_answers(core, now: _dt.datetime | None = None) -> dict[str, Any]:
    """The ANSWER-CONSUMER: apply owner answers to broker questions ONLY.

    - Ignores every inbox entry outside the ``cosbroker:``/``coscorrect:``
      namespaces (an unrelated answered question is never consumed here).
    - Verifies the batch record's Ed25519 signature over its recomputed
      candidate-set digest before acting (a tampered batches.jsonl fails).
    - Enforces subset validation, one-shot consumption (a replayed answer to a
      consumed batch is rejected), and late-answer rejection — judged on the
      DURABLE answer timestamp, never on when this consumer runs.
    - Stages ONLY accepted candidates into the HOST-ONLY approved queue under
      an Ed25519-signed content anchor (INT-01; whence the ordinary audited
      host drain signs them); rejected candidates go to rejected/, and
      a rejected ATTACHMENT is discarded with zero residue (it never entered
      the vault).
    - Runs under the single-writer lock with a crash-recoverable journal.
    """
    now = now or _utcnow()
    with vault_writer_lock(core.vault, verb="cos-consume"):
        return _consume_answers_locked(core, now)

def _consume_answers_locked(core, now: _dt.datetime) -> dict[str, Any]:
    """Consume broker answers under the vault writer lock."""
    vault = core.vault
    report: dict[str, Any] = {
        "accepted": [], "rejected": [], "requeued": [],
        "replay_rejected": [], "late_rejected": [], "invalid": [],
        "corrections_applied": [], "corrections_discarded": [],
        "corrections_failed": [],
    }
    entries = core._read_inbox()
    answered = {e["key"]: e for e in entries
                if isinstance(e, dict) and e.get("status") == "answered"
                and isinstance(e.get("key"), str)}
    # Recovery re-derives its decision from these same answers (never from the
    # journal's own word), so it runs AFTER they are read.
    _recover_consume_journal(core, report, now, answered)

    _consume_batch_answers(core, now, answered, report)
    _consume_corrections(vault, now, answered, report)
    return report

def enqueue_correction_questions(core, now: _dt.datetime | None = None) -> list[str]:
    """One owner-inbox question per claimed-but-unasked correction drop."""
    now = now or _utcnow()
    vault = core.vault
    asked: list[str] = []
    cor_pending = proposals_dir(vault) / "corrections-pending"
    if not cor_pending.is_dir():
        return asked
    for f in sorted(cor_pending.glob("correction-*.json")):
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        key = f"{CORRECT_KEY_PREFIX}{payload.get('round')}:{payload.get('msg_key')}"
        q = {
            "key": key,
            "question": (f"Apply COS correction (round {payload.get('round')}, "
                         f"msg {payload.get('msg_key')}): bucket→"
                         f"{payload.get('corrected_bucket')}, tier→"
                         f"{payload.get('corrected_tier')}?"),
            "options": ["apply", "discard"],
            "default": "discard",
            "context": "a correction becomes a correction_events row only via "
                       "this human answer — the VM drop alone never writes it",
        }
        if core.enqueue_question(q, source="cos-broker:correction",
                                 today=now.date()):
            asked.append(key)
    return asked

__all__ = ['_consume_journal_path', '_clear_consume_journal', '_write_consume_journal', '_cas_batch', '_answer_beat_the_deadline', '_apply_batch_decision', '_verified_decision', '_recover_consume_journal', 'consume_answers', '_consume_answers_locked', 'enqueue_correction_questions']
