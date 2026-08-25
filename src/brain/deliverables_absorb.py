"""DLV-12's driver — absorb an approved folder of finished outputs, resumably.

The owner keeps hundreds of finals in a folder by hand. Absorbing them means
running each one through the SAME drop lane s01 built (``inbox/_deliverables/``
→ archived original + signed raw note + anchor), never a bespoke import path,
because a second ingest route is a second thing to keep correct forever.

It is built HERE, beside the batch verb, for the reason the batch verb is: the
session that applies this to a live vault has no test gate, and a resume
journal over an ingest sequence is not code to write there.

**Why a journal at all, when the batch verb next door rolls forward from
recorded hashes:** ``raw/`` is IMMUTABLE. A failure at row 90 of 120 cannot be
rolled back and must not re-ingest rows 1-89, so resumability is the only
available guarantee and it has to be exact.

**The row-done test is the vault's own ingest manifest, not this journal.**
``ingest-manifest.json`` maps the sha256 of an original file's BYTES to the
raw id it became — the same record the pipeline's own duplicate detection
reads. So "is this row already absorbed" is answered by the vault, and the
journal only records what was attempted and what it produced. A journal that
was the sole authority would be a second opinion about the vault's contents,
and the disagreement would surface as a double ingest.

**Two journals, one recovery order, and it is exercised** (see
``tests/test_deliverables_apply.py``). The drop lane has its OWN inner journal
spanning the archived original, the raw note and the anchor, and a kill INSIDE
a row lands between them. The order is: the drain's own recovery pass first
(``run_ingest`` calls ``deliverables.recover`` before it scans for candidates,
so the interrupted anchor is finished from the inner journal), and only then
this driver's manifest check, which now sees a completed row. Reversing that
order re-stages a payload the vault already holds.

**Copy, never move.** The owner's folder is left exactly as found; a
reinstatement is that nothing was ever touched. It is also what makes a resume
always possible — the source of every unfinished row is still there.

**And nothing already waiting in the drop lane is overwritten either.** That
promise has two sides: a hand-dropped file sitting in the lane is a payload not
yet ingested, the inbox is gitignored and never indexed, so a copy landing on
its name destroys the only copy there is — silently, while the row reports an
ordinary success. The preflight refuses the whole batch instead. A leftover
staged copy of the row's OWN bytes is the one exemption, because rewriting a
file with the bytes it already holds destroys nothing, and refusing it would
make an interrupted run unresumable.

**A row's declared tier governs the FOLDER, not the row — so the lane has to be
this batch's alone before any row is staged.** The tier is declared to the lane
the only way the lane accepts one: a ``.classification`` control file beside the
payload. That marker is folder-wide, and the drain empties the WHOLE lane in one
pass, so every un-ingested file co-resident with a staged row is admitted at
that row's tier instead of the lane's fail-closed default. A NAME collision is
only the loudest case of it — a differently-named file leaks just as far, and a
file one level over in a project folder with no marker of its own leaks under a
row staged at the lane root. So the preflight refuses when the lane holds ANY
payload this batch did not itself stage, and the marker write sits inside the
same ``try``/``finally`` that restores it, so a failed copy cannot leave one
governing the folder either. All three are one defect: a folder-wide
declaration written for one file.

**Already present still gets an anchor.** A file whose bytes the vault already
holds is not skipped: it is byte-identical, so re-ingesting it would be a
duplicate, but it still needs the anchor note that puts it on the shelf.
Skipping it outright was the review finding this branch exists for.

**Already present is still subject to the lane's cross-tier refusal.** The
branch above skips the drain, and the drain is what refuses a row declared
ABOVE the tier the vault already holds those bytes at. Reporting that as a
success left the payload retrievable at the lower tier — the leak the refusal
exists for — so the branch asks the same question directly.
"""
from __future__ import annotations

import datetime
import hashlib
import re
import shutil
from pathlib import Path
from typing import Any

from . import classification as CLS
from . import deliverables_apply as _batch
from .ingest import deliverables as DLV

JOURNAL_V = 1
LOCK_VERB = "deliverables-absorb"

#: Dot-directories are tool state, not documents (`.trust/`, `.memo-loop/`).
#: The caller filters its own list; this is the belt, and it is cheap.
_SKIP_PREFIX = "."

