"""Host-private audit drift-disposition triage (INT-02)."""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotation-only; a runtime import here would be a cycle
    from .audit_chain import AuditChain

DRIFT_DISPOSITIONS_FILENAME = "audit-drift-dispositions.json"

# M-7. Until 2026-09-02 `content_drift` hashed the note's text AFTER Python's
# universal-newline translation, so every disposition recorded before that date
# is pinned to a hash that CANNOT distinguish the bytes it was ruled on from
# bytes that differ only in carriage returns -- which is precisely the drift the
# byte hash exists to detect. Those pins are marked, kept, and never allowed to
# explain drift again; see `mark_legacy_hash_convention`.
BYTE_HASH_CUTOVER = "2026-09-02"
LEGACY_TEXT_CONVENTION = "text (legacy, unverifiable)"


def _write_dispositions(path: Path, payload: dict) -> None:
    """Stage into a sibling temp file, then rename over the target.

    This file is the ONLY record of the owner's drift rulings — 108 of them on
    the reference vault. `load_drift_dispositions` fails CLOSED on a file it
    cannot parse, and failing closed reads as zero rulings, so an in-place
    write killed mid-flight (disk full, host kill, power loss) would silently
    turn every explained finding back into an unexplained one. `os.replace` is
    atomic within a filesystem, so a reader sees the old file or the new one.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def drift_dispositions_path(vault: Path) -> Path:
    """Host-private triage file, OFF the VM-visible mount (2026-08-07).

    Raises ``config.HostPathUnsafe`` when it cannot resolve somewhere the
    Cowork VM is unable to reach — see ``config.audit_drift_dispositions_path``
    for why this file in particular must be out of reach."""
    from . import config

    return config.audit_drift_dispositions_path(vault)


def legacy_drift_dispositions_path(vault: Path) -> Path:
    """Where this file lived until 2026-08-07: on the shared mount. Read ONLY
    by the one-time carry-forward below; nothing else may consult it again."""
    return Path(vault) / ".brain" / DRIFT_DISPOSITIONS_FILENAME


def migrate_drift_dispositions(vault: Path) -> str | None:
    """Carry a pre-2026-08-07 triage file forward to the host-private location.

    Copy, never move: the destination is the only thing read from now on, and
    deleting the operator's historical record on their behalf is not this
    function's call. Returns a one-line note when it acted, else ``None``.

    The carried-forward records came from a VM-writable path, so each one is
    QUARANTINED (``unverified_migrated: true``) rather than laundered into
    looking host-authored: ``match_disposition`` refuses a quarantined record,
    so it cannot silently explain away drift on a note the VM itself tampered
    with and pre-seeded a matching legacy disposition for. It still counts
    toward ``unexplained`` (fails closed) but carries a distinct
    ``disposition_reason`` so an operator sees "needs re-confirmation" rather
    than a generic unexplained-drift alarm indistinguishable from real
    tampering. Re-confirming one (the operator re-triages it through the
    normal disposition flow, on the host-private file) drops the flag."""
    legacy = legacy_drift_dispositions_path(vault)
    if not legacy.is_file():
        return None
    try:
        dest = drift_dispositions_path(vault)
    except Exception:  # noqa: BLE001 — unsafe destination: stay fail-closed
        return None
    if dest.exists():
        return None
    try:
        raw = json.loads(legacy.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    records = raw.get("dispositions") if isinstance(raw, dict) else raw
    if not isinstance(records, list):
        return None
    records = [
        {**r, "unverified_migrated": True} if isinstance(r, dict) else r
        for r in records
    ]
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            dest.parent.chmod(0o700)
        except OSError:
            pass
        _write_dispositions(dest, {"dispositions": records,
                                   "migrated_from_mount": legacy.as_posix()})
    except OSError:
        return None
    return (f"carried {len(records)} drift disposition(s) forward from the shared "
            f"mount to {dest} — they were recorded where a Cowork VM could write, "
            f"so re-check them if you have any reason to doubt that host")


def _is_legacy_convention_record(record: dict) -> bool:
    """True when this pin was ruled on under the OLD normalising text hash.

    The discriminator is the record's own ``recorded`` date against the
    cutover. A record with no ``recorded`` field is NOT stamped: the
    disposition file is host-private and off the VM-visible mount, so an
    undated record there is an informal host-authored one, not an attack
    surface -- and stamping it would break the only way an operator has of
    writing a disposition by hand.
    """
    if record.get("convention"):
        return str(record["convention"]) == LEGACY_TEXT_CONVENTION
    recorded = str(record.get("recorded") or "")[:10]
    return bool(recorded) and recorded < BYTE_HASH_CUTOVER


def mark_legacy_hash_convention(vault: Path) -> str | None:
    """Stamp every pre-cutover pin ``convention: "text (legacy, unverifiable)"``
    -- ONCE. Returns a one-line note when it acted, else ``None``.

    Deliberately NOT a re-key, and this is the whole point. There is no trusted
    byte anchor for a note the owner ruled on under the normalising hash: a
    CR-only edit leaves that hash unchanged, so a pin built on it cannot tell a
    note whose bytes are untouched from one that drifted in exactly the way
    this finding exists to surface. Any predicate derived from the old hash
    therefore re-blesses that drift PERMANENTLY, because the migration runs
    once. So the pins stay exactly as recorded -- the historical record is not
    rewritten -- and the notes they covered come back as UNEXPLAINED for the
    owner to re-rule under the byte convention. The count of pins left legacy
    IS the size of that re-ruling task, and it is recorded in the file.

    Records added after the cutover carry no marker and are byte-keyed."""
    try:
        path = drift_dispositions_path(vault)
    except Exception:  # noqa: BLE001 — HostPathUnsafe: nothing safe to stamp
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(raw, dict) and raw.get("migrated_hash_convention"):
        return None  # already stamped; it runs once by construction
    records = raw.get("dispositions") if isinstance(raw, dict) else raw
    if not isinstance(records, list):
        return None
    stamped = [
        {**r, "convention": LEGACY_TEXT_CONVENTION}
        if isinstance(r, dict) and _is_legacy_convention_record(r) else r
        for r in records
    ]
    legacy = sum(1 for r in stamped if isinstance(r, dict)
                 and r.get("convention") == LEGACY_TEXT_CONVENTION)
    out = dict(raw) if isinstance(raw, dict) else {}
    out["dispositions"] = stamped
    out["migrated_hash_convention"] = {
        "date": date.today().isoformat(),
        "cutover": BYTE_HASH_CUTOVER,
        "legacy_pins": legacy,
        "note": ("content drift now hashes the note's RAW BYTES. These pins were "
                 "ruled on under a hash taken after newline normalisation, which "
                 "cannot see a CR-only edit, so they are kept as the historical "
                 "record and never explain drift again. The notes they covered "
                 "surface as unexplained until re-ruled."),
    }
    try:
        _write_dispositions(path, out)
    except OSError:
        return None
    return (f"marked {legacy} drift disposition(s) as recorded under the legacy "
            f"text-hash convention; the notes they covered now surface as "
            f"unexplained drift until re-ruled against the note's raw bytes")


def load_drift_dispositions(vault: Path) -> dict[str, dict]:
    """``{path: record}`` from the triage file; ``{}`` when absent, unreadable,
    or resolvable only to a VM-visible path. Fails CLOSED into "nothing is
    explained" — an unreadable or untrustworthy disposition file must never
    silently clear a drift count."""
    migrate_drift_dispositions(vault)
    mark_legacy_hash_convention(vault)
    try:
        path = drift_dispositions_path(vault)
    except Exception:  # noqa: BLE001 — HostPathUnsafe and anything else
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    records = raw.get("dispositions") if isinstance(raw, dict) else raw
    if not isinstance(records, list):
        return {}
    return {r["path"]: r for r in records
            if isinstance(r, dict) and isinstance(r.get("path"), str)}


def _candidate_disposition(record: dict, dispositions: dict[str, dict]) -> dict | None:
    """The on-file disposition matching THIS record's path+issue+hash, before
    the quarantine check — shared by ``match_disposition`` and the
    needs-re-confirmation labelling in ``AuditChain.content_drift``."""
    d = dispositions.get(str(record.get("path")))
    if not isinstance(d, dict) or not d.get("disposition"):
        return None
    if d.get("issue") != record.get("issue"):
        return None
    key = "expected_sha256" if record.get("issue") == "missing" else "actual_sha256"
    if d.get(key) != record.get(key):
        return None
    return d


def match_disposition(record: dict, dispositions: dict[str, dict]) -> dict | None:
    """The disposition explaining THIS drift record, or ``None``.

    Matching requires the same path, the same issue, and the same observed
    hash the disposition was recorded against — so a further edit re-surfaces
    as unexplained instead of hiding under an old ruling. A disposition
    carried forward from the pre-2026-08-07 VM-writable legacy path
    (``unverified_migrated``) is REFUSED here — it was recorded somewhere the
    Cowork VM could write, so it must never silently explain drift the VM
    itself could have both caused and pre-seeded a matching legacy record
    for. It still surfaces (see ``content_drift``'s "needs re-confirmation"
    reason), just not as an accepted explanation."""
    d = _candidate_disposition(record, dispositions)
    if d is None or d.get("unverified_migrated"):
        return None
    if d.get("convention") == LEGACY_TEXT_CONVENTION:
        # Ruled on under the normalising text hash (M-7). That hash is blind to
        # a CR-only edit, so accepting it here would let the one drift this
        # change exists to detect stay explained forever.
        return None
    return d


def drift_disposition_label(record: dict, dispositions: dict[str, dict]) -> tuple:
    """``(disposition, reason)`` for one drift record — the single place
    ``AuditChain.content_drift`` derives both fields, so the quarantine
    labelling stays out of that function's own branching (complexity)."""
    match = match_disposition(record, dispositions)
    if match is not None:
        return match.get("disposition"), match.get("reason")
    candidate = _candidate_disposition(record, dispositions)
    if candidate is not None and candidate.get("unverified_migrated"):
        # A pre-2026-08-07 legacy disposition would have explained this
        # record, but it was recorded on the VM-writable mount — refused as
        # an explanation (match_disposition), surfaced distinctly here so it
        # reads as "needs re-confirmation" rather than an indistinguishable
        # fresh tamper alarm.
        return None, "needs_reconfirmation_migrated_from_mount"
    if candidate is not None and candidate.get("convention") == LEGACY_TEXT_CONVENTION:
        # The pin matches, but it was recorded against the pre-2026-09-02
        # normalised text hash — kept as history, refused as an explanation,
        # and named distinctly so the owner sees a re-ruling queue rather than
        # a wall of indistinguishable fresh tamper alarms.
        return None, "needs_reruling_text_hash_convention"
    return None, None


