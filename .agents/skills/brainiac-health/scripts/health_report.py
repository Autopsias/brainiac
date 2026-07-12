#!/usr/bin/env python3
"""brainiac-health (OBS-03): one on-demand readout across every registered
HOST vault — doctor's surface checks, the two heartbeats (maintain +
synthesis), this week's trend deltas, quarantine growth, last synthesis
cost, and open hot-queue items — instead of assembling it from four
commands by hand.

Stdlib-only (no third-party deps), so it runs under whatever ``python3`` is
on PATH regardless of which channel installed the ``brain`` CLI (uv tool /
pipx / pip --user / editable venv). It never reimplements the trend-
regression algorithm — that stays owned by ``brain.maintenance.health_trend``
(and its siblings) and is invoked, not re-derived, per the S03 context
bundle's explicit instruction.

Usage:
    python3 health_report.py                 # every registered host vault
    python3 health_report.py <vault> [...]   # override/add specific vaults
"""
from __future__ import annotations

import datetime
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

TREND_LOOKBACK_DAYS = 7
HOT_QUEUE_TAIL_ENTRIES = 8


# ---------------------------------------------------------------------------
# Repo-fallback detection (S02 context bundle, HARDENED note): the installed
# `brain` package may be a STALE build that predates the OBS-01 trend
# helpers (read_health_history / read_sparse_history / health_trend). If a
# checkout of this repo is discoverable from this script's own location,
# prefer its `src/` on sys.path so the skill reflects the real, current
# engine even before the next `pip install`; otherwise fall back to whatever
# is actually installed. maintenance.py + config.py + brain/__init__.py are
# all pure-stdlib, so a bare system python3 can import them either way.
# ---------------------------------------------------------------------------
def _find_repo_src(start: Path) -> Path | None:
    cur = start.resolve()
    for _ in range(8):
        if (cur / "pyproject.toml").is_file() and (cur / "src" / "brain").is_dir():
            return cur / "src"
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def _clear_brain_modules() -> None:
    for mod in list(sys.modules):
        if mod == "brain" or mod.startswith("brain."):
            del sys.modules[mod]


def _try_import_maintenance() -> Any | None:
    try:
        import brain.maintenance as m  # noqa: PLC0415
    except ImportError:
        return None
    return m if hasattr(m, "health_trend") else None


def _venv_site_packages(brain_exe: str) -> Path | None:
    """This script runs under whatever bare ``python3`` invoked it, which is
    very likely NOT the isolated venv `brain` itself was installed into (uv
    tool / pipx both create their own venv, off the ambient Python path). If
    the plain import above can't see ``brain`` at all, read the ``brain``
    launcher's own shebang to find that venv's site-packages and add it to
    ``sys.path`` — the maintenance module is pure stdlib, so a different
    interpreter reading its .py source works fine, only compiled-extension
    packages would need the exact matching interpreter."""
    try:
        with Path(brain_exe).open(encoding="utf-8", errors="ignore") as fh:
            first_line = fh.readline().strip()
    except OSError:
        return None
    if not first_line.startswith("#!"):
        return None
    interp = Path(first_line[2:].strip())
    venv_root = interp.parent.parent  # <venv>/bin/pythonX.Y -> <venv>
    if sys.platform == "win32":
        candidate = venv_root / "Lib" / "site-packages"
        return candidate if candidate.is_dir() else None
    lib_dir = venv_root / "lib"
    if not lib_dir.is_dir():
        return None
    for child in sorted(lib_dir.glob("python*")):
        sp = child / "site-packages"
        if sp.is_dir():
            return sp
    return None