#: A project is ONE folder name under the lane, so anything that could make it
#: more than one — or make it unusable — is refused HERE, in the all-or-nothing
#: preflight, not later inside the writer lock. Both separators, because `\` is
#: one on Windows and `sub\..\..\escape` otherwise resolves outside the lane;
#: and every control character, because a NUL raises inside `mkdir` after other
#: rows have already been staged and ingested.
_UNSAFE_PROJECT = re.compile(r"[/\\]|[\x00-\x1f\x7f]")


class AbsorbRefused(RuntimeError):
    """The preflight refused. Nothing was staged and nothing was ingested."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("absorption refused: " + "; ".join(reasons))
        self.reasons = reasons


def journal_path(vault: Any) -> Path:
    from . import config

    return DLV.journal_path(vault).with_name(
        f"absorb-{config.vault_slug8(vault)}.json")


def row_sha(source: Path) -> str:
    """The sha256 of the file's BYTES — the same key ``ingest-manifest.json``
    uses, so a row's identity and the vault's record of it are one value."""
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _lane_clash(lane: Path, project: str, name: str, sha: str) -> str:
    """Why staging this row would destroy something already in the lane, or
    ``""`` if it would not.

    A file already sitting at the row's target name is a payload waiting to be
    ingested — most often a hand drop, which the lane exists for. Copying over
    it is unrecoverable: the inbox is gitignored and never indexed, so the
    overwritten bytes exist nowhere else.

    The exemption is bytes: a target byte-identical to this row's source is
    this driver's OWN staged copy, left behind by a run that died between the
    copy and the drain. Rewriting it with the same bytes destroys nothing, and
    refusing it would make that run unresumable. Anything else — different
    bytes, a directory, a symlink — is somebody else's and is refused.
    """
    target = (lane / project if project else lane) / name
    if not target.exists() and not target.is_symlink():
        return ""
    if target.is_file() and not target.is_symlink() and row_sha(target) == sha:
        return ""
    return (f"a different {name!r} is already waiting in the drop lane at "
            f"{target} — staging this row would overwrite it, and the inbox is "
            "not indexed and not versioned, so those bytes exist nowhere else")


def _lane_strays(lane: Path, inbox: Path,
                 planned: list[dict[str, Any]]) -> list[str]:
    """Payloads already in the drop lane that this batch did not stage itself.

    Staging a row declares its tier in a FOLDER-WIDE control file, and the drain
    empties the whole lane in one pass — so anything else waiting there is
    ingested at that row's declared tier instead of the lane's fail-closed
    default. That is the leak whatever the stray is called and whichever folder
    it sits in, which is why this asks about the lane's CONTENTS rather than
    about one target name.

    ``DLV.scan`` answers "what will the drain see", so it is what is asked —
    a second opinion here would be a second thing to keep in step with the lane.

    The one exemption is this driver's own leftover: a file at a planned row's
    exact target path whose bytes are that row's. A run killed between the copy
    and the drain leaves exactly that, and refusing it would make the resume
    the journal exists for impossible.
    """
    own = {(lane / r["project"] if r["project"] else lane) / r["name"]: r["sha"]
           for r in planned}
    strays: list[str] = []
    for path in sorted(DLV.scan(inbox)):
        sha = own.get(path)
        if sha is not None and row_sha(path) == sha:
            continue
        strays.append(str(path))
    return strays


def preflight(vault: Any,
              rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve every row before ANY of them is staged. All-or-nothing: a
    half-absorbed folder with no record of which half is the state this exists
    to prevent.

    It takes the vault because one of the questions it has to answer is about
    the vault's own drop lane — see ``_lane_clash``. Asking it later, inside
    the writer lock, is too late: earlier rows are signed by then and ``raw/``
    is immutable."""
    inbox = Path(vault) / "inbox"
    lane = inbox / DLV.DELIVERABLES_DIRNAME
    planned: list[dict[str, Any]] = []
    reasons: list[str] = []
    seen: set[str] = set()
    for row in rows:
        source = Path(str(row.get("source") or ""))
        if not source.is_file() or source.is_symlink():
            reasons.append(f"{source}: not a readable regular file")
            continue
        if source.name.startswith(_SKIP_PREFIX):
            reasons.append(f"{source}: a dot-file is tool state, not a document")
            continue
        tier = str(row.get("classification") or "")
        if tier not in CLS.TIERS:
            reasons.append(
                f"{source}: classification {tier!r} is not a recognised tier — "
                "a drop-zone ingest declares Internal and the tier guard only "
                "raises against an existing higher-tier twin, so an undeclared "
                "row would enter Internal and reach an Internal-capped reader")
            continue
        declared_project = str(row.get("project") or "")
        project = declared_project.strip()
        # `not project` catches a name that is nothing but whitespace: it would
        # otherwise be silently accepted as "no project at all" and the row
        # would land in the lane root, unprojected, with no refusal anywhere.
        if declared_project and (not project or project.startswith(_SKIP_PREFIX)
                                 or _UNSAFE_PROJECT.search(project)):
            reasons.append(
                f"{source}: unusable project folder name {declared_project!r}")
            continue
        sha = row_sha(source)
        if sha in seen:
            reasons.append(f"{source}: byte-identical to another row in this batch")
            continue
        clash = _lane_clash(lane, project, source.name, sha)
        if clash:
            reasons.append(f"{source}: {clash}")
            continue
        seen.add(sha)
        planned.append({"sha": sha, "source": str(source), "project": project,
                        "classification": tier, "name": source.name})
    strays = _lane_strays(lane, inbox, planned) if planned else []
    if strays:
        # Named, not just counted — the owner has to go find them. Capped, and
        # the cap SAYS so, because a listed subset read as the whole list is
        # how a partial report becomes a wrong one.
        shown = ", ".join(strays[:5])
        if len(strays) > 5:
            shown += f", and {len(strays) - 5} more"
        reasons.append(
            f"the drop lane still holds {len(strays)} payload(s) this batch did "
            f"not stage — {shown} — and staging any row declares a tier for the "
            "whole folder that the drain would then apply to them instead of "
            "the lane's fail-closed default; ingest or move them first")
    return planned, reasons


