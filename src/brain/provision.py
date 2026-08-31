"""VM-request → host-drain vault provisioning (PRV-10, 2026-08-17).

A Cowork session can scaffold a vault as plain files, but three steps need
the host: the audit signing key, the per-vault nightly task, and the
workspace registry. Until PRV-10 nothing on the host ever looked for a vault
a Cowork session created, so the work died in the sandbox.

The lane mirrors VM-draft → host-commit capture:

- **VM side** (``brain provision-request``, VM_ALLOWED): write ONE marker,
  ``<vault>/.brain/provision-request.json``. No key, no signing, no launchd —
  a request, exactly like ``draft-capture`` is for notes.
- **Host side** (``brain provision-drain``, host-broker; also a fold on the
  hourly ``brain maintain`` daily branch): scan the PARENT directories of
  already-registered workspaces for pending markers and complete each one —
  ``brain init --full --apply`` (key check + nightly registration + seed),
  ``brain sync --publish``, model staging, registry upsert.

Security rules (owner ruling 2026-08-16: automatic, no approval gate):

- The vault path is derived from WHERE the marker sits, never from fields
  inside it — a forged marker cannot point the drain at an arbitrary path.
- Only direct children of registered-workspace parents are scanned, and a
  symlinked workspace that resolves elsewhere is skipped, reported.
- An already-registered vault is never re-provisioned (marker consumed,
  reported as ``already-registered``).
- Every provision is reported through the maintain results → health report,
  never silent.

Scope limit, stated plainly: the FIRST vault on a machine still needs one
host ``/brainiac-install`` — with an empty registry there are no roots to
scan and no nightly to ride. This lane covers every vault after that.

Filesystem + subprocess only (same contract as ``init.py``): no BrainCore,
no embedder import. Concurrency between two draining nightlies is settled by
an atomic claim-rename on the marker — the loser gets FileNotFoundError and
skips.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import workspaces
# Wire 5 moved to `provision_mcp` (its own module: the Desktop config is a
# third-party file with a third party still writing to it). Re-exported here
# because this module is the one every caller already imports.
from .pathkey import real_key
from .provision_mcp import mcp_server_name, register_mcp  # noqa: F401

REQUEST_NAME = "provision-request.json"
CLAIM_NAME = REQUEST_NAME + ".claimed"
RESULT_NAME = "provision-result.json"

_INIT_TIMEOUT_S = 900
_SYNC_TIMEOUT_S = 1800
_STAGE_TIMEOUT_S = 3600


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def request_path(vault: str | os.PathLike[str]) -> Path:
    return Path(vault) / ".brain" / REQUEST_NAME


def write_request(vault: str | os.PathLike[str], *, role: str) -> dict[str, Any]:
    """Stage a provisioning request. Idempotent; the marker is DATA only —
    the drain derives every path from the marker's location, never its body."""
    vault_p = Path(vault)
    marker = request_path(vault_p)
    result = vault_p / ".brain" / RESULT_NAME
    if marker.exists():
        return {"action": "provision-request", "status": "already-pending",
                "request": str(marker)}
    if result.exists():
        try:
            prior = json.loads(result.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prior = {}
        if prior.get("ok"):
            return {"action": "provision-request", "status": "already-provisioned",
                    "result": str(result)}
    marker.parent.mkdir(parents=True, exist_ok=True)
    # Minimal vault shape so the workspace reads as a vault-in-waiting; the
    # host's `init --full --apply` does the real scaffolding + seeding.
    for zone in ("brain", "raw", "inbox"):
        (vault_p / zone).mkdir(exist_ok=True)
    payload = {"version": 1, "requested_at": _now_iso(), "role": role}
    tmp = marker.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, marker)
    return {"action": "provision-request", "status": "staged",
            "request": str(marker),
            "note": "the host completes provisioning on its next hourly "
                    "maintenance run; the outcome lands beside this marker "
                    f"as {RESULT_NAME}"}


def _registered(entries: list[dict]) -> tuple[set[Path], set[Path]]:
    """(scan roots, registered vault realpaths) from the registry."""
    roots: set[Path] = set()
    vaults: set[Path] = set()
    for e in entries:
        ws = e.get("workspace_path") or ""
        vp = e.get("vault_path") or ""
        if ws:
            roots.add(Path(real_key(ws)).parent)
        if vp:
            # THE key (realpath + NFC): macOS `readdir` hands the drain an NFD
            # spelling of a path the registry stores in NFC, and a realpath-only
            # comparison calls those two different vaults.
            vaults.add(Path(real_key(vp)))
    return roots, vaults


