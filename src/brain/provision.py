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
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import workspaces

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
            roots.add(Path(os.path.realpath(ws)).parent)
        if vp:
            vaults.add(Path(os.path.realpath(vp)))
    return roots, vaults


def _run_engine(vault: Path, argv: list[str], *, timeout: int,
                runner: Callable[..., Any]) -> dict[str, Any]:
    """Run `python -m brain --vault <vault> <argv...>` with BRAIN_VAULT set
    (the task registrar reads the vault from the ENVIRONMENT, not --vault)."""
    cmd = [sys.executable, "-m", "brain", "--vault", str(vault), *argv]
    env = {**os.environ, "BRAIN_VAULT": str(vault)}
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


def _stage_cowork_runtime(vault: Path, model_src: Path | None, *,
                          runner: Callable[..., Any]) -> dict[str, Any]:
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
    """
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
    try:
        proc = runner([str(script), str(vault), str(model_src), str(dist)],
                      capture_output=True, text=True, timeout=_STAGE_TIMEOUT_S,
                      cwd=str(checkout))
    except Exception as exc:
        return {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
    if proc.returncode != 0:
        return {"status": "failed", "exit": proc.returncode,
                "stderr": (proc.stderr or "").strip()[-500:]}
    # Verify the claim by looking for what the staging must have produced,
    # never by trusting the exit code alone: a `set -e` script can report 0
    # from a wrapper while an inner phase died, and the whole point of this
    # lane is that nothing downstream claims a capability it does not have.
    stamp = vault / ".brain" / "engine" / "brain" / "_version.py"
    if not stamp.is_file():
        return {"status": "failed", "exit": 0,
                "reason": f"installer reported success but {stamp} is absent"}
    return {"status": "staged", "checkout": str(checkout), "elfs": elfs}


def mcp_server_name(workspace: Path) -> str:
    """A per-vault MCP server name derived from the workspace folder.

    NOT the bare default `brainiac`: that name is a single global slot, so a
    second vault registering under it would silently repoint Claude Desktop
    away from the first. One vault, one server, one name.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", workspace.name.lower()).strip("-")
    return f"brainiac-{slug}" if slug else "brainiac"


def register_mcp(vault: Path, workspace: Path, *,
                 max_tier: str = "Internal") -> dict[str, Any]:
    """Register this vault as its own Claude Desktop MCP server.

    Without this a newly provisioned vault is unreachable from the Desktop
    Chat tab and from Cowork's MCP-on-host retrieval path — the vault
    installs, stages and indexes correctly and still cannot be queried, which
    is exactly what a new vault looked like before 2026-08-17.

    Idempotent and additive: `plan_claude_desktop` MERGES into the existing
    `mcpServers` map and reports `noop` when the entry already matches, so
    re-running never disturbs another vault's server or any unrelated one.
    """
    from . import connect as _connect

    try:
        cfg = _connect.claude_desktop_config_path()
        name = mcp_server_name(workspace)
        plan = _connect.plan_claude_desktop(cfg, str(vault), name, max_tier)
        if plan.already_connected:
            return {"status": "already-registered", "name": name, "config": str(cfg)}
        _connect.apply_json_merge(plan)
        return {"status": "registered", "name": name, "config": str(cfg),
                "max_tier": max_tier,
                "note": "restart Claude Desktop for the new server to appear"}
    except Exception as exc:  # noqa: BLE001 — never fail a provision over this
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}


def _provision_one(vault: Path, workspace: Path, *, entries: list[dict],
                   registered_vaults: set[Path],
                   registry_path: Path, lock_path: Path,
                   runner: Callable[..., Any]) -> dict[str, Any]:
    res: dict[str, Any] = {"vault": str(vault), "workspace": str(workspace)}
    if Path(os.path.realpath(vault)) in registered_vaults:
        res.update({"status": "already-registered", "ok": True})
        return res

    init_res = _run_engine(
        vault, ["init", "--full", "--apply", "--json"],
        timeout=_INIT_TIMEOUT_S, runner=runner)
    res["init"] = init_res
    if not init_res.get("ok"):
        res.update({"status": "init-failed", "ok": False})
        return res

    # The full Cowork staging also lands the model, reconciles the index and
    # publishes the snapshot, so the plain model copy + sync are the FALLBACK
    # for when it cannot run — never both.
    model_src = _find_model_source(vault, entries)
    cowork = _stage_cowork_runtime(vault, model_src, runner=runner)
    res["cowork"] = cowork
    if cowork["status"] != "staged":
        res["model"] = (_stage_model(vault, model_src) if model_src
                        else {"status": "no-local-source"})
        res["sync"] = _run_engine(vault, ["sync", "--publish", "--json"],
                                  timeout=_SYNC_TIMEOUT_S, runner=runner)
        sync_ok = bool(res["sync"].get("ok", False))
    else:
        sync_ok = True

    workspaces.upsert_entry(
        str(vault), workspace_path=str(workspace), target="host",
        registry_path=registry_path, lock_path=lock_path)
    targets = "host"
    # ONLY claim a Cowork workspace the staging actually built. An entry
    # asserting a Cowork-ready workspace with no engine inside it sends a
    # Cowork session to a `brain: command not found` dead end.
    if cowork["status"] == "staged":
        workspaces.upsert_entry(
            str(vault), workspace_path=str(workspace), target="cowork-vm",
            model_dir=str(vault / ".brain" / "model"),
            registry_path=registry_path, lock_path=lock_path)
        targets = "host + cowork-vm"
    # The MCP registration is what makes the vault reachable from the Desktop
    # Chat tab and Cowork's MCP-on-host path. A vault that indexes perfectly
    # and has no server entry is invisible to both.
    res["mcp"] = register_mcp(vault, workspace)
    res["registry"] = f"recorded ({targets})"
    res.update({"status": "provisioned", "ok": sync_ok})
    if cowork["status"] != "staged":
        res["next_step"] = (
            "Cowork runtime NOT staged "
            f"({cowork.get('reason') or cowork.get('stderr')}) — this vault "
            "works on the host; run /brainiac-cowork-setup on a host with a "
            "checkout to make it usable from Cowork")
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
                f"status={h.get('status')}",
                "run `brain provision-drain` by hand and read its report",
                h.get("vault", "")))
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
            if Path(os.path.realpath(workspace)).parent != root:
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