def absorb(core: Any, rows: list[dict[str, Any]], *,
           dry_run: bool = False) -> dict[str, Any]:
    """Ingest every approved row through the drop lane, resumably.

    Returns ``{"rows": N, "absorbed": [...], "resumed": bool}``. Each result
    row carries its ``sha``, the raw ``id`` it became, its anchor path, and
    ``already_present``/``recovered`` when either applies.
    """
    from .lock import vault_writer_lock

    core._require_host("absorb a deliverables folder (ingests and signs)")
    planned, reasons = preflight(core.vault, rows)
    if reasons:
        raise AbsorbRefused(reasons)
    if dry_run:
        return {"rows": len(planned), "absorbed": [], "dry_run": True,
                "planned": planned}
    with vault_writer_lock(core.vault, verb=LOCK_VERB):
        return _absorb_locked(core, planned)


def _absorb_locked(core: Any, planned: list[dict[str, Any]]) -> dict[str, Any]:
    # The INNER journal FIRST — this is the documented recovery order, and it
    # is the only thing that makes it real. The drop lane spans three writes
    # per file (archived original, signed raw note, anchor) under its own
    # forward-recovery journal, and a kill INSIDE a row lands between them.
    # Finishing that here, BEFORE any row asks the manifest whether it is
    # already absorbed, is what stops a resume re-staging a payload the vault
    # already holds and leaving a second copy of it in `raw/`.
    recovery: dict[str, Any] = {}
    DLV.recover(core, recovery)

    path = journal_path(core.vault)
    pending = _batch.read_journal(core, path)
    record = {
        "op": "deliverables-absorb", "batch_id": _fingerprint(planned),
        "opened": (pending or {}).get("opened")
        or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "rows": planned, "done": list((pending or {}).get("done") or []),
    }
    if pending and pending.get("batch_id") != record["batch_id"]:
        raise _batch.BatchPending(
            f"a different absorption ({pending.get('batch_id')}, opened "
            f"{pending.get('opened')}) is unfinished — finish or clear it first")
    _batch.write_journal(core, record, path, version=JOURNAL_V)

    out: list[dict[str, Any]] = []
    for row in planned:
        if row["sha"] in record["done"]:
            continue
        result = _absorb_row(core, row)
        out.append(result)
        if not result.get("id"):
            # A row that produced no raw id is NOT done. Marking it done anyway
            # let a resume skip it, clear the journal and report success over a
            # file the vault never took — with the only run that would have said
            # so being the one that died. It stays open instead: this run's
            # report names it, and the next run re-attempts it.
            continue
        record["done"] = [*record["done"], row["sha"]]
        _batch.write_journal(core, record, path, version=JOURNAL_V)
    _batch.clear_journal(core, path)
    return {"rows": len(planned), "absorbed": out, "resumed": bool(pending),
            "recovered": recovery.get("deliverables") or []}