def _run_engine(vault: Path, argv: list[str], *, timeout: int,
                runner: Callable[..., Any],
                env_extra: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """Run `python -m brain --vault <vault> <argv...>` with BRAIN_VAULT set
    (the task registrar reads the vault from the ENVIRONMENT, not --vault).

    ``env_extra`` reaches the whole chain: ``init --full --apply`` ->
    ``scripts/register_tasks.py`` -> ``scripts/install-brief-mac.sh``, each of
    which passes ``os.environ`` straight down. That is how the merged
    ``BRAIN_WORKSPACE_SWEEP_DIRS`` gets INTO the rendered plist instead of being
    overwritten by the next re-render (install-brief-mac.sh:91).

    PYTHONPATH pins the child to THIS ``brain``: ``-m brain`` resolves on the
    CHILD's path, so a second checkout ran the INSTALLED engine (2026-08-31).
    """
    cmd = [sys.executable, "-m", "brain", "--vault", str(vault), *argv]
    here = str(Path(__file__).resolve().parent.parent)
    env = {**os.environ, "BRAIN_VAULT": str(vault), **(env_extra or {}),
           "PYTHONPATH": (here + os.pathsep
                          + os.environ.get("PYTHONPATH", "")).rstrip(os.pathsep)}
    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except Exception as exc:  # spawn failure / timeout
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    out: dict[str, Any] = {"ok": proc.returncode == 0, "exit": proc.returncode}
    if proc.returncode != 0:
        out["stderr"] = (proc.stderr or "").strip()[-500:]
    else:
        try:
            out["report"] = json.loads(proc.stdout)
        except (json.JSONDecodeError, TypeError):
            pass
    return out


def _find_model_source(vault: Path, entries: list[dict]) -> Path | None:
    """An already-staged local bge-m3-int8 snapshot to seed the new vault from,
    so its Cowork leg never needs the (proxy-blocked) network download. Prefers
    a model already inside this vault, then any registered vault's."""
    own = vault / ".brain" / "model"
    if own.is_dir() and any(own.iterdir()):
        return own
    for e in entries:
        for cand in (Path(e["model_dir"]) if e.get("model_dir") else None,
                     Path(e["vault_path"]) / ".brain" / "model"
                     if e.get("vault_path") else None):
            if cand is not None and cand.is_dir() and any(cand.iterdir()):
                return cand
    return None


def _stage_model(vault: Path, src: Path) -> dict[str, Any]:
    """Copy a model snapshot into the vault (the fallback when the full Cowork
    staging cannot run). Best-effort: a vault without a model still provisions."""
    dst = vault / ".brain" / "model"
    if dst.is_dir() and any(dst.iterdir()):
        return {"status": "already-present", "path": str(dst)}
    try:
        shutil.copytree(src, dst, dirs_exist_ok=True)
        return {"status": "copied", "from": str(src), "path": str(dst)}
    except OSError as exc:
        return {"status": "copy-failed", "from": str(src), "error": str(exc)}


def _stage_cowork_runtime(vault: Path, model_src: Path | None, workspace: Path, *,
                          runner: Callable[..., Any],
                          snapshot_dir: Path | None = None) -> dict[str, Any]:
    """Assemble the VM-readable runtime (engine, per-arch ELFs, model, skills,
    routines, published snapshot) into the workspace.

    This is what makes the vault usable FROM Cowork at all, and it is why the
    drain must not write a ``cowork-vm`` registry entry on its own say-so: the
    installer is a shell script needing a checkout AND built Linux ELFs, so a
    pure PyPI install cannot run it. When it cannot run, we say so and record
    the vault host-only — a registry entry claiming a Cowork-ready workspace
    that has no engine in it is exactly the defect this function exists to
    stop (measured 2026-08-17 on a newly provisioned vault: a Cowork session opened
    the folder and found `brain: command not found`).

    ``workspace`` is the ATTACHED FOLDER and is passed to the installer as its
    fourth argument. It is separate from ``vault`` because after the s07 cutover
    they are separate places, and passing only the vault was a real hole, found
    by peer review 2026-08-29: the installer puts its whole leak refusal behind
    ``if [ -n "$4" ]``, so a three-argument call skipped the refusal AND took the
    branch that assumes ``$VAULT/..`` is the attached folder. On a relocated
    vault that stages the engine to ``<vault>/.brain`` --- off the mount, where
    no Cowork sandbox can see it --- and then the stamp check below, which used
    to look at that same wrong-side path, certified it as ``staged`` and let the
    drain write a ``cowork-vm`` registry entry. That is verbatim the defect the
    paragraph above says this function exists to stop.
    """
    brain_dir = _staging_root(vault, workspace)
    if model_src is None:
        return {"status": "skipped", "reason": "no local model snapshot found"}
    from . import update as brain_update

    checkout = brain_update.resolve_engine_source()
    if checkout is None:
        return {"status": "skipped", "reason": "no engine checkout resolved"}
    script = checkout / "tools" / "cowork_workspace_install.sh"
    if not script.is_file():
        return {"status": "skipped", "reason": f"{script} not present"}
    dist = checkout / "dist"
    elfs = sorted(p.name for p in dist.glob("brain-linux-*") if p.is_file())
    if not elfs:
        return {"status": "skipped",
                "reason": f"no Linux ELFs built in {dist} "
                          "(host: tools/build_brain_binary_linux.sh)"}
    # The installer needs to know where the published snapshot goes: it refuses
    # any destination inside the attached folder, and until 2026-08-30 nothing
    # in this lane passed a value at all, so the guard ran against the shell's
    # ambient environment rather than the vault's own default.
    from . import config as _config

    env = {**os.environ,
           "BRAIN_SNAPSHOT_DIR": str(snapshot_dir or _config.snapshot_dir(vault))}
    try:
        proc = runner([str(script), str(vault), str(model_src), str(dist),
                       str(workspace)],
                      capture_output=True, text=True, timeout=_STAGE_TIMEOUT_S,
                      cwd=str(checkout), env=env)
    except Exception as exc:
        return {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
    if proc.returncode != 0:
        return {"status": "failed", "exit": proc.returncode,
                "stderr": (proc.stderr or "").strip()[-500:]}
    # Verify the claim by looking for what the staging must have produced,
    # never by trusting the exit code alone: a `set -e` script can report 0
    # from a wrapper while an inner phase died, and the whole point of this
    # lane is that nothing downstream claims a capability it does not have.
    # Verify the STAGING ROOT the installer actually wrote to, not `vault/.brain`
    # --- on a relocated vault those are different directories, and checking the
    # wrong one is a stamp that cannot fail.
    stamp = brain_dir / "engine" / "brain" / "_version.py"
    if not stamp.is_file():
        return {"status": "failed", "exit": 0,
                "reason": f"installer reported success but {stamp} is absent"}
    return {"status": "staged", "checkout": str(checkout), "elfs": elfs}


def _staging_root(vault: Path, workspace: Path) -> Path:
    """The relocation-aware ``.brain`` --- ``vault/.brain`` co-located, on the
    mount once the vault moves. One resolver, shared with the installer."""
    from .cowork_staging import staging_root

    return staging_root(vault, workspace)


def _wire_summary(wires: dict[str, Any], *, sync_ok: bool) -> dict[str, Any]:
    """Six wire verdicts -> the report shape the drain's consumers speak.

    Split out of :func:`_provision_one` only to keep it under the function-length
    ratchet, and it is a coherent unit on its own: everything here is a pure
    function of the wire statuses, with no vault, workspace or runner in reach.

    Two rules live in it:

    * **A failed wire may NEVER hide inside a `provisioned` summary.** Wires 3
      and 5 are what make the vault findable at all, and this report is
      CONSUMED: the claim marker is deleted on it, ``write_request`` reads
      ``ok: true`` as already-provisioned, and ``maintain`` files an auto-fix.
      So the wire statuses decide the status, not the sync exit code alone.
    * **Only claim a Cowork workspace the staging actually built** — wire 4's
      own condition, read back here. An entry asserting a Cowork-ready
      workspace with no engine inside it sends a Cowork session to a
      ``brain: command not found`` dead end.
    """
    # The MCP registration is what makes the vault reachable from the Desktop
    # Chat tab and Cowork's MCP-on-host path — a vault that indexes perfectly
    # and has no server entry is invisible to both.
    out: dict[str, Any] = {
        "mcp": wires["5"].get("mcp", {"status": "failed",
                                      "error": wires["5"].get("detail", "")})}
    failed = [k for k in ("3", "5") if wires[k]["status"] == "failed"]
    if wires["3"]["status"] == "failed":
        out["registry"] = f"NOT recorded — wire 3 failed: {wires['3'].get('detail', '')}"
    else:
        targets = ("host + cowork-vm" if wires["4"]["status"] in ("already", "done")
                   else "host")
        out["registry"] = f"recorded ({targets})"
    out["status"] = "provisioned" if not failed else "wires-failed"
    out["ok"] = sync_ok and not failed
    if failed:
        out["failed_wires"] = failed
    return out


def _provision_one(vault: Path, workspace: Path, *, entries: list[dict],
                   registered_vaults: set[Path],
                   registry_path: Path, lock_path: Path,
                   runner: Callable[..., Any]) -> dict[str, Any]:
    """The DRAIN's wrapper around the six wires.

    Everything policy lives here and nowhere else: the ``already-registered``
    short-circuit (PRV-10: a registered vault is never re-provisioned, so the
    drain is not a repair path — ``brain provision-local`` is), the model-source
    lookup over the registry, the model-copy + ``sync --publish`` FALLBACK for
    when the Cowork staging could not run, and the report shape the maintain
    fold and ``provision-result.json`` already speak.
    """
    from .provision_wire import _OK, sweep_status, wire_vault

    res: dict[str, Any] = {"vault": str(vault), "workspace": str(workspace)}
    if Path(real_key(vault)) in registered_vaults:
        # The short-circuit returns before any wire runs, so the sweep-gap
        # report below it could only ever fire on a vault's FIRST provisioning
        # — i.e. never on the one whose list was wiped later, which is the only
        # case that exists. `sweep_status` is the READ-ONLY form: it creates no
        # folder and writes no plist, so PRV-10 ("a registered vault is never
        # re-provisioned") holds and the finding still reaches `maintain`.
        res.update({"status": "already-registered", "ok": True,
                    "sweep": sweep_status(vault, workspace)})
        return res

    model_src = _find_model_source(vault, entries)
    # `sweep_repair=False`: this fold IS the launchd job whose plist the merge
    # would rewrite, and it could only ever ask a human to reload it. The drain
    # REPORTS the gap (`pending`); `brain provision-local` is the repair path.
    # First provisioning still gets the sweep dir for free, because wire 1's
    # `init --full --apply` renders the plist from the merged list.
    wires = wire_vault(vault, workspace, model_dir=str(model_src) if model_src else None,
                       registry_path=registry_path, lock_path=lock_path,
                       sweep_repair=False, runner=runner)
    res["init"] = wires["1"].get("init", {})
    res["wires"] = {k: {"status": v["status"], "detail": v.get("detail", "")}
                    for k, v in wires.items()}
    if wires["1"]["status"] == "failed":
        res.update({"status": "init-failed", "ok": False})
        return res

    # The full Cowork staging also lands the model, reconciles the index and
    # publishes the snapshot, so the plain model copy + sync are the FALLBACK
    # for when it cannot run — never both.
    cowork = wires["2"].get("cowork") or {"status": "failed",
                                          "reason": wires["2"].get("detail", "")}
    # WIRE 2's verdict decides, never the stager's own word inside it. The
    # stager reports `staged` off the engine stamp alone, and the stamp is
    # copied hundreds of lines before the model cache and the workspace
    # contract — so wire 2 re-checks the ARTEFACTS and fails a run that
    # produced a subset. If the raw word won here, the drain would skip the
    # model+sync FALLBACK for a runtime that is not there and report a Cowork
    # workspace it never built: the exact partial-staging hole wire 2 closes,
    # one level up. Same predicate wire 4 gates its `cowork-vm` row on, so the
    # two can never disagree about whether this vault staged.
    if wires["2"]["status"] not in _OK and cowork.get("status") == "staged":
        cowork = {**cowork, "status": "failed",
                  "reason": wires["2"].get("detail", "")}
    res["cowork"] = cowork
    if cowork["status"] != "staged":
        res["model"] = (_stage_model(vault, model_src) if model_src
                        else {"status": "no-local-source"})
        res["sync"] = _run_engine(vault, ["sync", "--publish", "--json"],
                                  timeout=_SYNC_TIMEOUT_S, runner=runner)
        sync_ok = bool(res["sync"].get("ok", False))
    else:
        sync_ok = True

    res.update(_wire_summary(wires, sync_ok=sync_ok))
    if cowork["status"] != "staged":
        res["next_step"] = (
            "Cowork runtime NOT staged "
            f"({cowork.get('reason') or cowork.get('stderr')}) — this vault "
            "works on the host; run /brainiac-cowork-setup on a host with a "
            "checkout to make it usable from Cowork")
    res["sweep"] = wires["6"]
    return res


def maintain_fold(
    results: dict[str, Any],
    auto_fixed: list[dict[str, Any]],
    action_required: list[dict[str, Any]],
) -> None:
    """PRV-10 daily fold: drain pending requests and shape the outcomes into
    the maintain report (never silent). The caller wraps this in its own
    try/except → ``blocked``. Item shapes come from ``maintenance`` so the
    report renders like every other fold."""
    from . import maintenance as maint

    res = drain()
    if not (res["handled"] or res["stuck_claims"]):
        return
    results["provision_drain"] = res
    for h in res["handled"]:
        if h.get("status") == "provisioned" and h.get("ok"):
            auto_fixed.append(maint.auto_fixed_item(
                "provision-drain", h.get("vault", ""),
                "provisioned a Cowork-requested vault "
                "(init --full --apply + sync --publish + registry)"))
        elif not h.get("ok"):
            action_required.append(maint.action_required_item(
                f"vault provisioning failed: {h.get('vault')}",
                f"status={h.get('status')} "
                f"failed_wires={h.get('failed_wires') or []}",
                "run `brain provision-drain` by hand and read its report",
                h.get("vault", "")))
        sweep = h.get("sweep") or {}
        if sweep.get("status") in ("pending", "failed"):
            # The drain deliberately does NOT edit the launchd plist of the job
            # running it, and could not reload it if it did. It reports; the
            # operator command repairs.
            action_required.append(maint.action_required_item(
                "the Cowork deliverables folder is not swept into this vault",
                sweep.get("detail", ""),
                f"run `brain provision-local {h.get('vault')} --workspace "
                f"<the attached folder>`", h.get("vault", "")))
    for s in res["stuck_claims"]:
        action_required.append(maint.action_required_item(
            "a provision claim marker is stuck (crashed drain?)", s,
            "inspect, then rename back to provision-request.json to retry", s))


def drain(*, registry_path: Optional[Path] = None,
          lock_path: Optional[Path] = None,
          runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """Scan registered-workspace parents for pending requests; complete each.

    Returns ``{"handled": [...], "stuck_claims": [...], "roots": [...]}`` —
    empty ``handled`` on the (overwhelmingly common) no-request run.
    """
    # Resolve from the ENVIRONMENT at call time, not the module-load-time
    # constants — test isolation (a monkeypatched $BRAINIAC_HOME/$HOME) must
    # bind, or a maintain test scans the developer's live registry.
    home = Path(os.environ.get("BRAINIAC_HOME", Path.home() / ".brainiac"))
    registry_path = registry_path or home / "workspaces.json"
    lock_path = lock_path or home / "workspaces.lock"
    entries = workspaces.list_entries(registry_path=registry_path)
    roots, registered_vaults = _registered(entries)

    handled: list[dict[str, Any]] = []
    stuck: list[str] = []
    for root in sorted(roots):
        if not root.is_dir():
            continue
        # A claim left behind by a crashed drain would strand its vault
        # silently forever — surface it instead.
        stuck.extend(str(p) for p in root.glob(f"*/vault/.brain/{CLAIM_NAME}"))
        for marker in sorted(root.glob(f"*/vault/.brain/{REQUEST_NAME}")):
            vault = marker.parent.parent
            workspace = vault.parent
            real_workspace = Path(os.path.realpath(workspace))
            real_vault = Path(os.path.realpath(vault))
            # The VM owns everything under `workspace` (PRV-10) and can make
            # `vault` itself a symlink to an arbitrary host directory while
            # still passing the workspace-parent check below. Require the
            # resolved vault to land exactly at <real workspace>/vault, or
            # the drain would init/sync/register/MCP-wire an attacker-chosen
            # path instead of the requested workspace's own vault.
            if real_workspace.parent != root or real_vault != real_workspace / "vault":
                handled.append({"vault": str(vault), "status": "skipped-symlink-escape",
                                "ok": False})
                continue
            claim = marker.with_name(CLAIM_NAME)
            try:
                os.rename(marker, claim)  # atomic claim; racing drain loses cleanly
            except FileNotFoundError:
                continue
            try:
                res = _provision_one(
                    vault, workspace, entries=entries,
                    registered_vaults=registered_vaults,
                    registry_path=registry_path, lock_path=lock_path,
                    runner=runner)
            except Exception as exc:  # noqa: BLE001 — report, never abort the fold
                res = {"vault": str(vault), "status": "error",
                       "ok": False, "error": f"{type(exc).__name__}: {exc}"}
            res["completed_at"] = _now_iso()
            result_file = vault / ".brain" / RESULT_NAME
            try:
                result_file.write_text(json.dumps(res, indent=2) + "\n",
                                       encoding="utf-8")
            except OSError:
                pass
            claim.unlink(missing_ok=True)
            handled.append(res)
    return {"action": "provision-drain", "handled": handled,
            "stuck_claims": stuck, "roots": [str(r) for r in roots]}
