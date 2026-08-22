"""COS batch operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._facade import public
from ._attachment_store import attachment_metas
from ._claims_state import _pending_metas
from ._guards import _safe_basename
from ._io import _read_jsonl, _write_atomic
from ._standing_approval import STANDING_ANSWER, standing_approval
from ._layout import _env_days, _parse_ts, _ts, _utcnow, proposals_dir
from ._learning_ledger import log_defect
from ._version_links import version_link_metas

def _batches_path(vault) -> Path:
    return proposals_dir(vault) / "batches.jsonl"

def _write_batches(vault, batches: list[dict[str, Any]]) -> None:
    """Atomic + durable: this file IS the signed state crash recovery reads.

    It used to be a plain truncate-and-rewrite, so a power loss mid-CAS could
    leave it empty or half-written — after which recovery cannot re-derive the
    decision it needs while side effects are already in flight. Same temp ->
    fsync -> replace -> fsync-parent discipline the approved queue uses."""
    p = _batches_path(vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    public("_write_atomic")(p, "".join(json.dumps(b, sort_keys=True) + "\n"
                             for b in batches).encode("utf-8"))

def batch_digest(batch_id: str, created: str, candidates: list[dict[str, str]]) -> str:
    """Canonical digest over the candidate SET (order-independent)."""
    canon = json.dumps(
        {"batch_id": batch_id, "created": created, "schema": BATCH_SCHEMA,
         "candidates": sorted(candidates, key=lambda c: c["id"])},
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()

def open_batches(vault) -> list[dict[str, Any]]:
    return [b for b in _read_jsonl(_batches_path(vault)) if b.get("state") == "open"]

def enqueue_batch(core, now: _dt.datetime | None = None) -> dict[str, Any]:
    """Aggregate ALL pending proposals into ONE signed cos_ingestion_batch and
    enqueue exactly one owner-inbox question for it.

    Backpressure (ing-02): refuses while another batch is open — the owner
    queue holds at most one broker slot; new proposals wait in ``pending`` and
    join the next batch. Fails CLOSED if no signing key resolves (nothing is
    enqueued unsigned). Under the writer lock (B4): it appends to the same
    ``batches.jsonl`` the consumer and the GC rewrite."""
    now = now or _utcnow()
    with vault_writer_lock(core.vault, verb="cos-enqueue"):
        return _enqueue_batch_locked(core, now)

def _backpressure_result(vault) -> dict[str, Any] | None:
    """Describe an open-batch backpressure condition."""
    batches = open_batches(vault)
    if not batches:
        return None
    queued = {candidate["id"] for batch in batches for candidate in batch.get("candidates", [])}
    waiting = [meta["id"] for meta in (
        _pending_metas(vault) + attachment_metas(vault, state="pending") + version_link_metas(vault))
        if meta["id"] not in queued]
    return {"enqueued": False, "reason": "batch-already-open (backpressure)", "waiting": waiting}


def _batch_metas(vault) -> tuple[list[dict[str, Any]], list[str]]:
    """Choose pending candidates for one batch."""
    ingestion = _pending_metas(vault) + attachment_metas(vault, state="pending")
    supersedes = version_link_metas(vault)
    metas = ingestion[:BATCH_SUBCAP_INGESTION]
    metas += supersedes[:BATCH_SUBCAP_SUPERSEDE]
    spare = BATCH_CAP_TOTAL - len(metas)
    if spare > 0:
        metas += ingestion[BATCH_SUBCAP_INGESTION:BATCH_SUBCAP_INGESTION + spare]
    queued_now = {m["id"] for m in metas}
    deferred = [m["id"] for m in ingestion + supersedes if m["id"] not in queued_now]
    return metas, deferred


def _batch_candidates(vault, metas: list[dict[str, Any]], now: _dt.datetime
                      ) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Bind candidate identities into a batch digest."""
    candidates, kept = [], []
    for meta in metas:
        candidate = {"id": meta["id"], "sha256": meta["sha256"]}
        if meta.get("lane") == LANE_ATTACHMENT:
            name = _safe_basename(str(meta.get("filename") or Path(meta["path"]).name))
            if not name:
                log_defect(vault, "attachment-unbatchable",
                           f"{meta['id']}: sidecar filename "
                           f"{str(meta.get('filename'))[:60]!r} is not a bare "
                           f"filename — left in quarantine, not batched",
                           ts=_ts(now))
                continue
            candidate["name"] = name
        candidates.append(candidate)
        kept.append(meta)
    return candidates, kept


def _signed_batch_record(now: _dt.datetime, candidates: list[dict[str, str]]) -> dict[str, Any]:
    """Create one signed open-batch record."""
    from .. import audit

    batch_id = "cosb-" + hashlib.sha256(
        (_ts(now) + json.dumps(candidates, sort_keys=True)).encode()).hexdigest()[:12]
    created = _ts(now)
    digest = batch_digest(batch_id, created, candidates)
    key_obj, _src = audit.resolve_signing_key()  # KeyUnavailable → fail closed
    sig = key_obj.sign(digest.encode("utf-8")).hex()
    ttl_days = _env_days(BATCH_TTL_DAYS_ENV, DEFAULT_BATCH_TTL_DAYS)
    return {
        "schema": BATCH_SCHEMA, "batch_id": batch_id, "created": created,
        "candidates": candidates, "digest": digest, "sig": sig,
        "state": "open", "expires": _ts(now + _dt.timedelta(days=ttl_days)),
        "generation": 0,
    }


