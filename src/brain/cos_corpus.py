"""CAP-01/CAP-02 — the CAPTURE CORPUS: the mail text a run actually read.

Nine runs of `_cos_ingestion_ledger_<run>.jsonl` are on disk and the longest
text field in any of them is 219 characters. The ledger records the VERDICT
(`verdict`, `category`, `body_opened`, `tier`) and discards the message text
that verdict was made from. Two consequences, both measured: re-judging
anything costs a 90-minute live run against a real mailbox, and run 64 could
rebuild run 63's ledger with no host-side artifact able to tell.

This module keeps the input. One append-only JSONL file per run, one row per
thread, each row carrying the extracted text, its sha256, and the provenance
needed to join it back to the ledger by `conversation_id`. Nothing here judges
anything — the corpus is evidence; the replay harness (rep-01) re-runs a
judgment over it.

DELIBERATELY NOT SQLITE. An authoritative sqlite corpus would be the ONE
un-rebuildable database in a system whose architecture is "flat files are
truth, every index is a disposable cache" (AGENTS.md §1); sqlite is
single-writer even in WAL mode, and this repo has already spent an arc
(CC-01/CC-02) on exactly that contention; and 120 rows a night is not a
database problem.

DELIBERATELY SMALL (2026-08-02, owner ruling). An earlier draft of this module
grew a chained close digest, a second per-run lock over the existing append
lock, rollback inside the shared append primitive, per-row fsync, chmod
read-back verification and an inode re-check before unlink — 872 lines
defending a plaintext file on a single-user laptop against an attacker who
could simply read it. All of that is gone. What remains is the boring version:
write the row, hash the text, refuse on the VM, prune whole files, and REPORT
damage on read instead of policing it on write. The independent check on a
run's row count is the run's own ledger (wir-03 joins them) — not a
self-referential digest over the same bytes.

HOST-BROKER ONLY. Every entry point refuses on ``role=vm``. The mount proof
cannot catch a VM here: ``host_private_base()`` on a VM resolves to the VM's
OWN app-data directory, which is genuinely off the VM's vault mount, so the
proof passes and this module would write mail bodies inside the sandbox.

WHERE IT LIVES (CAP-02). Under ``config.host_private_base()``, proven off every
VM-visible root by ``config.proven_off_mount`` — the same two functions the
approved queue (INT-01), the attachment anchors (INT-04) and the writer lock
(INT-05) resolve through. It is NOT under ``<vault>/``, so ``notes.scan_vault``
never reaches it and no indexing rule had to be weakened: the exclusion is
structural, not a filter. Owner-only (0700 dir, 0600 rows, 0400 once closed),
every row classified MNPI.

AND FOR HOW LONG (CAP-02). ``prune`` deletes whole expired run files, and the
nightly ``brain maintain`` daily retention block CALLS IT — real mail bodies
ageing out is a schedule, not an operator's memory. ``$BRAIN_COS_CORPUS_DAYS``
(default 30) is the window. ``corpus_summary`` reports the date that fold last
ran on THIS host, because an engine that ships the fold still deletes nothing
where the nightly has never fired.

MNPI IS THE FILE'S FLOOR AND IS NOT NEGOTIABLE BY OVERLAY. An email-derived
SOURCE defaults to MNPI and an explicit ``overlay/keywords/`` mapping may lower
THAT note (AGENTS.md §2). A corpus file holds every thread the run read,
unfiltered and pre-judgment, so its tier is the floor of its most sensitive row.

THE TEXT IS NOT SCRUBBED, AND THAT IS DELIBERATE. ``provenance.scrub`` runs on
every surface that SERIALIZES a claim outward. The corpus is the opposite
direction: it must be byte-faithful to what the judge saw, or a replay over it
is not a replay. Each row records ``secret_findings`` (the NAMES of the
patterns present, never the values), so a corpus known to hold credentials is
visible at rest.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from . import config, cos, provenance

CORPUS_SCHEMA = "cos_capture_corpus/v1"
CLOSE_SCHEMA = "cos_capture_corpus_close/v1"

#: A close that certified ZERO rows, retracted. See :func:`reopen_run` — a
#: close carrying rows is never retractable, and there is no other reopen path.
REOPEN_SCHEMA = "cos_capture_corpus_reopen/v1"

#: The one classification a corpus row can carry. See the module docstring.
CLASSIFICATION = "MNPI"

_CORPUS_DIRNAME = "cos-corpus"
_SUFFIX = ".jsonl"

#: Retention, in whole days, over WHOLE RUN FILES. 30 matches the existing COS
#: GC window (``cos.DEFAULT_GC_DAYS``). It is a ceiling on how long unfiltered
#: mail bodies sit at rest, so the pressure on it is downward.
RETENTION_DAYS_ENV = "BRAIN_COS_CORPUS_DAYS"
DEFAULT_RETENTION_DAYS = 30

#: ``maintain-state.json`` key the nightly retention fold stamps after it calls
#: :func:`prune`. It is what makes ``pruned_by_a_scheduled_fold`` a FACT about
#: this host rather than a claim about the code: an engine that ships the fold
#: still deletes nothing on a host where the nightly has never run.
PRUNE_MARKER = "_cos_corpus_prune"

#: Per-row text ceiling. The run's extraction budget is 4000 characters (6000
#: for the raw-page fallback), so this is three orders of headroom and only
#: trips on a runaway page dump. Past it the row is REFUSED, never truncated: a
#: silently shortened row is a corpus that lies about what the judge read.
MAX_TEXT_BYTES = 1 << 20

#: Ceiling on every non-body field. All arrive from the browser leg, so they
#: are untrusted input; none has a legitimate form near this size. Over it the
#: row is REFUSED, not trimmed — the conversation id is the JOIN KEY, and a
#: shortened key joins to nothing while looking like a good row.
MAX_FIELD_CHARS = 4096

#: How far ahead of the host clock a run id's date may sit. Retention is
#: computed from that date, so a run id dated 2099 is a corpus that never
#: expires. Two days, not zero, because the host clock and a run that started
#: before midnight UTC legitimately disagree.
MAX_FUTURE_DAYS = 2


class CorpusUnsafe(config.HostPathUnsafe):
    """The corpus cannot be placed provably off every VM-visible root."""


class CorpusHostOnly(RuntimeError):
    """A corpus operation was attempted on the Cowork VM leg."""


class CorpusClosed(RuntimeError):
    """The corpus for this run is closed — it is write-once."""


class CorpusRefused(ValueError):
    """A row was refused rather than written in a shape that would mislead."""


class NoBodiesToJudge(CorpusRefused):
    """Judging was attempted over a corpus in which NO row carries body text."""


def _host_only(what: str) -> None:
    """Refuse on ``role=vm`` — the gate ``write``/``ingest``/``supersede`` pass
    through, made at the MODULE boundary rather than at each call site.

    ``config.host_private_base()`` resolves to the VM's OWN app-data directory
    when it runs there, which is genuinely off the VM's vault mount — so
    ``proven_off_mount`` PASSES on a VM. The mount proof cannot catch this;
    only the role can.
    """
    if config.role() == config.ROLE_VM:
        raise CorpusHostOnly(
            f"refused: {what} is host-broker only. The capture corpus holds "
            f"unfiltered MNPI mail bodies and is written on the host that "
            f"signs the audit chain — never on the Cowork VM.")


# -- location (CAP-02) --------------------------------------------------------
def corpus_dir(vault=None) -> Path:
    """The corpus directory NAME — unresolved, unproven, never created here."""
    return (config.host_private_base() / _CORPUS_DIRNAME
            / cos.approved_vault_identity(vault))


def corpus_root(vault=None) -> Path:
    """``corpus_dir`` resolved and PROVEN off every VM-visible root.

    Raises :class:`CorpusUnsafe` otherwise, and does NOT fall back to another
    location. Unfiltered mail bodies on a VM-readable path is the one outcome
    this module may not produce.

    KNOWN LIMITATION, accepted deliberately (s01, 2026-08-02):
    ``config.proven_off_mount`` decides by PATHNAME ANCESTRY after
    ``resolve()``, which is not proof against a case-insensitive APFS spelling
    of a mount root, a bind mount, or a hardlink, and there is a
    check-then-traverse window before the write. Both are real; both live in
    pre-existing SHARED code the approved queue, the attachment anchors and the
    writer lock also route through, so hardening it is a systemic change, not
    this module's — and a second local resolver is how the first one ends up
    subtly weaker.
    """
    _host_only("the COS capture corpus")
    try:
        return config.proven_off_mount(corpus_dir(vault), vault,
                                       what="COS capture corpus")
    except config.HostPathUnsafe as exc:
        raise CorpusUnsafe(str(exc)) from None


def _corpus_run_id(run_id: Any, *, now: _dt.datetime | None = None) -> str:
    """``run_id`` checked for SHAPE and for a real, non-future calendar date.

    ``cos.checked_run_id`` proves the shape — a run id reaches the host from
    VM-writable directory names, so it is untrusted input. The date check is
    the retention half: ``2026-99-99-run1`` is a well-shaped id whose date
    never parses, so it would be a file no window ever expires.
    """
    rid = cos.checked_run_id(run_id)
    day = _run_date(rid)
    if day is None:
        raise CorpusRefused(
            f"run id {rid!r} does not start with a real calendar date. "
            f"Retention reads the date from the FILENAME, so a corpus under "
            f"this id would never expire.")
    today = (now or cos.utcnow()).date()
    if (day - today).days > MAX_FUTURE_DAYS:
        raise CorpusRefused(
            f"run id {rid!r} is dated more than {MAX_FUTURE_DAYS} days ahead "
            f"of the host clock; retention would hold it for that long.")
    return rid


def corpus_path(vault, run_id: Any) -> Path:
    """The corpus file for one run."""
    return corpus_root(vault) / f"{cos.checked_run_id(run_id)}{_SUFFIX}"


def _ensure(vault) -> Path:
    d = corpus_root(vault)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.secure_file_permissions(d, 0o700)
    return d


# -- reading ------------------------------------------------------------------
def _read(path: Path, run: str | None = None
          ) -> tuple[list[dict[str, Any]], dict[str, Any] | None, int]:
    """Every data row, the close IN FORCE if there is one, and the count of
    lines that would not parse.

    "In force" is the LAST lifecycle record, not "a close record exists": the
    file is append-only, so retracting a close appends a reopen rather than
    deleting anything, and a reopened corpus is an open one. A file that was
    closed, reopened and closed again reads as closed on the SECOND close, with
    the first still on disk where a later reader can see it.

    ``run``, when the caller knows which run's corpus this is, is what a
    lifecycle record has to NAME to count as this corpus's lifecycle record.
    Every close and reopen carries its ``run``, so a line naming a different one
    got here by damage — two corpora concatenated, a mis-targeted append — and
    is not this file's close no matter where it lands. It counts as a line that
    would not parse, which is all it is: :func:`corpus_status` reports it and
    the reopen gate refuses on it, the same as any other unreadable line. There
    is no repair path and no override. :func:`read_corpus_file` reads a fixture
    by path with no run id in hand, so it passes none and this does not apply.

    Damage is REPORTED, not policed: a corpus with one torn line is still
    evidence, and ``corpus_status`` is where a replay sees that its denominator
    is short. Reading the whole file is fine — 120 rows a night.
    """
    rows: list[dict[str, Any]] = []
    lifecycle: dict[str, Any] | None = None
    bad = 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                if not isinstance(rec, dict):
                    bad += 1
                elif rec.get("schema") == CORPUS_SCHEMA:
                    rows.append(rec)
                elif rec.get("schema") in (CLOSE_SCHEMA, REOPEN_SCHEMA):
                    if run is not None and rec.get("run") != run:
                        bad += 1          # another run's lifecycle record
                    else:
                        lifecycle = rec
                else:
                    bad += 1
    except FileNotFoundError:
        return [], None, 0
    if lifecycle is not None and lifecycle.get("schema") != CLOSE_SCHEMA:
        lifecycle = None
    return rows, lifecycle, bad


def read_corpus(vault, run_id: Any) -> list[dict[str, Any]]:
    """The run's data rows, in the order they were captured."""
    return _read(corpus_path(vault, run_id), cos.checked_run_id(run_id))[0]


