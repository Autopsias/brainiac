"""COS owner-answer consumption."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._facade import public
from ._batches import _batches_path
from ._io import _read_jsonl
from ._layout import _ts, proposals_dir
from ._corrections import record_correction


def _consume_batch_answer(core, now: _dt.datetime, key: str, entry: dict[str, Any],
                          batch: dict[str, Any] | None, report: dict[str, Any]) -> None:
    """Consume one answered broker batch."""
    vault = core.vault
    batch_id = key[len(BROKER_KEY_PREFIX):]
    if batch is None:
        report["invalid"].append({"batch_id": batch_id, "reason": "unknown-batch"})
        return
    state = batch.get("state")
    generation = int(batch.get("generation", 0))
    if state == "consumed":
        report["replay_rejected"].append(batch_id)
        return
    if state == "expired" and not public("_answer_beat_the_deadline")(entry, batch):
        report["late_rejected"].append(batch_id)
        return
    if state not in ("open", "expired"):
        report["invalid"].append({"batch_id": batch_id, "reason": f"state={state}"})
        return
    accepted, answer_mode, outcome = public("_verified_decision")(vault, batch, entry)
    if accepted is None and outcome == _SIG_FAILED:
        public("_cas_batch")(vault, batch_id, expect_state=state, expect_gen=generation,
                              updates={"state": "invalid", "consumed_at": _ts(now)})
        report["invalid"].append({"batch_id": batch_id, "reason": _SIG_FAILED})
        return
    batch_ids = [candidate["id"] for candidate in batch.get("candidates", [])]
    if accepted is None:
        _close_unconsumable(vault, batch_id, state, generation, outcome, now, batch_ids, report)
        return
    _apply_verified_answer(core, now, key, batch, state, generation, accepted, answer_mode, outcome, report)


def _close_unconsumable(vault, batch_id: str, state: str, generation: int, outcome: str,
                        now: _dt.datetime, batch_ids: list[str], report: dict[str, Any]) -> None:
    """Close one invalid owner answer."""
    if public("_cas_batch")(
            vault, batch_id, expect_state=state, expect_gen=generation,
            updates={"state": "consumed", "outcome": outcome, "consumed_at": _ts(now)}):
        report["invalid"].append({"batch_id": batch_id, "reason": outcome})
        report["requeued"].extend(batch_ids)
    else:
        report["invalid"].append({"batch_id": batch_id, "reason": "cas-failed"})


def _apply_verified_answer(core, now: _dt.datetime, key: str, batch: dict[str, Any], state: str,
                           generation: int, accepted: list[str], answer_mode: str, outcome: str,
                           report: dict[str, Any]) -> None:
    """Journal and apply one verified owner answer."""
    vault = core.vault
    batch_id = key[len(BROKER_KEY_PREFIX):]
    public("_write_consume_journal")(vault, {
        "batch_id": batch_id, "state": state, "generation": generation,
        "accepted": accepted, "answer_mode": answer_mode, "outcome": outcome, "ts": _ts(now),
    })
    entered = public("_cas_batch")(
        vault, batch_id, expect_state=state, expect_gen=generation,
        updates={"state": "applying", "outcome": outcome, "answer_key": key,
                 "applying_at": _ts(now)})
    if not entered:
        report["invalid"].append({"batch_id": batch_id, "reason": "cas-failed"})
        public("_clear_consume_journal")(vault)
        return
    public("_apply_batch_decision")(core, batch, accepted, answer_mode, now, report)
    if not public("_cas_batch")(
            vault, batch_id, expect_state="applying", expect_gen=generation + 1,
            updates={"state": "consumed", "consumed_at": _ts(now)}):
        report["invalid"].append({"batch_id": batch_id, "reason": "cas-failed"})
    public("_clear_consume_journal")(vault)


def _consume_batch_answers(core, now: _dt.datetime, answered: dict[str, dict[str, Any]],
                           report: dict[str, Any]) -> None:
    """Consume answered broker batches."""
    batches = {batch.get("batch_id"): batch for batch in _read_jsonl(_batches_path(core.vault))}
    for key, entry in answered.items():
        if key.startswith(BROKER_KEY_PREFIX):
            _consume_batch_answer(core, now, key, entry,
                                  batches.get(key[len(BROKER_KEY_PREFIX):]), report)


def _consume_corrections(vault, now: _dt.datetime, answered: dict[str, dict[str, Any]],
                         report: dict[str, Any]) -> None:
    """Consume answered correction records."""
    pending = proposals_dir(vault) / "corrections-pending"
    if not pending.is_dir():
        return
    for record in sorted(pending.glob("correction-*.json")):
        try:
            payload = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record.unlink(missing_ok=True)
            continue
        key = f"{CORRECT_KEY_PREFIX}{payload.get('round')}:{payload.get('msg_key')}"
        entry = answered.get(key)
        if entry is None:
            continue
        if str(entry.get("answer", "")).strip().lower() == "apply":
            try:
                record_correction(vault, payload["round"], payload["msg_key"],
                                  payload["corrected_bucket"], payload["corrected_tier"],
                                  actor=f"owner-inbox:{key}", ts=_ts(now))
                report["corrections_applied"].append(key)
            except ValueError as exc:
                report["corrections_failed"].append({"key": key, "reason": str(exc)})
        else:
            report["corrections_discarded"].append(key)
        record.unlink(missing_ok=True)


__all__ = ["_consume_batch_answers", "_consume_corrections"]
