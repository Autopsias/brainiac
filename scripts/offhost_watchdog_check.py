#!/usr/bin/env python3
"""WD-01 — the off-host watchdog of last resort's LOCAL check-logic.

If launchd itself dies (or its plists get wiped), the brain-nightly umbrella
— and the synthesis-heartbeat check running INSIDE it (WATCHDOG-01, commit
d28c0ce) — die with it. This script is the OTHER scheduling substrate's
runnable check: for every HOST vault registered in
``~/.brainiac/workspaces.json``, it reads the local health-history +
synthesis-state files (never mutates anything) and fires ONE macOS
notification if any vault has gone quiet — silent otherwise.

LOCAL-first (owner decision 2026-07-12): the off-host CLOUD leg (a Claude
`/schedule` routine reading this remotely, weekly) is DEFERRED — verified
via the `schedule` skill's own documentation that a cloud routine "cannot
access local files, local services, or local environment variables"; there
is no remote-export transport yet to get this data there. This script is
what a human runs periodically today (cron/manual/`/brainiac-health`-
adjacent habit) and is also exactly what the SPEC'd `/schedule` routine's
prompt would invoke once a remote-read channel exists — see
docs/operations/wd01-offhost-watchdog-spec.md.

Stdlib-only, mirrors the brainiac-health skill's repo-fallback import
pattern (works whether `brain` is pip/uv/pipx-installed, or only present as
a repo checkout) so it never hard-crashes just because it can't see the
package the "normal" way.

Usage:
    python3 scripts/offhost_watchdog_check.py                # every registered host vault
    python3 scripts/offhost_watchdog_check.py <vault> [...]   # override/add specific vaults
    python3 scripts/offhost_watchdog_check.py --json          # machine-readable, no notification side effect noise
    python3 scripts/offhost_watchdog_check.py --no-notify      # compute findings, never call osascript (dry-run/evidence)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _find_repo_src(start: Path) -> Path | None:
    cur = start.resolve()
    for _ in range(8):
        if (cur / "pyproject.toml").is_file() and (cur / "src" / "brain").is_dir():
            return cur / "src"
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def _load_maintenance(repo_src: Path | None) -> Any | None:
    """Prefer a discoverable repo checkout (this script's own location is
    normally IN one); fall back to whatever `brain` is already importable
    from the ambient interpreter. Never raises — a caller degrades to
    "cannot check" rather than crashing."""
    if repo_src is not None:
        sys.path.insert(0, str(repo_src))
    try:
        import brain.maintenance as m  # noqa: PLC0415
    except ImportError:
        return None
    return m if hasattr(m, "offhost_watchdog_findings") else None


def _registered_host_vaults(repo_src: Path | None) -> list[str]:
    if repo_src is not None:
        tools_dir = repo_src.parent / "tools"
        if (tools_dir / "workspace_registry.py").is_file():
            sys.path.insert(0, str(tools_dir))
            try:
                import workspace_registry as _wr  # noqa: PLC0415

                return sorted({e["vault_path"] for e in _wr.list_entries(target="host")})
            except Exception:
                pass
    registry_path = Path.home() / ".brainiac" / "workspaces.json"
    try:
        data = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("entries", []) if isinstance(data, dict) else []
    return sorted({e["vault_path"] for e in entries
                   if isinstance(e, dict) and e.get("target") == "host" and e.get("vault_path")})


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("vaults", nargs="*", help="override/add specific vault paths")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--no-notify", action="store_true",
                        help="compute findings only — never call osascript (dry-run/evidence)")
    args = parser.parse_args(argv[1:])

    repo_src = _find_repo_src(Path(__file__))  # fix [7]: resolve once, pass down
    m = _load_maintenance(repo_src)
    if m is None:
        doc = {"status": "broken", "reason": "brain.maintenance unavailable "
               "(no repo checkout found and the installed package is missing "
               "offhost_watchdog_findings)"}
        print(json.dumps(doc, indent=2) if args.json else f"BROKEN: {doc['reason']}")
        return 2

    vaults = args.vaults or _registered_host_vaults(repo_src)
    if not vaults:
        doc = {"status": "no-vaults", "reason": "no host vault registered in "
               "~/.brainiac/workspaces.json and none passed as an argument"}
        print(json.dumps(doc, indent=2) if args.json else f"NO-VAULTS: {doc['reason']}")
        return 0  # nothing to watch yet is not a breach

    all_findings: dict[str, list[str]] = {}
    for v in vaults:
        all_findings[v] = m.offhost_watchdog_findings(Path(v))

    flat = [f for fs in all_findings.values() for f in fs]
    breached = bool(flat)

    # Fix [5]: --json implies no-notify (the help promises "no notification
    # side effect noise") — a machine-readable read must never also push a
    # macOS notification.
    if breached and not args.no_notify and not args.json:
        text = flat[0] if len(flat) == 1 else f"{len(flat)} watchdog finding(s) — see logs"
        m.fire_notification(text, title="Brainiac off-host watchdog")

    if args.json:
        print(json.dumps({"status": "breach" if breached else "healthy",
                          "findings": all_findings}, indent=2))
    elif breached:
        print(f"BREACH — {len(flat)} finding(s):")
        for f in flat:
            print(f"  - {f}")
    else:
        print(f"HEALTHY — {len(vaults)} vault(s) checked, no findings")

    return 1 if breached else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
