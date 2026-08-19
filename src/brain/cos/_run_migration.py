"""COS run-migration operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._facade import public
from ._io import _write_atomic
from ._layout import _ts
from ._learning_ledger import log_defect
from ._runs import legacy_runs_dir, run_manifest_path, runs_dir

def _migration_destination(vault) -> tuple[Path, Path] | None:
    """Resolve host run-record migration paths."""
    try:
        return legacy_runs_dir(vault), runs_dir(vault)
    except Exception:  # noqa: BLE001 — unsafe destination: stay fail-closed
        return None


def _legacy_run_names(legacy: Path) -> list[str]:
    """List legacy run-record names."""
    try:
        return sorted(path.name for path in legacy.iterdir() if path.is_file())
    except OSError:
        return []


def _prepare_destination(dest_dir: Path) -> None:
    """Create the host-private migration destination."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(dest_dir, 0o700)  # nosemgrep: insecure-file-permissions -- host-private run-record store
    except OSError:
        pass


def _migrate_run_file(vault, legacy: Path, dest_dir: Path, name: str) -> str:
    """Carry one legacy run record."""
    source, destination = legacy / name, dest_dir / name
    try:
        data = source.read_bytes()
    except OSError:
        return "conflict"
    if destination.exists():
        try:
            same = destination.read_bytes() == data
        except OSError:
            same = False
        if not same:
            log_defect(
                vault, "run-record-mount-conflict",
                f"{name} exists both in the host-private run store and in the legacy on-mount "
                f"{legacy} with DIFFERENT bytes. Neither copy is preferred: the on-mount one is "
                "left in place as evidence and its run scores INCONCLUSIVE until a human decides which is real.")
            return "conflict"
        status = "already"
    else:
        try:
            public("_write_atomic")(destination, data, mode=0o600)
        except OSError:
            return "conflict"
        status = "carried"
    try:
        source.unlink()
    except OSError:
        pass
    return status


def _write_migration_marker(marker: Path, legacy: Path, carried: list[str]) -> None:
    """Record a completed run-record migration."""
    try:
        public("_write_atomic")(marker, json.dumps({
            "carried_from": str(legacy), "files": len(carried), "at": _ts()},
            indent=2).encode("utf-8"), mode=0o600)
    except OSError:
        pass


def migrate_run_records(vault=None) -> dict[str, Any]:
    """Carry the pre-2026-08-16 on-mount run records forward, ONCE.

    Returns ``{"carried": [...], "conflicts": [...], "already": bool}``.
    Idempotent and cheap: after the marker exists it scans the legacy directory
    and returns without reading a byte of it.

    MOVE, not copy (unlike ``audit.migrate_drift_dispositions``, which carries
    one small file the operator may still want to read): these 100+ records are
    now read ONLY from the destination, and leaving a second set of manifests
    and verdicts lying in a VM-writable directory is the confusion this change
    exists to remove. Written to the destination and fsynced BEFORE the source
    is unlinked, so a crash mid-migration strands nothing.

    SAME RUN ID IN BOTH PLACES — FAIL CLOSED. Identical bytes are the ordinary
    resumed migration and the legacy copy is simply dropped. DIFFERING bytes
    are never resolved by preferring either side: a manifest or verdict that
    disagrees with its host-private counterpart is the tampering signal this
    directory was moved to make impossible, so the legacy file is left exactly
    where it is, a defect is logged, and :func:`run_record_intruders` keeps
    reporting it — which is what takes the affected run to INCONCLUSIVE in
    ``cos_runverify.verify_run``. It is deliberately scoped to the RUN, not to
    the whole store: one planted file must not be able to stop every other
    night being verified.

    THE ONE WINDOW, STATED. The migration is gated on the marker, and the
    marker is stamped the first time this runs on a host whose host-private
    store exists. A host that has NEVER written a run record and whose legacy
    directory is created from the mount would import that plant once. It is
    bounded (a deployment with no run history has no candidates a forged
    verdict could claim) and it closes the moment the host writes its first
    record, which every `cos-run-begin` does before anything is judged."""
    paths = _migration_destination(vault)
    if paths is None:
        return {"carried": [], "conflicts": [], "already": False}
    legacy, dest_dir = paths
    marker = dest_dir / RUNS_MIGRATION_MARKER
    if marker.exists():
        return {"carried": [], "conflicts": [], "already": True}
    names = _legacy_run_names(legacy)
    # NOTHING ON EITHER SIDE — leave no trace. A vault that never ran the old
    # layout has no legacy directory (nothing creates one any more), and a name
    # resolution that mkdirs an app-data directory per throwaway vault is the
    # side effect `config.host_lock_dir` refuses for the same reason. The
    # marker gets stamped by the next call after the store exists, which is the
    # first thing any write path creates.
    if not names and not dest_dir.is_dir():
        return {"carried": [], "conflicts": [], "already": False}
    _prepare_destination(dest_dir)
    carried: list[str] = []
    conflicts: list[str] = []
    for name in names:
        outcome = _migrate_run_file(vault, legacy, dest_dir, name)
        if outcome == "carried":
            carried.append(name)
        elif outcome == "conflict":
            conflicts.append(name)
    if not conflicts:
        _write_migration_marker(marker, legacy, carried)
    return {"carried": carried, "conflicts": conflicts, "already": False}

def run_record_intruders(vault=None, run_id: str | None = None) -> list[str]:
    """Files still sitting in the legacy on-mount run directory.

    After :func:`migrate_run_records` has run, this is empty on a healthy host.
    Anything it names is either a refused conflict or a file written into a
    VM-writable directory that the run validator once trusted — never an input,
    always a reason to refuse a verdict for the run it names."""
    try:
        names = [p.name for p in legacy_runs_dir(vault).iterdir() if p.is_file()]
    except OSError:
        return []
    if run_id is None:
        return sorted(names)
    rid = str(run_id)
    return sorted(n for n in names if rid in n)

def run_manifest(vault, run_id: Any) -> dict[str, Any] | None:
    """The frozen manifest for ``run_id``, or ``None`` if the host wrote none.

    A READ entry point, so it carries the one-time mount carry-forward too
    (same shape as ``audit.load_drift_dispositions``): verification re-executes
    over historical runs on hosts that may not have written a manifest since
    the relocation, and a migration that only ran on the write path would make
    every one of those nights read as "the host recorded nothing"."""
    migrate_run_records(vault)
    try:
        p = run_manifest_path(vault, run_id)
    except ValueError:
        return None
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return m if isinstance(m, dict) and m.get("run_id") == str(run_id) else None

__all__ = ['migrate_run_records', 'run_record_intruders', 'run_manifest']