def _load_maintenance(brain_exe: str | None) -> tuple[Any | None, str]:
    """Returns (module_or_None, source_label). Prefers a discoverable repo
    checkout over whatever's already importable, since the repo copy is the
    one guaranteed to carry the current helpers during this hardening
    effort; falls back to the plain import, then to `brain`'s own venv
    site-packages (for isolated-venv channels a bare python3 can't see on
    its own). Never raises — a caller sees ``None`` and degrades the trend/
    heartbeat section instead of crashing the whole readout."""
    repo_src = _find_repo_src(Path(__file__))
    if repo_src is not None:
        sys.path.insert(0, str(repo_src))
        _clear_brain_modules()
        m = _try_import_maintenance()
        if m is not None:
            return m, f"repo checkout ({repo_src.parent})"

    m = _try_import_maintenance()
    if m is not None:
        return m, "installed package"

    venv_checked = None
    if brain_exe:
        venv_checked = _venv_site_packages(brain_exe)
        if venv_checked is not None:
            sys.path.insert(0, str(venv_checked))
            _clear_brain_modules()
            m = _try_import_maintenance()
            if m is not None:
                return m, f"brain's own venv ({venv_checked})"

    if venv_checked is not None:
        return None, (f"unavailable — no repo checkout found, and the installed package "
                       f"at {venv_checked} is missing the OBS-01 trend helpers "
                       f"(read_health_history/health_trend) — needs a real upgrade, not a path fix")
    return None, ("unavailable — no repo checkout found, the installed package is "
                  "missing the OBS-01 trend helpers, and brain's own venv "
                  "site-packages could not be resolved")


# ---------------------------------------------------------------------------
# Host vault registry (~/.brainiac/workspaces.json). Reuses
# tools/workspace_registry.py's reader when a repo checkout is discoverable
# (same pattern src/brain/doctor.py's own registry check uses); falls back to
# a plain read of the JSON file otherwise — never raises, an unreadable/
# missing registry just means "no vaults found".
# ---------------------------------------------------------------------------
def _registered_host_vaults() -> list[str]:
    repo_src = _find_repo_src(Path(__file__))
    if repo_src is not None:
        tools_dir = repo_src.parent / "tools"
        if (tools_dir / "workspace_registry.py").is_file():
            sys.path.insert(0, str(tools_dir))
            try:
                import workspace_registry as _wr  # noqa: PLC0415

                entries = _wr.list_entries(target="host")
                return sorted({e["vault_path"] for e in entries})
            except Exception:
                pass
    registry_path = Path.home() / ".brainiac" / "workspaces.json"
    try:
        data = json.loads(registry_path.read_text(encoding="utf-8"))
    except OSError:
        return []
    except ValueError:
        return []
    entries = data.get("entries", []) if isinstance(data, dict) else []
    return sorted({e["vault_path"] for e in entries
                   if isinstance(e, dict) and e.get("target") == "host" and e.get("vault_path")})


