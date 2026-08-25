"""DLV-11 — the audited batch-apply verb: ONE transaction for a whole backfill.

The backfill s07 applies is N frontmatter stamps and M supersessions against a
live reference vault. Done the obvious way that is N + M unsynchronised
transactions: ``brain write`` calls ``write_note`` WITHOUT taking the writer
lock (``core/_capture.py``), and every ``supersede`` takes its OWN
(``core/_supersession.py``). So the hourly nightly can interleave between any
two rows, retire a note the batch is about to stamp, or rewrite one it already
checked. This module is the verb that makes the whole set one transaction.

Four properties, each one a review finding rather than a nicety:

* **ONE acquisition of the outer writer lock** wraps every stamp and every
  supersede in the batch. The lock is per-process re-entrant
  (``lock.py``), so the N inner ``supersede`` acquisitions underneath are
  same-process no-ops and cannot deadlock against it.

* **ONE batch-level expected audit head.** The plan carries the chain tip the
  owner's decision was made against, and the batch refuses if the chain has
  moved since. Without it the apply asks for a precondition it never had: the
  proposal was true when it was written, not when it is run.

* **A per-note journal, so a batch that dies halfway resumes to a consistent
  state** and never leaves half the approved set stamped. It reuses the
  ``_supersession_journal`` shape — version stamp, canonical checksum,
  ``_write_atomic_durable`` + parent fsync, host-private and off the mount —
  rather than inventing a second journal convention, because the file names
  ids a host would then sign.

  Recovery is FORWARD, and the journal is what makes forward safe. Every row
  records the sha256 of its note BEFORE the batch (``pre``) and AFTER the
  edit this batch will make (``post``), so a resumed run reads each unfinished
  row's note and finds it in exactly one of three states: ``pre`` (untouched —
  apply it), ``post`` (the write landed and the crash beat the journal update
  — skip it), or NEITHER, which means something outside this batch rewrote
  that note and the row is refused rather than blindly re-stamped. That is a
  rollback journal's guarantee — the approved set is never half-applied — held
  without issuing N compensating signed writes over an ADDITIVE change to
  reach it.

* **A row whose recorded hashes no longer match is REFUSED, and refuses the
  batch.** Note content, frontmatter, and the effective payload bytes are each
  bound. The substitution case — same id, same title, different bytes — is
  exactly what the recorded hashes exist to catch, and it is the reason the
  preflight is all-or-nothing: a partially applied backfill whose refused rows
  were the drifted ones is the worst of both outcomes.

The payload hash resolves through ``deliverables_sync._payload_of``, the same
function the census and the shelf use, so "the bytes the owner approved" means
the same thing here as it does on the shelf.
"""
from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
from typing import Any

from . import frontmatter as fm

#: Bump when a journal row's shape changes incompatibly. A journal carrying an
#: unknown version is not acted on — fail closed, exactly as the supersession
#: journal does.
JOURNAL_V = 1
LOCK_VERB = "deliverables-apply"
STAMP = "stamp"
SUPERSEDE = "supersede"
ACTIONS = (STAMP, SUPERSEDE)


class BatchRefused(RuntimeError):
    """The batch was refused BEFORE anything was written.

    Carries ``reasons`` (one line per offending row, or one for the batch) so
    the caller reports what drifted rather than "it failed"."""

    def __init__(self, summary: str, reasons: list[str]) -> None:
        super().__init__(f"{summary}: " + "; ".join(reasons))
        self.reasons = reasons


class BatchPending(RuntimeError):
    """A DIFFERENT batch is unfinished. Two half-applied backfills interleaved
    is the one state no journal can untangle, so this refuses rather than
    guessing which one the caller meant."""


# ---------------------------------------------------------------------------
# the journal — same shape, checksum and durability as _supersession_journal
# ---------------------------------------------------------------------------

def journal_path(vault: Any) -> Path:
    """Beside the drop lane's journal, and off the mount for its reason: this
    file names ids and projects a host would then SIGN, so a VM-writable copy
    would be an unsigned host write command."""
    from . import config
    from .ingest import deliverables as DLV

    return DLV.journal_path(vault).with_name(
        f"batch-{config.vault_slug8(vault)}.json")