def _batch_question(vault, batch_id: str, record: dict[str, Any],
                    metas: list[dict[str, Any]]) -> dict[str, Any]:
    """Render one owner batch question."""
    candidates = record["candidates"]
    digest = record["digest"]

    ids = [c["id"] for c in candidates]
    lines, n_files = _candidate_descriptions(vault, metas)
    gc_days = _env_days(GC_DAYS_ENV, DEFAULT_GC_DAYS)
    n_links = sum(1 for m in metas if m.get("kind") == KIND_SUPERSEDE)
    n_notes = len(ids) - n_files - n_links
    what = ", ".join(p for p in (
        f"{n_notes} note(s)" if n_notes else "",
        f"{n_files} FILE(s)" if n_files else "",
        f"{n_links} VERSION LINK(s)" if n_links else "") if p)
    return {
        "key": BROKER_KEY_PREFIX + batch_id,
        "question": (f"COS ingestion batch {batch_id}: {what} await approval "
                     f"before signing:\n" + "\n".join(f"  - {ln}" for ln in lines)),
        "options": [_ACCEPT_ALL, _REJECT_ALL,
                    "accept: <id,id,...> (partial — list the ids to accept)"],
        "default": _REJECT_ALL,
        "context": f"schema={BATCH_SCHEMA} digest={digest[:16]}… "
                   f"expires={record['expires']}. Only accepted candidates are "
                   f"ever signed; unanswered batches expire and requeue. "
                   + (f"A rejected FILE is NOT deleted — it moves to the "
                      f"host-private expired/ holding area and is recoverable "
                      f"for {gc_days} days." if n_files else "")
                   + ("A VERSION LINK only retires the older note in favour of "
                      "the newer one — both stay readable, and rejecting it "
                      "changes nothing." if n_links else ""),
    }


def _enqueue_batch_locked(core, now: _dt.datetime) -> dict[str, Any]:
    """Create the next signed owner-approval batch."""
    vault = core.vault
    if (backpressure := _backpressure_result(vault)) is not None:
        return backpressure
    metas, deferred = _batch_metas(vault)
    if not metas:
        return {"enqueued": False, "reason": "no-pending-proposals"}
    candidates, metas = _batch_candidates(vault, metas, now)
    if not candidates:
        return {"enqueued": False, "reason": "no-pending-proposals"}
    record = _signed_batch_record(now, candidates)
    batches = _read_jsonl(_batches_path(vault))
    batches.append(record)
    _write_batches(vault, batches)
    batch_id = record["batch_id"]
    question = _batch_question(vault, batch_id, record, metas)
    core.enqueue_question(provenance.scrub(question),
                          source=f"cos-broker:{batch_id}", today=now.date())
    out = {"enqueued": True, "batch_id": batch_id,
           "candidates": [candidate["id"] for candidate in candidates],
           "digest": record["digest"]}
    # THE OWNER'S STANDING ANSWER (owner ruling 2026-08-21). When this vault
    # carries one, the question is answered the moment it is asked, from the
    # host-private record — never routed around. `consume_answers` still runs
    # on the next fold with its per-candidate content CAS and its signing step
    # unchanged, so the only thing that becomes standing is the keystroke.
    # Failure to record the answer is REPORTED, never swallowed: a batch left
    # unanswered is the manual gate, which is the safe direction.
    if (standing := standing_approval(vault)) is not None:
        out["standing_approval"] = {
            "answered": bool(core.answer_question(question["key"], STANDING_ANSWER)),
            "recorded": standing.get("recorded"),
            "reason": standing.get("reason"),
        }
    if deferred:
        # Over the caps: named, not silently dropped. They join the next batch.
        out["deferred"] = deferred
    return out

def _signal_phrases(signals: dict[str, Any]) -> list[str]:
    """The evidence behind a version-link proposal, in plain language. The
    owner is being asked to retire a note; "confidence 0.94" is not a reason."""
    out: list[str] = []
    if signals.get("conversation"):
        out.append("same verified email thread")
    if signals.get("sender"):
        out.append(f"same verified sender ({signals['sender']})")
    if signals.get("name_family"):
        out.append(f"same document name ({signals['name_family']!r})")
    adv = signals.get("version_advance")
    if isinstance(adv, dict):
        # A WORD marker's rank is an internal ordering number: "version marker
        # 0 -> 100" is not a reason to retire a document. Render the words.
        markers = signals.get("version_markers")
        scale = ""
        if isinstance(markers, dict) and isinstance(markers.get("new"), (list, tuple)):
            scale = str(markers["new"][0])
        label = ((lambda r: _MARKER_RANK_LABEL.get(r, r)) if scale == "word"
                 else (lambda r: r))
        out.append(f"version marker {label(adv.get('old'))} -> "
                   f"{label(adv.get('new'))}")
    dup = signals.get("near_duplicate")
    if isinstance(dup, dict):
        out.append(f"content {float(dup.get('score', 0)) * 100:.1f}% similar")
    dates = signals.get("newer_date")
    if isinstance(dates, dict):
        out.append(f"dated {dates.get('old')} -> {dates.get('new')}")
    return out