# ---------------------------------------------------------------------------
# brain CLI subprocess helpers
# ---------------------------------------------------------------------------
def _run_brain_json(brain_exe: str, vault: str, *args: str) -> tuple[dict | None, str | None]:
    """Runs `brain --vault <vault> <args> --json`. ``doctor`` (and
    potentially other subcommands) intentionally exits non-zero when it
    finds issues — same convention as a linter — so a non-zero return code
    with valid JSON on stdout is NOT an error; only a genuine crash (no
    parseable JSON at all) is reported as one."""
    try:
        proc = subprocess.run(
            [brain_exe, "--vault", vault, *args, "--json"],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    try:
        return json.loads(proc.stdout), None
    except ValueError:
        return None, (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()[:500]


# ---------------------------------------------------------------------------
# Deltas table — a plain "latest vs ~7 days ago" snapshot comparison. This is
# deliberately NOT the regression-detection algorithm (thresholds, daily
# bucketing reducers, span-gating all stay in health_trend) — just picking
# two display points for a human-readable table.
# ---------------------------------------------------------------------------
def _record_day(rec: dict) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(rec.get("ts") or "")[:10])
    except ValueError:
        return None


def _gauge_delta(history: list[dict], metric: str, today: datetime.date) -> dict:
    """Latest value vs. the value from the closest record at/before 7 days
    ago (falls back to the oldest record available, reporting the real gap)."""
    dated = sorted(
        ((_record_day(r), r.get(metric)) for r in history if _record_day(r) is not None),
        key=lambda x: x[0],
    )
    dated = [(d, v) for d, v in dated if v is not None]
    if len(dated) < 2:
        return {"current": dated[-1][1] if dated else None, "baseline": None, "lookback_days": None}
    current = dated[-1][1]
    target = today - datetime.timedelta(days=TREND_LOOKBACK_DAYS)
    at_or_before = [dv for dv in dated[:-1] if dv[0] <= target]
    baseline_day, baseline_val = (at_or_before[-1] if at_or_before else dated[0])
    return {"current": current, "baseline": baseline_val,
            "lookback_days": (dated[-1][0] - baseline_day).days}


def _sparse_delta(history: list[dict], sparse_history: list[dict], metric: str) -> dict:
    """Same idea as health_trend's own golden_score handling: last non-null
    vs. previous non-null, from the union of main + sparse history."""
    merged: dict[str, dict] = {}
    for r in list(history) + list(sparse_history):
        if r.get(metric) is None:
            continue
        rid = str(r.get("run_id") or r.get("ts"))
        merged[rid] = r
    points = sorted(merged.values(), key=lambda r: str(r.get("ts") or ""))
    if not points:
        return {"current": None, "baseline": None, "lookback_days": None}
    current = points[-1][metric]
    if len(points) < 2:
        return {"current": current, "baseline": None, "lookback_days": None}
    baseline = points[-2][metric]
    try:
        d1 = _record_day(points[-1])
        d0 = _record_day(points[-2])
        lookback = (d1 - d0).days if d1 and d0 else None
    except Exception:
        lookback = None
    return {"current": current, "baseline": baseline, "lookback_days": lookback}


def _arrow(current: Any, baseline: Any) -> str:
    if not isinstance(current, (int, float)) or not isinstance(baseline, (int, float)):
        return ""
    if current > baseline:
        return "up"
    if current < baseline:
        return "down"
    return "flat"


def _fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.2f}" if v < 100 else f"{v:.0f}"
    return str(v)


def _row_text(label: str, d: dict) -> str:
    cur, base = d["current"], d["baseline"]
    if base is None:
        return f"  {label:<16} current {_fmt(cur):>10}   baseline n/a (insufficient history)"
    arrow = _arrow(cur, base)
    pct = ""
    if isinstance(cur, (int, float)) and isinstance(base, (int, float)) and base:
        pct = f"  ({(cur - base) / base * 100:+.1f}%)"
    return (f"  {label:<16} current {_fmt(cur):>10}   ~{d['lookback_days']}d ago "
            f"{_fmt(base):>10}   [{arrow}]{pct}")


# ---------------------------------------------------------------------------
# Per-vault report
# ---------------------------------------------------------------------------
def _hot_queue_tail(vault: Path, n: int = HOT_QUEUE_TAIL_ENTRIES) -> list[str]:
    path = vault / ".brain" / "memory" / "hot.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    headings = [ln.strip("# ").strip() for ln in text.splitlines() if ln.startswith("## ")]
    return headings[-n:]