def read_corpus_file(path: Any) -> tuple[list[dict[str, Any]], dict[str, Any] | None, int]:
    """Rows, close record and unparseable-line count from ONE corpus file BY
    PATH — no host-private root resolution, no run-id shape check.

    The replay harness (rep-01) reads a COMMITTED fixture corpus that lives in
    the repo, not under ``corpus_root``; without this it would need its own
    JSONL reader, and a second reader is how the two quietly disagree about
    what a damaged file contains.
    """
    return _read(Path(path))


def is_closed(vault, run_id: Any) -> bool:
    """Has this run's corpus been closed?"""
    return _read(corpus_path(vault, run_id),
                 cos.checked_run_id(run_id))[1] is not None


def corpus_status(vault, run_id: Any) -> dict[str, Any]:
    """What a replay needs to trust this corpus as a denominator.

    ``complete`` means the run closed AND the rows on disk match the count it
    declared. Anything else — an unclosed file (the capture stage died), a
    short count (a torn append), unparseable lines, or a repeated
    ``conversation_id`` (a retry double-counted) — is reported here rather than
    refused at write time, so the replay decides what to do with it.
    """
    rid = cos.checked_run_id(run_id)
    rows, close, bad = _read(corpus_path(vault, rid), rid)
    ids = [r.get("conversation_id") for r in rows]
    dupes = sorted({i for i in ids if i and ids.count(i) > 1})
    declared = close.get("rows") if close else None
    reasons = []
    if close is None:
        reasons.append("never closed — the capture stage did not finish")
    elif declared != len(rows):
        reasons.append(f"declared {declared} rows, {len(rows)} on disk")
    if bad:
        reasons.append(f"{bad} unparseable line(s)")
    if dupes:
        reasons.append(f"{len(dupes)} repeated conversation id(s)")
    return {
        "run": rid,
        "rows": len(rows),
        "closed": close is not None,
        "declared_rows": declared,
        "bad_lines": bad,
        "duplicate_conversation_ids": dupes,
        "complete": not reasons,
        "reason": "; ".join(reasons),
    }


