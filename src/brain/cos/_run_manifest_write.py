"""COS run-manifest operation."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._facade import public
from ._io import _reserve_exclusive, _write_atomic
from ._layout import _ts, _utcnow
from ._layout_setup import ensure_layout
from ._run_migration import run_manifest
from ._runs import _checked_run_id, current_run_path, next_run_id, run_manifest_path

def _manifest_inputs(vault, *, run_id: str | None, lane: str | None,
                     skill_path: Path | str | None, attended: bool,
                     now: _dt.datetime) -> tuple[bool, str, dict[str, Any]]:
    """Resolve immutable run-manifest inputs."""
    from .. import cos_deploy

    allocated = not run_id
    rid = _checked_run_id(run_id or public("next_run_id")(vault, now))
    skill = (cos_deploy.read_skill(skill_path) if skill_path
             else cos_deploy.deployed_skill(lane=lane))
    if not skill.get("bundle_version"):
        raise ValueError(
            f"{skill['path']} states no `kernel_version` — refusing to write a "
            "run manifest that would stamp every candidate with nothing")
    from .. import cos_echecks                                # noqa: PLC0415
    _capability_digest = cos_echecks.capability_digest
    _git = cos_echecks.git_state()
    if attended and _git["clean"] is not True:
        raise ValueError(
            "refusing to begin an ATTENDED run from a "
            + ("dirty" if _git["clean"] is False else "non-git")
            + " working tree: the manifest would record commit "
            f"{_git['commit']} while the code that actually runs is something "
            "else, and assertion (6) of the attended validation — 'the "
            "capability set is unchanged AT THE COMMIT THAT RAN' — would be "
            "unprovable. Commit or stash first.")
    return allocated, rid, {
        "schema": RUN_MANIFEST_SCHEMA,
        "run_id": rid,
        "lane": skill.get("lane") or lane,
        "lane_reason": skill.get("lane_reason") or "operator-asserted skill path",
        "skill_path": skill["path"],
        "skill_sha256": skill["sha256"],
        "bundle_version": skill["bundle_version"],
        "extraction_rules_version": skill["extraction_rules_version"],
        "expected_echecks": skill.get("echecks"),
        "capability_digest": _capability_digest(),
        "git_commit": _git["commit"],
        "git_clean": _git["clean"],
        "attended": bool(attended),
        "expected_artifacts": [
            f"_cos_nightly_{rid}.md",
            f"_cos_ingestion_ledger_{rid}.jsonl",
            f"cos_contract_pre_{rid}.json",
            "_cos_metrics.jsonl",
        ],
    }


def _same_manifest(doc: dict[str, Any], manifest: dict[str, Any]) -> bool:
    """Compare immutable manifest fields."""
    return {key: value for key, value in doc.items() if key != "written"} == manifest


def _existing_manifest(vault, rid: str, *, allocated: bool,
                       manifest: dict[str, Any]) -> dict[str, Any] | None:
    """Return an idempotent manifest or refuse reuse."""
    existing = run_manifest(vault, rid)
    if existing is None:
        return None
    if allocated:
        raise ValueError(
            f"refusing to begin {rid}: this run ALLOCATED that id (no "
            "--run-id was given) and a manifest for it already exists, so "
            "another run took it first. Two runs under one id share one "
            "evidence directory and overwrite each other's enumeration, "
            "judgment, plan and rehearsal. Begin again — the allocator "
            "will hand out the next number.")
    if not _same_manifest(existing, manifest):
        raise ValueError(
            f"a run manifest for {rid} already exists and differs — a run "
            "manifest is IMMUTABLE (it is the record of what produced that "
            "run's candidates). Start a new run id instead.")
    return existing


def _reserve_manifest(vault, path: Path, rid: str, *, allocated: bool,
                      manifest: dict[str, Any]) -> dict[str, Any] | None:
    """Reserve a run-manifest path."""
    try:
        _reserve_exclusive(path)
    except FileExistsError as exc:
        if allocated:
            raise ValueError(
                f"refusing to begin {rid}: another run reserved that id "
                "between this one allocating it and writing its manifest. "
                "Begin again — the allocator will hand out the next number.") from exc
        reread = run_manifest(vault, rid)
        if reread is None:
            raise ValueError(
                f"refusing to begin {rid}: its name is reserved but no manifest "
                "is published under it yet. Either another writer is mid-begin "
                "— retry in a moment, after the manifest is published, and do "
                "NOT wait on the reservation — or a prior begin CRASHED between "
                "reserving the id and writing the manifest, which BURNS the id "
                "permanently: no retry can ever clear it, so begin under a "
                f"different --run-id (the reservation at {rid} is a zero-byte "
                "placeholder a human may remove once no writer holds it)."
            ) from exc
        if not _same_manifest(reread, manifest):
            raise ValueError(
                f"a run manifest for {rid} already exists and differs — a run "
                "manifest is IMMUTABLE (it is the record of what produced that "
                "run's candidates). Start a new run id instead.")
        return reread
    return None


def _write_current_run(vault, rid: str, manifest: dict[str, Any], now: _dt.datetime) -> None:
    """Publish VM-readable run instructions."""
    current = current_run_path(vault)
    current.parent.mkdir(parents=True, exist_ok=True)
    public("_write_atomic")(current, (json.dumps(
        {"run_id": rid, "started": _ts(now), "lane": manifest["lane"],
         "skill_path": manifest["skill_path"], "skill_sha256": manifest["skill_sha256"],
         "expected_artifacts": manifest["expected_artifacts"]},
        sort_keys=True) + "\n").encode("utf-8"), mode=MODE_VM_READABLE)


def write_run_manifest(vault, *, run_id: str | None = None,
                       lane: str | None = None,
                       skill_path: Path | str | None = None,
                       attended: bool = False,
                       now: _dt.datetime | None = None) -> dict[str, Any]:
    """Freeze one launch bundle into an immutable run manifest."""
    now = now or _utcnow()
    ensure_layout(vault)
    allocated, rid, manifest = _manifest_inputs(
        vault, run_id=run_id, lane=lane, skill_path=skill_path, attended=attended, now=now)
    if (existing := _existing_manifest(vault, rid, allocated=allocated, manifest=manifest)) is not None:
        return existing
    p = run_manifest_path(vault, rid)
    p.parent.mkdir(parents=True, exist_ok=True)
    if (existing := _reserve_manifest(vault, p, rid, allocated=allocated, manifest=manifest)) is not None:
        return existing
    record = {**manifest, "written": _ts(now)}
    public("_write_atomic")(p, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
    try:
        os.chmod(p, 0o400)  # nosemgrep: insecure-file-permissions -- read-only by design
    except OSError:
        pass
    _write_current_run(vault, rid, manifest, now)
    return record

__all__ = ['write_run_manifest']
