"""The undo ledger and lane lock of `cos_mutate` — atomic appends, counters, the undo row

Moved verbatim out of `cos_mutate` (batch-2 drain); every name is re-imported
by the parent so its `cos_mutate` module path is unchanged.
"""
from __future__ import annotations

import datetime as _dt
import hashlib

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
# --- the undo ledger's CLOSED FIELD SET (grounding design D14, sink 11) -----
#: `_cos_undo_ledger_<run>.jsonl` is written at APPLY, long after grounding
#: exists, so the ordering argument that covers the capture corpus is FALSE for
#: it. What covers it is that no key here is free text a model could author —
#: and that argument only holds if it is enforced where the row is SERIALIZED.
#: `_undo_row` ends with `row.update(extra)` and the apply path serializes
#: `dict(intent, …)` merges anyway, so a rule pinned to `_undo_row` constrains
#: nothing that reaches disk. It is enforced in `UndoLedger.append`.
#:
#: THE BOUND IS A SUBSET, NOT AN EQUALITY, and that is a decision rather than a
#: slip (design revision 4, carried finding 3). Exact key-set equality is
#: unsatisfiable as the record stated it: the write-ahead `intent` row
#: serializes 24 keys against this 28-key set, and the four merge keys do not
#: exist yet when it is written. The UPPER bound — nothing outside this set may
#: be serialized — is the half that closes the sink, and it is unambiguous. The
#: lower bound is deliberately not asserted: filling absent keys with `None`
#: would make every intent row claim a `receipts` it does not have.
LEDGER_ROW_KEYS = frozenset({
    # the 23 `_undo_row` names
    "idempotency_key", "conversation_id", "conversation_id_digest", "verb",
    "state", "reason", "account", "message_id", "key_scheme", "thread_id",
    "mutation_lane", "original_folder", "destination_folder", "action_ts",
    "primitive", "connector_result", "verification", "chip", "mode",
    "before_image", "item_id_at_resolve", "changekey_refetched_at", "run",
    # the stamp `append` itself adds
    "ts",
    # and exactly the four the apply/unchip merges add
    "new_item_id", "dispatched", "receipts", "observed_after",
})

from cos_mutate_gates import (  # noqa: E402
    MUTATION_LANE, MutationStop, _read_jsonl, _ts, short)
from cos_mutate_policy import (  # noqa: E402
    DEFAULT_CAPS, DRAFT_FOLDER, STATES, TERMINAL)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cos_reconcile_metrics import applied_counts  # noqa: E402
from cos_mutate_policy import (  # noqa: E402
    DEFAULT_CAPS, DRAFT_FOLDER, STATES, TERMINAL)
import os
import re
from cos_mutate_gates import PRIMITIVE, receipts_shape_ok  # noqa: E402
from cos_reconcile_metrics import VERB_COUNTER  # noqa: E402


def _write_text_atomic(path: Path, text: str, *, mode: int | None = None) -> None:
    """Publish `text` at `path` atomically and never through a symlink.

    EVERY write in this file goes through here (review 2026-08-13, round 7).
    `tests/test_cos_pathguard_io.py::test_no_raw_write_remains_on_a_mount_path`
    scans this module now, for the reason it already scanned the cos package: most of
    these paths are under `<vault>/cos-ops`, which is VM-writable by nature, so
    a plain `write_text` at a predictable name FOLLOWS a symlink an attacker
    pre-created there and the host truncates whatever it points at. The shapes
    file, the dry-run ledger and the canary all sat at exactly such names.
    """
    from brain import cos                                        # noqa: PLC0415

    cos._write_atomic(path, text.encode("utf-8"),
                      **({} if mode is None else {"mode": mode}))


