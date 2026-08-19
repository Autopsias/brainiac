"""Host-private audit drift-disposition triage (INT-02)."""
from __future__ import annotations

import json
from pathlib import Path

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

    The carried-forward records came from a VM-writable path, so the copy is
    STAMPED (``migrated_from_mount``) rather than laundered into looking
    host-authored. Without this, a vault with triaged historical drift would
    report every previously-explained record as unexplained on first run after
    the upgrade — a false tamper alarm, which is the fastest way to teach an
    operator to ignore a real one."""
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


def match_disposition(record: dict, dispositions: dict[str, dict]) -> dict | None:
    """The disposition explaining THIS drift record, or ``None``.

    Matching requires the same path, the same issue, and the same observed
    hash the disposition was recorded against — so a further edit re-surfaces
    as unexplained instead of hiding under an old ruling."""
    d = dispositions.get(str(record.get("path")))
    if not isinstance(d, dict) or not d.get("disposition"):
        return None
    if d.get("issue") != record.get("issue"):
        return None
    key = "expected_sha256" if record.get("issue") == "missing" else "actual_sha256"
    if d.get(key) != record.get(key):
        return None
    return d


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