def _human_bytes(n: int | None) -> str:
    if not isinstance(n, int) or n < 0:
        return "size unknown"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0  # type: ignore[assignment]
    return f"{n} B"

def _candidate_descriptions(vault, metas: list[dict[str, Any]]
                            ) -> tuple[list[str], int]:
    """One PLAIN-LANGUAGE line per batch candidate, plus the file count (B7).

    The owner is being asked to make an irreversible-ish call with `reject
    all` as the stated default, so the question has to say WHAT each item is.
    An attachment is a FILE (name, size, sender) — never a "candidate note":
    the sweep MOVED it out of the download location, so a reject decides the
    fate of what may be the owner's only copy (AGENTS.md §9).
    """
    lines: list[str] = []
    n_files = 0
    for m in metas:
        if m.get("kind") == KIND_SUPERSEDE:
            why = ", ".join(_signal_phrases(m.get("signals") or {}))
            lines.append(
                f"{m['id']} — VERSION LINK: retire {m.get('old_title') or m['old_id']!r} "
                f"({m['old_id']}) in favour of {m.get('new_title') or m['new_id']!r} "
                f"({m['new_id']}) — {why or 'no signals recorded'}")
            continue
        if m.get("lane") != LANE_ATTACHMENT:
            lines.append(f"{m['id']} — note ({m.get('category') or 'unclassified'})")
            continue
        n_files += 1
        try:
            size = Path(m["path"]).stat().st_size
        except OSError:
            size = None
        sender = str((m.get("provenance") or {}).get("sender") or "unknown sender")
        lines.append(
            f"{m['id']} — FILE {m.get('filename') or Path(m['path']).name!r} "
            f"({_human_bytes(size)}, from {sender}, "
            f"category {m.get('category') or 'unclassified'})")
    return lines, n_files

def expire_batches(vault, now: _dt.datetime | None = None) -> list[str]:
    """Expire open batches past their TTL. Their candidates REQUEUE (stay in
    ``pending/``) and join the next batch; a late answer to an expired batch
    is rejected by ``consume_answers``."""
    now = now or _utcnow()
    with vault_writer_lock(vault, verb="cos-expire-batches"):
        batches = _read_jsonl(_batches_path(vault))
        expired: list[str] = []
        changed = False
        for b in batches:
            if b.get("state") != "open":
                continue
            exp = _parse_ts(b.get("expires", ""))
            if exp and exp <= now:
                b["state"] = "expired"
                b["expired_at"] = _ts(now)
                b["generation"] = int(b.get("generation", 0)) + 1
                expired.append(b["batch_id"])
                changed = True
        if changed:
            _write_batches(vault, batches)
        return expired

def close_expired_batch_questions(core, expired_batch_ids: list[str]) -> int:
    """Mark the owner-inbox questions of expired batches ``expired`` so the
    ~5-cap queue never accumulates stale broker slots (ing-02) and a LATE
    answer is refused at the inbox level too (``record_answer`` only touches
    ``open`` entries)."""
    if not expired_batch_ids:
        return 0
    from ..inbox import expire_questions

    entries, closed = expire_questions(
        core._read_inbox(), {BROKER_KEY_PREFIX + b for b in expired_batch_ids},
        expired=_ts())
    if closed:
        core._write_inbox(entries)
    return closed

def parse_batch_answer(answer: str, batch_ids: list[str]) -> tuple[list[str] | None, str]:
    """Parse an owner answer against the batch's candidate ids.

    Returns ``(accepted_ids, outcome)``; ``accepted_ids is None`` means the
    answer was invalid (not consumable — candidates requeue)."""
    a = (answer or "").strip().lower()
    if a == _ACCEPT_ALL:
        return list(batch_ids), "accept-all"
    if a == _REJECT_ALL:
        return [], "reject-all"
    m = _ACCEPT_PARTIAL_RE.match(a)
    if m:
        ids = [s.strip() for s in m.group("ids").split(",") if s.strip()]
        unknown = [i for i in ids if i not in batch_ids]
        if unknown:
            return None, f"invalid-answer: not in batch: {', '.join(unknown)}"
        return ids, "accept-partial"
    return None, f"invalid-answer: unparseable {answer!r}"

__all__ = ['_batches_path', '_write_batches', 'batch_digest', 'open_batches', 'enqueue_batch', '_enqueue_batch_locked', '_signal_phrases', '_human_bytes', '_candidate_descriptions', 'expire_batches', 'close_expired_batch_questions', 'parse_batch_answer']