def mutation_lane_lock_path(vault: Path, run_id: str) -> Path:
    """Where the apply's exclusive lock lives — HOST-PRIVATE, off the mount.

    `config.host_lock_dir` is the same app-data directory the COS ledger
    appends and the index writer already lock in, and for the same reason
    (INT-05): a lock file on the VM-writable mount can be unlinked under a live
    holder and replaced at the same name, after which the next holder flocks a
    DIFFERENT inode and both run. Not being reachable is the only fix.

    Keyed on VAULT + RUN, so two different runs never wait on each other.

    THE PHYSICAL VAULT, NOT ITS SPELLING (review 2026-08-13, round 5, C-lock).
    `os.path.abspath` normalises `..` and the cwd and stops there: it never
    resolves a symlink, so the SAME vault reached through a link and through
    its real path produced two different key digests, two different lock files,
    and two applies of one run dispatching side by side — the exact double
    dispatch this lock exists to stop, defeated by how the path was typed.
    `Path.resolve()` answers with the physical path in both spellings.
    """
    from brain import config                                      # noqa: PLC0415

    key = hashlib.sha256(
        f"cos-apply|{Path(vault).resolve()}|{run_id}".encode("utf-8")
    ).hexdigest()[:16]
    return config.host_lock_dir(create=True) / f"{key}.lock"


def _mutation_lane_lock(vault: Path, run_id: str) -> Any:
    """The exclusive lock the APPLY holds for its WHOLE pass.

    Reused, not re-implemented: `brain.lock.writer_lock` is the same portable
    flock primitive the index writer already uses — kernel-released on
    crash/SIGKILL, so there is no stale-pidfile heuristic to get wrong.

    The digest binding above stops an apply consuming a payload nothing
    rehearsed; this stops TWO applies consuming the same rehearsed one at once.
    They are different failures: one dispatches the wrong thing, the other
    dispatches the right thing twice, and `done_keys` only skips a row whose
    undo entry is already on disk — which a concurrent pass has not written yet.
    """
    from brain import lock                                        # noqa: PLC0415

    return lock.writer_lock(mutation_lane_lock_path(vault, run_id),
                            verb=f"cos-apply {run_id}")