def _absorb_row(core: Any, row: dict[str, Any]) -> dict[str, Any]:
    """One row: finish it if the vault already holds it, else stage and drain.

    The manifest is consulted FIRST and again after the drain, because those
    are two different questions — "was this row already done" and "what did
    this drain make of it"."""
    existing = _manifest_id(core.vault, row["sha"])
    if existing:
        conflict = _refuse_low_twin(core.vault, existing, row)
        if conflict is not None:
            return conflict
        return {**_row_result(row, existing),
                "already_present": True,
                "anchor": _ensure_anchor(core, existing, row)}
    marker, prior = _lane_marker(core.vault, row)
    try:
        # Both writes live INSIDE this try. The marker governs admission for
        # the whole folder, so a copy that raises must not leave this row's
        # tier behind deciding what a later drop is admitted at.
        marker.write_text(row["classification"], encoding="utf-8")
        shutil.copy2(row["source"], marker.parent / row["name"])
        report = core.ingest_dropzone()
    finally:
        _restore_marker(marker, prior)
    existing = _manifest_id(core.vault, row["sha"])
    if not existing:
        return {**_row_result(row, ""), "ingested": False,
                "reason": _why_not(report, row)}
    return {**_row_result(row, existing), "ingested": True,
            "anchor": _ensure_anchor(core, existing, row)}


def _fingerprint(planned: list[dict[str, Any]]) -> str:
    joined = "\n".join(sorted(f"{r['sha']}:{r['project']}" for r in planned))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _row_result(row: dict[str, Any], note_id: str) -> dict[str, Any]:
    return {"sha": row["sha"], "source": row["source"], "id": note_id}


def _manifest_id(vault: Path, sha: str) -> str:
    """The raw id these BYTES already became, or ``""``. The vault's own
    record, never this driver's journal."""
    from .ingest import pipeline as facade

    return str(facade._load_manifest(Path(vault)).get(sha) or "")


def _refuse_low_twin(vault: Path, existing_id: str,
                     row: dict[str, Any]) -> dict[str, Any] | None:
    """Refuse an already-present row whose declared tier is ABOVE the tier the
    vault already holds these bytes at. ``None`` means no conflict.

    The drain is what normally catches this (``deliverable_tier_conflict``),
    and the already-present branch never drains — so without this the row is
    reported as an ordinary success while the payload stays independently
    retrievable at the LOWER tier, which is the whole leak the lane's refusal
    exists to prevent. Same predicate as the drain's, imported rather than
    restated, so the two can never drift apart.

    Per row, not the batch: the drain quarantines one drop and carries on, and
    the row carries no id, so it is never marked done and the next run — after
    the owner has raised the existing note through the audited path — re-runs
    it.
    """
    existing_tier = DLV.low_twin_tier(Path(vault), existing_id,
                                      row["classification"])
    if existing_tier is None:
        return None
    return {**_row_result(row, ""), "already_present": True, "ingested": False,
            "reason": "deliverable_tier_conflict", "existing_id": existing_id,
            "existing_classification": existing_tier,
            "declared_classification": row["classification"]}