def batch_id(plan: dict[str, Any]) -> str:
    """A deterministic id for THIS plan's content. Re-running the identical
    plan resumes it; running a different one against a pending journal is the
    :class:`BatchPending` refusal above — no id to pass around, and no way to
    resume the wrong batch by mistyping one."""
    payload = json.dumps({"audit_head": plan.get("audit_head"),
                          "rows": plan.get("rows")},
                         sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _checksum(core: Any, record: dict[str, Any]) -> str:
    """The supersession journal's own canonical digest, not a second one."""
    return core._supersede_journal_checksum(record)


def write_journal(core: Any, record: dict[str, Any], path: Path | None = None,
                  *, version: int = JOURNAL_V) -> None:
    """The rollback/resume record, written atomically and durably BEFORE the
    write it covers. ``path``/``version`` are parameters so the absorption
    driver next door reuses this shape rather than growing a third journal
    convention (there are already two: this one and the drop lane's)."""
    from . import config
    from .core._durability import _write_atomic_durable

    core._require_durable_replace("journal a deliverables batch-apply")
    record = {"v": version, **record}
    record["checksum"] = _checksum(core, record)
    path = path or journal_path(core.vault)
    core._mkdir_durable(path.parent)
    config.secure_file_permissions(path.parent, 0o700)
    _write_atomic_durable(path, json.dumps(record).encode("utf-8"), mode=0o600)


def clear_journal(core: Any, path: Path | None = None) -> None:
    from .core._durability import _fsync_dir_strict

    path = path or journal_path(core.vault)
    path.unlink(missing_ok=True)
    _fsync_dir_strict(path.parent)


def read_journal(core: Any, path: Path | None = None,
                 *, version: int = JOURNAL_V) -> dict[str, Any] | None:
    """The pending batch, or ``None``. An unreadable or torn journal RAISES:
    a batch record that cannot be trusted must not be silently discarded, for
    the same reason the supersession journal fails closed."""
    path = path or journal_path(core.vault)
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BatchRefused("pending batch journal is unreadable", [str(exc)])
    found = record.get("v") if isinstance(record, dict) else None
    if found != version:
        raise BatchRefused("pending batch journal is unreadable",
                           [f"schema version {found!r}, expected {version}"])
    want = _checksum(core, record)
    if record.get("checksum") != want:
        raise BatchRefused("pending batch journal is unreadable", [
            f"checksum mismatch (recorded {record.get('checksum')!r}, computed "
            f"{want!r}) — the record is torn or was edited"])
    return record


# ---------------------------------------------------------------------------
# row shapes
# ---------------------------------------------------------------------------

def row_key(row: dict[str, Any]) -> str:
    if row.get("action") == SUPERSEDE:
        return f"{SUPERSEDE}:{row.get('old_id')}->{row.get('new_id')}"
    return f"{STAMP}:{row.get('note_id')}"


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def note_path(core: Any, note_id: str) -> Path | None:
    """The on-disk note for ``note_id``, read from the INDEX like ``supersede``
    does — one resolution rule for both halves of a batch."""
    row = core.index.get(note_id)
    if not row:
        return None
    path = Path(row["path"])
    return path if path.is_file() else None


def stamped_text(text: str, row: dict[str, Any]) -> str:
    """The note's bytes after an ADDITIVE stamp: ``deliverable: true`` plus the
    approved ``project:``. ``type:`` is never rewritten — that is what keeps a
    mass stamp reversible by deleting two lines."""
    updates: dict[str, Any] = {"deliverable": "true"}
    project = str(row.get("project") or "").strip()
    if project:
        updates["project"] = project
    return fm.set_keys(text, updates)


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------

def preflight(core: Any, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """``(plan_rows, reasons)`` — every row resolved and every recorded hash
    re-checked UNDER THE LOCK. A non-empty ``reasons`` means nothing is
    written: the caller approved a set, not a subset."""
    planned: list[dict[str, Any]] = []
    reasons: list[str] = []
    seen: set[str] = set()
    for row in rows:
        key = row_key(row)
        if key in seen:
            reasons.append(f"{key}: named twice in one batch")
            continue
        seen.add(key)
        try:
            planned.append(_preflight_row(core, row, key))
        except BatchRefused as exc:
            reasons.extend(exc.reasons)
    return planned, reasons


def _preflight_row(core: Any, row: dict[str, Any], key: str) -> dict[str, Any]:
    action = row.get("action")
    if action not in ACTIONS:
        raise BatchRefused("row", [f"{key}: unknown action {action!r}"])
    if action == SUPERSEDE:
        return _preflight_supersede(core, row, key)
    return _preflight_stamp(core, row, key)


def _preflight_stamp(core: Any, row: dict[str, Any], key: str) -> dict[str, Any]:
    note_id = str(row.get("note_id") or "")
    path = note_path(core, note_id)
    if path is None:
        raise BatchRefused("row", [f"{key}: no readable note for id {note_id!r}"])
    before = path.read_text(encoding="utf-8")
    reasons = _hash_mismatches(core, row, key, before, path)
    meta, _ = fm.parse_text(before)
    project = str(row.get("project") or "").strip()
    existing = str(meta.get("project") or "").strip()
    if project and existing and existing != project:
        reasons.append(
            f"{key}: the note already carries project {existing!r} and the "
            f"approved row says {project!r} — a retarget is not an additive "
            "stamp, so it is refused rather than applied")
    if reasons:
        raise BatchRefused("row", reasons)
    after = stamped_text(before, row)
    return {"key": key, "action": STAMP, "note_id": note_id,
            "rel": path.relative_to(core.vault).as_posix(),
            "pre": _sha_text(before), "post": _sha_text(after),
            "project": project}


def _hash_mismatches(core: Any, row: dict[str, Any], key: str,
                     before: str, path: Path) -> list[str]:
    """Every recorded hash the row still has to match: the whole note file, its
    frontmatter block alone, and the effective payload bytes."""
    reasons: list[str] = []
    want_note = str(row.get("note_sha256") or "")
    if want_note and want_note != _sha_text(before):
        reasons.append(f"{key}: note content changed since it was approved")
    want_fm = str(row.get("frontmatter_sha256") or "")
    if want_fm:
        block = fm.split(before)
        got = _sha_text(block[0] if block else "")
        if got != want_fm:
            reasons.append(f"{key}: frontmatter changed since it was approved")
    want_payload = str(row.get("payload_sha256") or "")
    if want_payload:
        reasons.extend(_payload_mismatch(core, key, path, want_payload))
    return reasons


def _payload_mismatch(core: Any, key: str, path: Path, want: str) -> list[str]:
    """The ARCHIVED PAYLOAD BYTES, resolved exactly as the shelf resolves them.

    A raw note's own ``sha256:`` is computed over extracted Markdown, not over
    the original file, so hashing the note alone leaves the bytes the owner
    actually approved unbound."""
    from . import deliverables_ledger as L, deliverables_sync as ds, notes as _notes

    root = Path(core.vault)
    note = _notes.load_note(path, root)
    payload, kind, _tier, problem = ds._payload_of(note, root)
    if payload is None or kind == "unresolved":
        return [f"{key}: the approved payload is unresolvable ({problem or kind})"]
    try:
        got = L.sha256_file(payload)
    except OSError as exc:
        return [f"{key}: the approved payload could not be read ({exc})"]
    if got != want:
        return [f"{key}: the archived payload bytes changed since they were "
                f"approved (recorded {want[:12]}…, found {got[:12]}…)"]
    return []


def _preflight_supersede(core: Any, row: dict[str, Any], key: str) -> dict[str, Any]:
    """A supersede row binds BOTH notes' bytes. The hashes are handed to
    ``core.supersede(expect=…)`` as well, which re-verifies them inside its own
    acquisition — the same values, checked twice, because a precondition
    checked only out of band is TOCTOU by construction."""
    old_id, new_id = str(row.get("old_id") or ""), str(row.get("new_id") or "")
    reasons: list[str] = []
    paths: dict[str, Path] = {}
    for side, note_id in (("old", old_id), ("new", new_id)):
        path = note_path(core, note_id)
        if path is None:
            reasons.append(f"{key}: no readable note for {side} id {note_id!r}")
            continue
        paths[side] = path
        want = str(row.get(f"{side}_sha256") or "")
        if want and want != _sha_text(path.read_text(encoding="utf-8")):
            reasons.append(f"{key}: {side} note changed since it was approved")
    if reasons:
        raise BatchRefused("row", reasons)
    old_meta, _ = fm.parse_text(paths["old"].read_text(encoding="utf-8"))
    already = str(old_meta.get("superseded_by") or "").strip()
    if already and already != new_id:
        raise BatchRefused("row", [
            f"{key}: {old_id!r} is already retired in favour of {already!r} — "
            "this engine refuses to re-supersede an already-superseded note, "
            "so the row is refused here rather than failing mid-batch"])
    expect = {f"{side}_sha256": row[f"{side}_sha256"]
              for side in ("old", "new") if row.get(f"{side}_sha256")}
    return {"key": key, "action": SUPERSEDE, "old_id": old_id, "new_id": new_id,
            "expect": expect,
            "pre": _sha_text(paths["old"].read_text(encoding="utf-8")),
            "rel": paths["old"].relative_to(core.vault).as_posix()}


# ---------------------------------------------------------------------------
# the verb
# ---------------------------------------------------------------------------

def apply_batch(core: Any, plan: dict[str, Any], *,
                reason: str = "") -> dict[str, Any]:
    """Apply an owner-approved backfill plan as ONE transaction.

    ``plan`` is ``{"audit_head": <chain tip when the plan was decided>,
    "rows": [...]}``. Rows are ``{"action": "stamp", "note_id", "project",
    "note_sha256", "frontmatter_sha256", "payload_sha256"}`` or
    ``{"action": "supersede", "old_id", "new_id", "old_sha256", "new_sha256"}``
    — every hash optional in the SHAPE and, in practice, supplied by the
    proposal that the owner approved.

    Raises :class:`BatchRefused` (nothing written) on a moved chain, a
    vanished note, a drifted hash, or a duplicate row; :class:`BatchPending`
    when a different batch is unfinished.
    """
    from .lock import vault_writer_lock

    core._require_host("apply a deliverables backfill (signs every row)")
    rows = list(plan.get("rows") or [])
    with vault_writer_lock(core.vault, verb=LOCK_VERB):
        return _apply_locked(core, plan, rows, reason)


def _apply_locked(core: Any, plan: dict[str, Any], rows: list[dict[str, Any]],
                  reason: str) -> dict[str, Any]:
    # An interrupted supersession is rolled back FIRST: its journal holds two
    # pre-images, and a batch that stamped on top of a half-written chain
    # would make that rollback rewrite this batch's own work away.
    core._recover_pending_supersede()

    bid = batch_id(plan)
    pending = read_journal(core)
    if pending is not None and pending.get("batch_id") != bid:
        raise BatchPending(
            f"a different batch ({pending.get('batch_id')}, opened "
            f"{pending.get('opened')}, {len(pending.get('done') or [])} of "
            f"{len(pending.get('rows') or [])} rows applied) is unfinished — "
            "finish or clear it before starting another")
    done: list[str] = list(pending.get("done") or []) if pending else []

    want_head = str(plan.get("audit_head") or "")
    if not want_head:
        raise BatchRefused("the plan carries no expected audit head", [
            "a batch binds itself to the chain state its rows were read "
            "against; without one the apply asks for a precondition it never "
            "had. Record `audit_head` when the plan is built."])
    head = core.audit.head()
    # A journal for THIS plan is the record that this check ALREADY passed: the
    # journal is opened BEFORE the first row is applied, and a plan carrying a
    # different `audit_head` hashes to a different `batch_id` and was refused as
    # pending above. So a resume must not re-ask — the batch's own signed writes
    # move the chain. Keying this off `done` instead was a permanent refusal:
    # row one's `write_note` moves the head BEFORE the journal records that row,
    # so a crash in that one-write window left `done == []` beside a moved head
    # and every later resume refused the plan forever. A genuinely stale plan
    # has no journal and still trips here; an OUTSIDE writer during the
    # interruption is caught per row by the pre/post check in `_apply_row`.
    if want_head != head and pending is None:
        raise BatchRefused("the audit chain moved since this plan was decided", [
            f"expected head {want_head[:12]}…, found {head[:12]}… — every row's "
            "recorded state was read against a chain that has since been "
            "written to; nothing was applied"])

    if pending is not None:
        # A RESUMED batch replans NOTHING. The journal's rows carry the ``pre``
        # and ``post`` images recorded against the vault as it stood when the
        # plan was accepted; re-running the preflight would recompute ``pre``
        # from whatever each note says NOW, which is precisely the state the
        # three-way check exists to catch. Reusing the journal is what makes
        # "neither the pre-image nor the post-image" observable at all — the
        # resume is the only moment an outside writer has had a window.
        planned = [r for r in (pending.get("rows") or []) if r["key"] not in done]
    else:
        planned, refusals = preflight(core, rows)
        if refusals:
            raise BatchRefused("the approved batch was refused", refusals)

    record = {
        "op": "deliverables-apply", "batch_id": bid,
        "opened": (pending or {}).get("opened")
        or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "audit_head": want_head, "rows": planned, "done": done,
    }
    if planned:
        write_journal(core, record)
    applied: list[dict[str, Any]] = []
    for entry in planned:
        applied.append(_apply_row(core, entry, reason))
        done.append(entry["key"])
        record["done"] = list(done)
        write_journal(core, record)
    clear_journal(core)
    # ONE reindex for the whole batch, and only if a STAMP landed: `supersede`
    # already reindexes its own pair, and a stamp changes frontmatter the index
    # caches — leaving it stale would make `bases-query --where deliverable=...`
    # disagree with the vault until the next nightly.
    reindexed = (core.sync(drain=False)
                 if any(r.get("written") for r in applied) else None)
    return {"batch_id": bid, "audit_head_before": head,
            "reindexed": {k: reindexed.get(k) for k in ("added", "updated")}
            if isinstance(reindexed, dict) else None,
            "audit_head_after": core.audit.head(),
            "rows": len(rows), "applied": applied,
            "resumed": bool(pending), "already_done": len(done) - len(applied)}


def _apply_row(core: Any, entry: dict[str, Any], reason: str) -> dict[str, Any]:
    """One row, or the reason it needed none. Never a blind rewrite: a note
    whose bytes are neither the recorded pre-image nor the expected post-image
    was changed by something outside this batch."""
    path = Path(core.vault) / entry["rel"]
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    current = _sha_text(text) if text else ""
    if entry["action"] == STAMP and current == entry["post"]:
        return {**_result(entry), "skipped": "already stamped"}
    if entry["action"] == SUPERSEDE and _already_superseded(text, entry):
        return {**_result(entry), "skipped": "already superseded"}
    if current != entry["pre"]:
        raise BatchRefused("the batch was interrupted and the vault moved", [
            f"{entry['key']}: the note is neither the state this batch recorded "
            "nor the state this batch would write — something outside the "
            "batch changed it; the remaining rows were not applied"])
    if entry["action"] == STAMP:
        core.write_note(entry["rel"], stamped_text(text, entry),
                        reason=reason or f"deliverable stamp: {entry['note_id']}")
        return {**_result(entry), "written": entry["rel"]}
    out = core.supersede(entry["old_id"], entry["new_id"],
                         reason=reason or "deliverable version family",
                         expect=entry["expect"])
    return {**_result(entry), "superseded": [entry["old_id"], entry["new_id"]],
            "reindexed": out.get("reindexed")}


def _already_superseded(old_text: str, entry: dict[str, Any]) -> bool:
    """Whether THIS supersession already landed — read off the old note's own
    frontmatter, not inferred from the fact that its bytes moved. A crash
    between the second signed write and the journal update lands here, and the
    row must be skipped rather than re-run (``supersede`` refuses to
    re-supersede, so a blind retry would fail the whole resumed batch)."""
    if not old_text:
        return False
    meta, _ = fm.parse_text(old_text)
    return str(meta.get("superseded_by") or "").strip() == entry["new_id"]


def _result(entry: dict[str, Any]) -> dict[str, Any]:
    return {"key": entry["key"], "action": entry["action"]}