# ---------------------------------------------------------------------------
# the undo ledger and the state machine
# ---------------------------------------------------------------------------
class UndoLedger:
    """Append-only, one row per state TRANSITION, latest row per key wins.

    Append-only because the interesting question after a stop is not "what is
    the state" but "what happened, in order" — and a rewritten row cannot answer
    it. `conversation_id` + verb is the idempotency key, per v4.7: OWA ItemIds
    change when an item moves folders, so a move-time id is a session handle and
    never an identity.
    """

    def __init__(self, vault: Path, run_id: str) -> None:
        from brain import cos                                    # noqa: PLC0415
        self.path = cos.run_ops_dir(vault) / f"_cos_undo_ledger_{run_id}.jsonl"
        self.run_id = run_id
        #: post-dispatch rows whose content this ledger refused. Non-empty means
        #: the run must stop AFTER the row is on disk — `refused_ledger_keys` in
        #: the run facts is this list's length.
        self.refused_rows: list[str] = []

    def rows(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.path)

    def latest(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for row in self.rows():
            out[row["idempotency_key"]] = row
        return out

    def append(self, row: dict[str, Any]) -> dict[str, Any]:
        if row.get("state") not in STATES:
            raise MutationStop(f"{row.get('state')!r} is not a state this "
                               f"machine has ({', '.join(STATES)})")
        row = dict(row, run=self.run_id, ts=_ts())
        # --- the closed field set, enforced ON THE SERIALIZED ROW (D14/11) ---
        # THE FAILURE POSTURE DIFFERS BY POSITION, and that is deliberate:
        # never lose the record of a dispatched mutation. A write-ahead
        # `intent` row precedes its bridge call, so a violation there is a
        # programming error with nothing yet on the server — stop before
        # anything dispatches. On a POST-DISPATCH row the mutation has already
        # happened, so the row is written first with the offending content
        # replaced by a marker, and the run stops afterwards. A refused key is
        # never a silently written key.
        write_ahead = row.get("state") == "intent"
        unknown = sorted(set(row) - LEDGER_ROW_KEYS)
        bad_receipts = not receipts_shape_ok(row.get("receipts"), row)
        refusal = ""
        if unknown or bad_receipts:
            refusal = ((f"the undo ledger refuses key(s) {unknown}"
                        if unknown else "")
                       + ("; " if unknown and bad_receipts else "")
                       + ("the `receipts` value is not the page's shape"
                          if bad_receipts else ""))
            if write_ahead:
                raise MutationStop(
                    f"{refusal} — nothing has dispatched, so this row is "
                    "refused rather than written")
            self.refused_rows.append(refusal)
            row = {k: v for k, v in row.items() if k in LEDGER_ROW_KEYS}
            if bad_receipts:
                row["receipts"] = {"refused": "shape"}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # NOT `path.open("a")` — that FOLLOWS a symlink at the final name, and
        # this ledger lives under `<vault>/cos-ops`, which is VM-writable by
        # nature (review 2026-08-13, round 7; the identical defect in
        # `cos._append_jsonl` was R5-1). `cos._open_append_nofollow` is the
        # engine's one sanctioned append: it creates EXCLUSIVELY when absent
        # and, when present, refuses a symlink and confirms the same regular
        # inode on the fd — no check-then-open window either way.
        # It is NOT `cos.append_jsonl` either: that helper does not fsync, and
        # this ledger is a WRITE-AHEAD record written before the bridge call it
        # describes. A lost intent row is a mutation on the server with nothing
        # on disk saying who made it — the run-106 shape. Its per-ledger lock is
        # not needed here: the apply holds the mutation-lane lock, so this file
        # has exactly one writer.
        from brain import cos                                    # noqa: PLC0415

        data = (json.dumps(row, ensure_ascii=False, sort_keys=True)
                + "\n").encode("utf-8")
        fd = cos._open_append_nofollow(self.path)
        try:
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                if n <= 0:
                    raise OSError(f"append made no progress on {self.path.name}")
                view = view[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
        if refusal:
            # THE ROW IS ON DISK. Only now does the run stop: the mutation this
            # row describes already reached the server, and losing its record is
            # the run-106 shape.
            raise MutationStop(
                f"{refusal} — the row was written with the offending content "
                "replaced by a marker, and this run stops here")
        return row

    def applied_counts(self) -> dict[str, int]:
        """What counts against the cap: anything that MIGHT have reached the
        server. `sent` counts — a mutation whose outcome is unknown has already
        spent its blast radius."""
        return applied_counts(self.rows())

    def unfinished(self) -> list[dict[str, Any]]:
        return [r for r in self.latest().values() if r["state"] not in TERMINAL]


# `applied_counts` is imported at the top of this file — see the note there.


def dispatched_counters(vault: Path, run_id: str) -> dict[str, int]:
    """This run's four mutation counters, RECOUNTED from its undo ledger.

    `captured` is not recounted and not returned — see `VERB_COUNTER`.
    """
    return {VERB_COUNTER[v]: n
            for v, n in applied_counts(UndoLedger(vault, run_id).rows()).items()
            if v in VERB_COUNTER}


def record_mutation_counters(vault: Path, run_id: str) -> dict[str, Any]:
    """Write what this apply DISPATCHED into the run's metrics row.

    WHY THIS EXISTS (measured, run 145, 2026-08-16). `cos_driver` appends the
    metrics row hours before the apply, with `archived/marked/drafts_created/
    captured` at 0 and `mutation_lane: "none-read-only"` — which is TRUE of the
    read lane and stays on the record forever, because nothing ever updated it.
    Run 145 archived 11 threads, chipped 3 and drafted 2, and its row of record
    still read all-zero. That is not only a wrong number: `mutation_counts()` is
    what `unledgered_mutations` and `check_plan_binding` corroborate a MISSING
    artifact against, so an all-zero row disarmed both — the anti-vacuity guards
    concluded the run had done nothing, on a night that applied 16 mutations.

    APPEND, NEVER EDIT (REP-01/REP-02, E29(c)). The row is a copy of the driver's
    own row with the counters and the lane corrected, declaring
    `supersedes_run_ts`; `append_metric` refuses anything else, re-verifies the
    ingestion recount and re-joins the host stamps, so this cannot smuggle a
    changed counter past them. Re-running it is a no-op ("unchanged").

    It writes NOTHING when the run dispatched nothing: a read-only night's row is
    already correct, and appending a superseding duplicate would make every
    quiet night look like a corrected rerun.
    """
    from brain import cos                                        # noqa: PLC0415

    ops = cos.run_ops_dir(vault)
    counters = dispatched_counters(vault, run_id)
    out: dict[str, Any] = {"counters": counters, "appended": "not-attempted"}
    recon = _reconcile_module()
    if recon is None:
        out["appended"] = "no-checkers"
        return out
    rows = [r for r in recon._rows(ops / "_cos_metrics.jsonl")
            if (r.get("date"), _run_suffix(str(r.get("run"))))
            == (run_id[:10], _run_suffix(run_id))]
    if not rows:
        out["appended"] = "no-driver-row"
        return out
    prior = rows[-1]
    lane = MUTATION_LANE if any(counters.values()) else prior.get("mutation_lane")
    row = {**prior, **counters, "mutation_lane": lane,
           "run_ts": _ts(), recon.SUPERSEDES: str(prior.get("run_ts"))}
    if all(int(prior.get(k) or 0) == v for k, v in counters.items()) \
            and prior.get("mutation_lane") == lane:
        out["appended"] = "unchanged"                # already correct; no-op
        return out
    out["appended"] = recon.append_metric(ops, row)
    out["supersedes"] = row[recon.SUPERSEDES]
    return out


def _run_suffix(value: str) -> str:
    m = re.search(r"run(\d+)$", str(value)) or re.search(r"^(\d+)$", str(value))
    return m.group(1) if m else str(value)


def _reconcile_module() -> Any:
    try:
        import cos_reconcile_metrics as recon                   # noqa: PLC0415
    except ImportError:
        return None
    return recon


def _undo_row(m: dict[str, Any], resolved: dict[str, Any], *, state: str,
              run_id: str, account: str, **extra: Any) -> dict[str, Any]:
    """The E17 field set, every field present, `null` a recorded value.

    `key_scheme: message-id` with the provider-immutable `InternetMessageId` is
    what E17 requires on the rest lane; `thread_id` (the conversation id) is
    what the UNDO actually keys on, per v4.7. Both are recorded because they
    answer different questions, and a rest-lane row carrying a `convid` key
    scheme would be an E17 mismatch.
    """
    imid = resolved.get("internet_message_id")
    row = {
        "idempotency_key": f"{m['conversation_id']}|{m['verb']}",
        "conversation_id": m["conversation_id"],
        "conversation_id_digest": short(m["conversation_id"]),
        "verb": m["verb"],
        "state": state,
        "reason": m.get("reason"),
        "account": account,
        "message_id": imid,
        "key_scheme": "message-id" if imid else "convid",
        "thread_id": m["conversation_id"],
        "mutation_lane": MUTATION_LANE,
        "original_folder": "Inbox",
        "destination_folder": ({"archive": "archive", "draft": DRAFT_FOLDER}
                               .get(m["verb"], "Inbox")),
        "action_ts": _ts(),
        "primitive": PRIMITIVE[m["verb"]],
        "connector_result": None,
        "verification": None,
        # WHICH CHIP, on the row. Reconciliation asks "is this chip on the
        # thread"; without the name it asked about `undefined` and answered NO —
        # so run 118 recorded `aborted-not-applied` for a chip the mailbox was
        # carrying. An undo needs the name too: removing "the chip" is not an
        # instruction anything can follow.
        "chip": m.get("chip"),
        "mode": m.get("mode"),
        "before_image": resolved.get("before_categories"),
        "item_id_at_resolve": resolved.get("item_id"),
        "changekey_refetched_at": resolved.get("changekey_refetched_at"),
        "run": run_id,
    }
    row.update(extra)
    return row