def drift_summary(vault: Path, chain: "AuditChain") -> dict:
    """``{"total": n, "unexplained": n, "records": [...]}`` — the one place the
    unexplained count is derived, so every health surface gates on the same
    number.

    ponytail: full hash pass, no sampling — 0.3s over a 2,600-note vault on the
    reference deployment. If a vault ever gets big enough for that to hurt
    hourly, cache it on (path, mtime, size) rather than sampling: a sampled
    "0 drift" is the false all-clear this whole item exists to remove."""
    records = chain.content_drift(Path(vault))
    return {
        "total": len(records),
        "unexplained": sum(1 for r in records if not r.get("disposition")),
        "records": records,
        # What the number above can SPEAK FOR. A path the chain never bound
        # with a content hash cannot drift, so "0 unexplained" over a chain
        # that is half unbound is not the all-clear it reads as.
        "coverage": chain.content_coverage(),
    }


def rerule_legacy_pins(
    vault: Path, records: list[dict], *, apply: bool = False
) -> dict:
    """Re-rule pins refused ONLY for the legacy text-hash convention (M-7).

    ``mark_legacy_hash_convention`` says "the notes they covered surface as
    unexplained until re-ruled" — this is that missing re-ruling step. Nothing
    else in the engine ever WROTE the disposition file, so a vault the
    migration stamped had no way back: on the reference vault it turned 108
    standing owner rulings into 97 unexplained drift findings and a stale
    doctor row, with no command able to clear one.

    What makes re-ruling safe is the marker itself. A record carries
    ``needs_reruling_text_hash_convention`` only when its pin already matched
    on path, issue AND the observed hash — and after M-7 that observed hash is
    the note's RAW BYTES. That equality is the byte anchor the migration said
    did not exist: the ruled-on text hash equals today's byte hash, so today's
    file holds no carriage return at all, and a CR-ONLY edit after signing
    changes the byte hash and never reaches this function.

    The residual, stated rather than hidden: an edit that only REMOVES
    carriage returns after the ruling produces the same equality. So re-ruling
    is an OWNER act — nothing in the engine calls this, and ``apply`` defaults
    to False so a caller reports the list before it writes anything.

    Returns ``{"paths": [...], "applied": bool, "written": int}``.
    """
    queued = [
        r for r in records
        if r.get("disposition_reason") == "needs_reruling_text_hash_convention"
    ]
    paths = [str(r.get("path")) for r in queued]
    out: dict = {"paths": paths, "applied": False, "written": 0}
    if not queued or not apply:
        return out
    try:
        path = drift_dispositions_path(vault)
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — HostPathUnsafe/OSError/ValueError: fail closed
        return out
    stored = raw.get("dispositions") if isinstance(raw, dict) else raw
    if not isinstance(stored, list):
        return out
    today = date.today().isoformat()

    def _ident(r: dict) -> tuple:
        # The SAME triple `_candidate_disposition` matches on. Keying by path
        # alone would re-rule a shadowed duplicate pin that never reached the
        # queue — measured on the reference vault: 95 queued paths, 96 records.
        key = "expected_sha256" if r.get("issue") == "missing" else "actual_sha256"
        return (str(r.get("path")), r.get("issue"), r.get(key))

    wanted = {_ident(r) for r in queued}
    written = 0
    reruled = []
    for rec in stored:
        if (isinstance(rec, dict) and _ident(rec) in wanted
                and rec.get("convention") == LEGACY_TEXT_CONVENTION):
            # The hashes are NOT re-keyed: they already equal the observed byte
            # hash, which is the only reason this pin became a candidate.
            rec = {k: v for k, v in rec.items() if k != "convention"}
            rec["recorded"] = today
            rec["reruled_from"] = LEGACY_TEXT_CONVENTION
            written += 1
        reruled.append(rec)
    outraw = dict(raw) if isinstance(raw, dict) else {}
    outraw["dispositions"] = reruled
    try:
        _write_dispositions(path, outraw)
    except OSError:
        return out
    out["applied"] = True
    out["written"] = written
    return out