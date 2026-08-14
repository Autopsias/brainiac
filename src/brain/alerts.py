"""``brain alerts`` — the ONE place that decides what a degraded vault is
saying, so every harness reads the same list from the same code.

Until 2026-08-14 this logic lived only inside a Claude Code SessionStart hook
(``~/.claude/hooks/brainiac-alerts.sh``). Codex, the Cowork VM and the Desktop
Code tab were locked out BY CONSTRUCTION, not by capability: the inputs are six
plain files and every harness can read files. The measured cost was a Cowork
session working for days against a vault whose ``unlinked_sources`` corpus
invariant had regressed, with no surface that could tell it. Moving the logic
into the engine makes the hook a thin caller and gives every other harness the
identical list.

Two design rules, both learned from earlier failures in this repo:

* **Silence must never be able to mean "could not look."** Two of the five
  inputs live in the HOST home (``~/.brainiac``, ``~/.brain``) and the Cowork
  VM cannot reach them. On ``role=vm`` they are reported in ``unreachable``
  rather than quietly contributing nothing — the same posture ``doctor`` takes
  with its host-only surfaces.
* **Pure file reads.** No index, no embedder, no network, no signing key. This
  runs on every session start of every harness; it stays cheap enough that no
  one is tempted to disable it.
"""

from __future__ import annotations

import datetime
import glob
import json
import os
from pathlib import Path
from typing import Any

# A degradation marker is written at most once per finding per day by
# `brain maintain`. Reporting today's AND yesterday's covers the case of a
# finding raised late at night and a session opened the next morning; the
# marker for a condition that has since cleared ages out on its own.
MARKER_WINDOW_DAYS = 1

# An auto-update record older than this is stale news, not an alert — a
# stopped or dead `maintain` must not nag forever.
UPDATE_STATE_MAX_AGE_DAYS = 7

# A weekly task that has not succeeded in longer than this has missed a run.
SYNTHESIS_STALE_DAYS = 8


def _read_json(path: Path) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _date(value: Any) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _alert(key: str, text: str, scope: str = "") -> dict[str, str]:
    return {"key": key, "text": text, "scope": scope}


# ---------------------------------------------------------------------------
# Host-home sources — unreachable from the VM leg
# ---------------------------------------------------------------------------

def update_alerts(home: Path, today: datetime.date) -> list[dict[str, str]]:
    """The auto-update marker the hourly maintain writes (``~/.brainiac``)."""
    state = _read_json(home / ".brainiac" / "update-state.json")
    if not isinstance(state, dict):
        return []
    at = _date(state.get("at"))
    if at is not None and (today - at).days > UPDATE_STATE_MAX_AGE_DAYS:
        return []
    latest = state.get("latest") or "?"
    status = state.get("status")
    if status == "applied":
        return [_alert("update:applied",
                       f"Brainiac auto-updated to {latest} (if the Cowork Desktop "
                       "skill store is stale, one click finishes it)")]
    if status == "failed":
        detail = state.get("detail") or "unknown step"
        return [_alert("update:failed",
                       f"Brainiac auto-update to {latest} FAILED at {detail} "
                       "— run 'brain update'")]
    if status == "available":
        return [_alert("update:available",
                       f"Brainiac update {latest} available — run 'brain update'")]
    return []


def synthesis_alerts(home: Path, today: datetime.date) -> list[dict[str, str]]:
    """Weekly synthesis task health (``~/.brain/synthesis-state.json``)."""
    state = _read_json(home / ".brain" / "synthesis-state.json")
    if not isinstance(state, dict):
        return []
    out: list[dict[str, str]] = []
    for vault, entry in state.items():
        if not isinstance(entry, dict):
            continue
        name = os.path.basename(os.path.dirname(str(vault))) or str(vault)
        rc = entry.get("rc")
        last_ok = entry.get("last_success")
        if rc not in (0, None):
            out.append(_alert(
                "synthesis:failing",
                f"weekly synthesis FAILING (rc={rc}, last success {last_ok or 'never'})",
                name))
            continue
        day = _date(last_ok)
        if day is not None:
            age = (today - day).days
            if age > SYNTHESIS_STALE_DAYS:
                out.append(_alert(
                    "synthesis:stale",
                    f"weekly synthesis STALE (last success {last_ok}, {age}d ago)",
                    name))
    return out


# ---------------------------------------------------------------------------
# Per-vault sources — on the shared mount, so BOTH roles read them
# ---------------------------------------------------------------------------