def build_vault_report(brain_exe: str, vault_path: str, m: Any, maintenance_source: str) -> dict:
    vault = Path(vault_path)
    today = datetime.date.today()
    report: dict[str, Any] = {"vault": vault_path, "problems": []}

    doctor, doctor_err = _run_brain_json(brain_exe, vault_path, "doctor")
    status, status_err = _run_brain_json(brain_exe, vault_path, "status")
    if doctor_err:
        report["problems"].append(("broken", f"brain doctor failed: {doctor_err}",
                                    "check `brain doctor` manually for the raw error"))
    if status_err:
        report["problems"].append(("broken", f"brain status failed: {status_err}",
                                    "check `brain status` manually for the raw error"))
    report["doctor"] = doctor
    report["status"] = status

    if doctor:
        for row in doctor.get("rows", []):
            if row.get("status") == "stale":
                report["problems"].append((
                    "degraded", f"{row['surface']}: {row['detail']}",
                    row.get("remediation") or "see `brain doctor --json` for detail"))
            elif row.get("status") == "unmanaged":
                report["problems"].append((
                    "degraded", f"{row['surface']}: {row['detail']}",
                    row.get("remediation") or "see `brain doctor --json` for detail"))

    if status:
        hb = status.get("maintain_heartbeat") or {}
        if hb.get("status") == "repeated_failures":
            report["problems"].append((
                "broken",
                f"maintain heartbeat: repeated failures in {hb.get('repeated_failure_branches')}",
                "check `~/.brain/logs` / launchctl for the brain-nightly task, "
                "then re-run `brain maintain` manually"))
        elif hb.get("status") == "stale":
            report["problems"].append((
                "degraded", f"maintain heartbeat: stale branch(es) {hb.get('stale_branches')}",
                "confirm the brain-nightly scheduled task is still registered "
                "(`brain doctor`), then re-run `brain maintain` manually"))
        pending = status.get("pending_drafts")
        if isinstance(pending, (int, float)) and pending > 0:
            report["pending_drafts"] = pending

    if m is None:
        report["problems"].append((
            "degraded", f"trend/heartbeat unavailable: {maintenance_source}",
            "run /brainiac-update to refresh the engine, then re-run /brainiac-health"))
        report["synthesis_cost"] = None
        report["deltas"] = {k: {"current": None, "baseline": None, "lookback_days": None}
                             for k in ("notes", "quarantine", "selftest_ms",
                                       "golden_score", "synthesis_cost_usd")}
    else:
        try:
            history = m.read_health_history(vault)
            sparse = m.read_sparse_history(vault)
            trend = m.health_trend(history, today, sparse_history=sparse)
            report["trend_findings"] = trend
            for f in trend:
                sev = "broken" if f["metric"] == "blocked" else "degraded"
                report["problems"].append((sev, f["summary"], _trend_remediation(f["metric"])))

            hb_finding = m.synthesis_heartbeat_finding(vault, today)
            if hb_finding:
                report["problems"].append((
                    "degraded", hb_finding["finding"], hb_finding["proposed_action"]))
            report["synthesis_cost"] = m.latest_synthesis_cost(vault)

            report["deltas"] = {
                "notes": _gauge_delta(history, "notes", today),
                "quarantine": _gauge_delta(history, "quarantine", today),
                "selftest_ms": _gauge_delta(history, "selftest_ms", today),
                "golden_score": _sparse_delta(history, sparse, "golden_score"),
                "synthesis_cost_usd": _sparse_delta(history, sparse, "synthesis_cost_usd"),
            }
        except AttributeError as exc:
            # Defensive: _load_maintenance already checks for health_trend
            # before returning a module, but a SIBLING helper (e.g. a future
            # engine version renaming synthesis_heartbeat_finding) could
            # still be missing — never crash the whole readout over one
            # missing trend surface when doctor/status are still useful.
            report["problems"].append((
                "degraded", f"trend/heartbeat partially unavailable: {exc}",
                "run /brainiac-update to refresh the engine, then re-run /brainiac-health"))
            report.setdefault("synthesis_cost", None)
            report.setdefault("deltas", {k: {"current": None, "baseline": None, "lookback_days": None}
                                          for k in ("notes", "quarantine", "selftest_ms",
                                                    "golden_score", "synthesis_cost_usd")})
    report["hot_queue"] = _hot_queue_tail(vault)
    return report


