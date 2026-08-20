"""Host-private audit drift-disposition triage (INT-02)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotation-only; a runtime import here would be a cycle
    from .audit_chain import AuditChain

DRIFT_DISPOSITIONS_FILENAME = "audit-drift-dispositions.json"


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
        dest.write_text(json.dumps(
            {"dispositions": records,
             "migrated_from_mount": legacy.as_posix()},
            indent=2), encoding="utf-8")
    except OSError:
        return None
    return (f"carried {len(records)} drift disposition(s) forward from the shared "
            f"mount to {dest} — they were recorded where a Cowork VM could write, "
            f"so re-check them if you have any reason to doubt that host")


def load_drift_dispositions(vault: Path) -> dict[str, dict]:
    """``{path: record}`` from the triage file; ``{}`` when absent, unreadable,
    or resolvable only to a VM-visible path. Fails CLOSED into "nothing is
    explained" — an unreadable or untrustworthy disposition file must never
    silently clear a drift count."""
    migrate_drift_dispositions(vault)
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
    }