def vault_alerts(vault: Path, today: datetime.date) -> list[dict[str, str]]:
    """Engine-feedback backlog, owner-decision queue, and the degradation
    markers ``brain maintain`` already writes under ``.brain/notify-sent/``."""
    name = vault.parent.name or str(vault)
    out: list[dict[str, str]] = []

    pending_bugs = glob.glob(str(vault / ".brain" / "engine-feedback" / "*.md"))
    if pending_bugs:
        out.append(_alert("engine-feedback",
                          f"{len(pending_bugs)} engine-feedback bug prompt(s) waiting",
                          name))

    try:
        with open(vault / ".brain" / "memory" / "inbox.jsonl", encoding="utf-8") as fh:
            open_questions = sum(
                1 for line in fh
                if line.strip() and json.loads(line).get("status", "open") == "open")
    except Exception:
        open_questions = 0
    if open_questions:
        out.append(_alert("owner-inbox",
                          f"{open_questions} owner decision(s) queued (use /brain-inbox)",
                          name))

    keys: set[str] = set()
    for marker in glob.glob(str(vault / ".brain" / "notify-sent" / "*.marker")):
        day = _date(os.path.basename(marker))
        if day is None or (today - day).days > MARKER_WINDOW_DAYS:
            continue
        try:
            keys.add(Path(marker).read_text(encoding="utf-8").strip() or "unknown")
        except OSError:
            continue
    # The synthesis watchdog duplicates `synthesis_alerts` above; the blocked,
    # trend and invariant keys are news.
    keys.discard("synthesis-watchdog")
    if keys:
        out.append(_alert("degradation",
                          "degradation finding(s) in last 48h: " + ", ".join(sorted(keys)),
                          name))
    return out


def host_vaults(home: Path) -> list[Path]:
    """Every host-target vault in the workspace registry, deduped, in order."""
    registry = _read_json(home / ".brainiac" / "workspaces.json")
    if not isinstance(registry, dict):
        return []
    seen: list[Path] = []
    for entry in registry.get("entries", []):
        if not isinstance(entry, dict) or entry.get("target") != "host":
            continue
        raw = entry.get("vault_path")
        if not raw:
            continue
        path = Path(raw)
        if path not in seen:
            seen.append(path)
    return seen


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def collect(
    *,
    role: str = "host",
    vault: str | os.PathLike[str] | None = None,
    home: Path | None = None,
    today: datetime.date | None = None,
) -> dict[str, Any]:
    """Every alert this role can see, plus what it could not look at.

    ``host`` sweeps the workspace registry; ``vm`` reads only its own vault and
    reports the two host-home sources as unreachable."""
    home = home or Path.home()
    today = today or datetime.date.today()
    alerts: list[dict[str, str]] = []
    unreachable: list[str] = []

    if role == "vm":
        unreachable = [
            "auto-update state (~/.brainiac/update-state.json) — host-only, "
            "run `brain alerts` on the host Mac",
            "weekly synthesis task health (~/.brain/synthesis-state.json) — "
            "host-only, run `brain alerts` on the host Mac",
        ]
        vaults = [Path(vault)] if vault else []
        if not vaults:
            # The VM leg has exactly one vault and no registry to fall back on.
            # If it could not be resolved there is NOTHING behind a "no alerts"
            # answer, so say that instead of returning a clean bill of health.
            unreachable.append(
                "this vault — no --vault/$BRAIN_VAULT and no vault at ./vault, "
                "so nothing was inspected")
    else:
        alerts += update_alerts(home, today)
        alerts += synthesis_alerts(home, today)
        vaults = host_vaults(home)
        if not vaults and vault:
            vaults = [Path(vault)]
        if not vaults:
            unreachable.append(
                "no vault inspected — the workspace registry "
                "(~/.brainiac/workspaces.json) lists no host vault and none was "
                "given; run `brain alerts --vault <path>`")

    for path in vaults:
        alerts += vault_alerts(path, today)

    return {
        "role": role,
        "vaults": [str(p) for p in vaults],
        "alerts": alerts,
        "unreachable": unreachable,
        "ok": not alerts,
    }


def render_human(report: dict[str, Any]) -> str:
    """One line per alert. Deliberately terse — this is read at session start."""
    lines: list[str] = []
    for item in report["alerts"]:
        scope = item.get("scope")
        lines.append(f"  ! {scope + ': ' if scope else ''}{item['text']}")
    if not lines:
        lines.append("  no alerts")
    for note in report.get("unreachable", []):
        lines.append(f"  - not checkable from role={report['role']}: {note}")
    header = f"brain alerts — {len(report['alerts'])} finding(s), role={report['role']}"
    return "\n".join([header, *lines])


def one_line(report: dict[str, Any]) -> str:
    """The banner form a SessionStart hook injects. Empty when all clear."""
    if not report["alerts"]:
        return ""
    parts = []
    for item in report["alerts"]:
        scope = item.get("scope")
        parts.append(f"{scope}: {item['text']}" if scope else item["text"])
    return "BRAINIAC ALERTS: " + " | ".join(parts)