def _trend_remediation(metric: str) -> str:
    return {
        "blocked": "inspect `.brain/memory/hot.md` and re-run `brain maintain --json` "
                   "to see the blocked item's detail",
        "selftest_ms": "index/search latency regressed — consider `brain rebuild` "
                       "or check disk/CPU load on the host",
        "quarantine": "quarantine is growing faster than usual — run the "
                     "`vault-ingestion` skill or inspect `inbox/_quarantine/` directly",
        "golden_score": "retrieval quality regressed — run the `autoresearch` skill "
                        "or review recent promotions/curation findings",
    }.get(metric, "see `brain maintain --json` for detail")


def _verdict(problems: list[tuple[str, str, str]]) -> tuple[str, str]:
    broken = [p for p in problems if p[0] == "broken"]
    degraded = [p for p in problems if p[0] == "degraded"]
    if broken:
        return "BROKEN", broken[0][1]
    if degraded:
        return "DEGRADED", degraded[0][1]
    return "HEALTHY", "all checked surfaces current, no trend regressions, both heartbeats on cadence"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render_vault(report: dict) -> str:
    lines = [f"### {report['vault']}"]
    verdict, reason = _verdict(report["problems"])
    lines.append(f"VERDICT: {verdict} — {reason}")
    lines.append("")

    lines.append("Deltas (current vs ~7 days ago):")
    d = report["deltas"]
    lines.append(_row_text("notes", d["notes"]))
    lines.append(_row_text("quarantine", d["quarantine"]))
    lines.append(_row_text("latency_ms", d["selftest_ms"]))
    lines.append(_row_text("golden_score", d["golden_score"]))
    lines.append(_row_text("synthesis_$", d["synthesis_cost_usd"]))
    lines.append("")

    status = report.get("status") or {}
    hb = status.get("maintain_heartbeat") or {}
    if hb:
        lines.append(f"Maintain heartbeat: {hb.get('status', 'unknown')} "
                     f"({len(hb.get('branches', {}))} branch(es) tracked)")
    cost = report.get("synthesis_cost")
    lines.append(f"Last metered synthesis cost: {_fmt(cost)}"
                 + (" USD" if cost is not None else " (never measured)"))
    pending = report.get("pending_drafts")
    if pending:
        lines.append(f"Pending capture drafts awaiting host commit: {pending}")
    lines.append("")

    problems = report["problems"]
    if problems:
        lines.append(f"Open items ({len(problems)}):")
        for sev, finding, action in problems:
            lines.append(f"  [{sev.upper()}] {finding}")
            lines.append(f"    -> {action}")
    else:
        lines.append("Open items: none")

    hot = report.get("hot_queue") or []
    if hot:
        lines.append("")
        lines.append(f"Hot-queue tail ({len(hot)} most recent, `.brain/memory/hot.md`):")
        for h in hot:
            lines.append(f"  - {h}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    brain_exe = shutil.which("brain")
    if not brain_exe:
        print("VERDICT: BROKEN — no `brain` executable on PATH")
        print("  -> run /brainiac-install, or check the channel-specific bin dir is on PATH")
        return 1

    vaults = argv[1:] or _registered_host_vaults()
    if not vaults:
        print("VERDICT: BROKEN — no host vault registered in ~/.brainiac/workspaces.json "
              "and none passed as an argument")
        print("  -> run /brainiac-install, or pass a vault path explicitly")
        return 1

    m, source = _load_maintenance(brain_exe)
    reports = [build_vault_report(brain_exe, v, m, source) for v in vaults]

    overall = [_verdict(r["problems"])[0] for r in reports]
    order = {"BROKEN": 2, "DEGRADED": 1, "HEALTHY": 0}
    top = max(overall, key=lambda v: order[v]) if overall else "BROKEN"

    print(f"VERDICT: {top} across {len(reports)} host vault(s)")
    print(f"(trend helpers loaded from: {source})")
    print()
    for r in reports:
        print(render_vault(r))
    return 0 if top != "BROKEN" else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