def judgeable(rows: list[dict[str, Any]], *, source: str
              ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The rows a judge may be handed, and the count of the ones it may NOT.

    WIR-02 — THE BODY TEXT IS THE JUDGE'S INPUT, so a missing body pass is a
    MISSING INPUT and not a thread to judge on its subject line. Run 65 did not
    execute Phase 1.6's body pass and then judged 58 threads ``no-substance``
    whose bodies were never opened; run 64 read the instruction six times and
    skipped it anyway. An obligation a long-running agent has to REMEMBER is
    not in force, so this is a precondition rather than a reminder: judging
    cannot start without the input it judges.

    THE PARTIAL CASE, decided: **some bodyless rows are NORMAL and are never a
    refusal.** Phase 1.6 rule 1½ forbids opening an UNREAD thread and caps
    opens at 20 a night, so a real corpus always carries rows with no body —
    refusing on any of them would refuse every honest night. The bodied rows
    are judged; the bodyless ones are counted and handed back BESIDE them, in
    one return value the caller cannot take the rows without. That count is
    what stops a short candidate rate from reading as thin mail.

    **ZERO bodied rows is the refusal**, and it is exactly run 65's shape.
    """
    bodied = [r for r in rows if str(r.get("text") or "").strip()]
    report = {"source": str(source), "rows": len(rows),
              "judgeable": len(bodied), "bodyless": len(rows) - len(bodied)}
    if not bodied:
        raise NoBodiesToJudge(
            f"refused to judge {source}: 0 of {len(rows)} corpus row(s) carry "
            f"body text"
            + (" (the corpus is empty)" if not rows
               else f" — all {len(rows)} row(s) are bodyless")
            + ". The judge's input IS the message body, so this is a MISSING "
              "INPUT, not a quiet night: the body pass did not run. Nothing "
              "was judged. Fix the body pass, never this check.")
    return bodied, report


def list_runs(vault) -> list[str]:
    """Run ids with a corpus on disk, oldest first."""
    try:
        entries = sorted(corpus_root(vault).iterdir())
    except (OSError, config.HostPathUnsafe):
        return []
    out = []
    for p in entries:
        if p.is_file() and p.name.endswith(_SUFFIX):
            rid = p.name[:-len(_SUFFIX)]
            if cos.RUN_ID_RE.match(rid):
                out.append(rid)
    return out

# The corpus writers and the retention/prune fold live in their own modules
# since the 2026-08-16 size ratchet; re-exported so every
# `brain.cos_corpus.<name>` caller is unchanged.
from .cos_corpus_retention import (  # noqa: E402,F401  (facade re-export)
    _keep_days as _keep_days,
    _run_date as _run_date,
    corpus_summary as corpus_summary,
    last_scheduled_prune as last_scheduled_prune,
    prune as prune,
    retention_days as retention_days,
)
from .cos_corpus_write import (  # noqa: E402,F401  (facade re-export)
    _bounded_json as _bounded_json,
    _opt as _opt,
    append_thread as append_thread,
    close_run as close_run,
    reopen_run as reopen_run,
)