def _lane_marker(vault: Path, row: dict[str, Any]) -> tuple[Path, str | None]:
    """Make this row's folder and report its tier marker with whatever the
    marker held BEFORE the row — but write NOTHING.

    Both writes belong to the caller's ``try``, whose ``finally`` restores this
    ``prior``: the marker governs ADMISSION for every later drop into that
    folder, so one left behind — by a completed row OR by a copy that raised —
    admits a hand-dropped file at this row's declared tier instead of the lane's
    fail-closed default, or replaces the owner's own standing declaration for
    good.

    The copy is unguarded on that path on purpose: whether it would land on
    somebody else's pending drop, or share the folder with one, is the
    preflight's question (``_lane_clash`` and ``_lane_strays``), asked before
    any row is staged. Asking it again here could only refuse a row mid batch,
    after earlier rows are signed into an immutable ``raw/``.
    """
    lane = Path(vault) / "inbox" / DLV.DELIVERABLES_DIRNAME
    target_dir = lane / row["project"] if row["project"] else lane
    target_dir.mkdir(parents=True, exist_ok=True)
    marker = target_dir / DLV.CLASSIFICATION_FILENAME
    prior = marker.read_text(encoding="utf-8") if marker.is_file() else None
    return marker, prior


def _restore_marker(marker: Path, prior: str | None) -> None:
    """Put the lane back exactly as this row found it."""
    if prior is None:
        marker.unlink(missing_ok=True)
    else:
        marker.write_text(prior, encoding="utf-8")


def _ensure_anchor(core: Any, note_id: str, row: dict[str, Any]) -> str:
    """The anchor for ``note_id``, written now if it is missing.

    This is what keeps an ALREADY-PRESENT file on the shelf: its bytes are in
    the vault, the ordinary duplicate path leaves it there, and without an
    anchor carrying ``deliverable: true`` nothing would ever shelve it.

    An anchor that ALREADY exists is left exactly as it is — its tier is not
    re-checked and not raised. Two reasons, and they point the same way: the
    anchor is a signed note, and raising an already-signed note's tier is an
    audited owner act (``brain write``), never something an ingest path does by
    itself — the same rule ``refuse_low_twin`` states; and the anchor body is
    the one place an owner's commentary about the output lives, so rewriting it
    from the row would silently destroy it. What this driver can reach the
    existing anchor with is a tier no HIGHER than the raw note's own, because a
    higher declared one is refused before this is called.
    """
    rel = DLV.anchor_rel(note_id)
    if (Path(core.vault) / rel).is_file():
        return rel
    return DLV.write_anchor(core, {
        "slug": note_id, "title": row["name"], "project": row["project"] or None,
        "classification": _anchor_tier(Path(core.vault), note_id, row),
        "created": datetime.date.today().isoformat(),
    })


def _anchor_tier(vault: Path, note_id: str, row: dict[str, Any]) -> str:
    """The tier for an anchor written NOW: the HIGHER of the row's declared
    tier and the tier the vault ALREADY holds for these bytes.

    NOW is the whole guarantee — an anchor that already exists is never
    revisited (see ``_ensure_anchor``), so this decides the tier of a first
    write and nothing else.

    On the already-present path the drain never runs, so the declared tier
    would otherwise reach a fresh anchor unchecked. An MNPI payload absorbed a
    second time under a declared ``Internal`` would then carry an Internal
    anchor — the anchor's title, project link and existence readable by a
    reader the raw note itself denies. It is also the belt on the INGESTED
    path, where the lane's own anchor write was deferred and the tierguard may
    have raised the raw note above what the row declared. ``CLS.rank`` fails an
    unlabelled or unrecognised value to MNPI, so a malformed tier on either
    side lands at the most restrictive rung rather than being trusted.
    """
    from .ingest import pipeline_files

    ranks = [CLS.rank(row["classification"])]
    existing = pipeline_files.existing_note_classification(vault, note_id)
    if existing is not None:
        ranks.append(CLS.rank(existing))
    return CLS.TIERS[max(ranks)]


def _why_not(report: dict[str, Any], row: dict[str, Any]) -> str:
    """Name what the drain did with a row that produced no raw id — a
    quarantine reason or a refusal, never a bare 'it did not work'."""
    for bucket in ("quarantined", "skipped", "duplicates"):
        for item in report.get(bucket) or []:
            if str(item.get("file") or "") == row["name"]:
                return f"{bucket}: {item.get('reason') or item.get('existing_id') or '?'}"
    return "the drain reported no outcome for this file"
