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
import re
from pathlib import Path
from typing import Any

from . import config as _config
from .alerts_staging import (  # noqa: F401 — re-exported for callers/tests
    _UNTRIAGED_FALLBACK,
    _UNTRIAGED_PREFIX_FALLBACK,
    _remediation,
    _staged_versions,
    _untriaged,
    _untriaged_key,
    _unwrap_untriaged,
    host_vaults,
    staging_alerts,
)

# `brain maintain` overwrites `notify-sent/current.json` at the end of every
# run with the findings TRUE AT THAT MOMENT. That file is this digest's feed.
#
# It reads the feed and not the sibling `*.marker` files, and the difference is
# the whole point: a marker records that a finding was ANNOUNCED on a given
# day, never that it still holds. Reading markers over a 48h window meant a
# condition fixed at noon kept alerting until the day after tomorrow — in one
# measured session (2026-08-20) three of the four reported lines were corpus
# invariants already back at zero. An alert that is usually wrong is an alert
# nobody reads, which is the exact failure this whole module exists to prevent.
CURRENT_FINDINGS_FILE = "current.json"

# The feed is only as current as the run that wrote it. Past this, `maintain`
# itself has stopped and the frozen findings inside are not the news — the
# silence is.
FINDINGS_STALE_DAYS = 2

# An auto-update record older than this is stale news, not an alert — a
# stopped or dead `maintain` must not nag forever.
UPDATE_STATE_MAX_AGE_DAYS = 7

# A weekly task that has not succeeded in longer than this has missed a run.
SYNTHESIS_STALE_DAYS = 8

# The feed's `findings[].key` vocabulary is closed (see maintenance_notify.py:
# "blocked", "synthesis-watchdog", "trend:<metric>", "invariant:<metric>",
# "branch-escalate:<branch>", "ingest_quarantine*", ...). The dir is on the
# VirtioFS mount the Cowork VM can write, so a key failing this shape must
# never reach the rendered alert text — it is the one thing standing between
# a forged feed and attacker-authored text in the host SessionStart context.
_FINDING_KEY_RE = re.compile(r"[a-z0-9:_-]{1,64}")


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

def _running_version() -> str | None:
    """This engine's version, or None if it cannot be read.

    Kept to the import floor `brain alerts` promises: a module attribute
    read, no index, embedder, network or key."""
    try:
        from ._version import __version__

        return str(__version__) or None
    except Exception:  # noqa: BLE001 — an unreadable stamp must not break alerts
        return None


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
    running = _running_version()
    if status == "applied":
        # FIX-04 (registry: `update:applied` is LOG, "good news is not an
        # alert"). Until 2026-08-21 this branch banner'd it here for up to
        # `UPDATE_STATE_MAX_AGE_DAYS` (7 days) every session — the dead banner
        # this fold's own hot.md line (written where the update is recorded,
        # `folds.reporting.auto_update_fold`) now replaces. Never re-add a
        # banner for it here; that is the exact regression this fix closes.
        return []
    if status == "failed":
        detail = state.get("detail") or "unknown step"
        if state.get("escalated"):
            attempts = state.get("attempts") or "several"
            return [_alert("update:failed",
                           f"Brainiac auto-update to {latest} FAILED {attempts} "
                           f"time(s) and has stopped retrying (last: {detail}) "
                           "— run 'brain update' by hand")]
        return [_alert("update:failed",
                       f"Brainiac auto-update to {latest} FAILED at {detail} "
                       "— run 'brain update'")]
    if status == "available":
        if running is not None and latest == running:
            return []  # already installed since the marker was written
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

def vault_alerts(
    vault: Path, today: datetime.date, *, role: str = "host",
) -> list[dict[str, str]]:
    """Engine-feedback backlog, the exceptions banner, and whatever is
    degraded right now per ``degradation_alerts`` below."""
    name = vault.parent.name or str(vault)
    out: list[dict[str, str]] = []

    pending_bugs = glob.glob(str(vault / ".brain" / "engine-feedback" / "*.md"))
    if pending_bugs:
        out.append(_alert("engine-feedback",
                          f"{len(pending_bugs)} engine-feedback bug prompt(s) waiting",
                          name))

    out += exceptions_alerts(vault, today, name, role=role)
    out += degradation_alerts(vault, today, name)
    return out


def exceptions_alerts(
    vault: Path, today: datetime.date, name: str, *, role: str,
) -> list[dict[str, str]]:
    """The one exceptions banner both roles now share: ``N thing(s) need
    you — run `brain exceptions --open`, or read <page path>``. NEVER reads ``inbox.jsonl`` directly any more (GRILL
    ruling 2026-08-20: "inbox.jsonl stays host-only doctrine; attacker-writable
    file existence is not evidence") — both roles read the SAME signed
    machine summary ``exceptions_page.generate()`` writes at the end of every
    ``brain maintain`` run (``.brain/exceptions.json``), which is what makes
    the VM count and the host count the SAME NUMBER by construction. The VM
    additionally VERIFIES that summary (signature, pinned vault_id, schema,
    freshness, page hash — ``exceptions_verify.verify``) before trusting
    anything in it; an unverifiable summary is reported unreachable, never a
    fabricated zero (HARDENED:adv-2026-08-20 / codex-verify-r1). The host
    trusts its own local file (it wrote it) but still checks staleness — the
    same posture ``degradation_alerts`` takes on the sibling feed."""
    from . import exceptions_page as _exc_page

    if role == "vm":
        from . import exceptions_verify as _exc_verify

        ok, summary, reason = _exc_verify.verify(vault, today)
        if not ok:
            return [_alert("exceptions:unreachable",
                           f"exceptions summary unreachable — {reason}", name)]
    else:
        summary = _read_json(_config.brain_runtime_dir(vault) / _exc_page.JSON_FILENAME)
        if not isinstance(summary, dict):
            # Same distinction `degradation_alerts` makes for its own feed: a
            # vault that ran `maintain` but predates this feature is
            # reportable; a vault that never ran `maintain` at all is
            # genuinely quiet.
            if _config.maintain_state_path(vault).exists():
                return [_alert("exceptions:no-summary",
                               "no exceptions summary yet — the engine "
                               "running this vault's `brain maintain` "
                               "predates it, so exceptions are UNREPORTED "
                               "until the next run", name)]
            return []
        at = _date(summary.get("generated_at"))
        if at is None or (today - at).days > FINDINGS_STALE_DAYS:
            return [_alert("exceptions:stale",
                           "exceptions summary is stale or unparseable — "
                           "nothing is watching this vault's exceptions", name)]

    count = int(summary.get("count") or 0)
    if not count:
        return []
    page = str(summary.get("page_path") or "")
    # Name the COMMAND, not only the path. This line fires at session start in
    # Claude Code, Codex and Cowork, and a bare path is unopenable in two of
    # the three — `brain exceptions --open` works on the host and reports why
    # it cannot in a sandbox, where `--text` prints the page instead.
    return [_alert(
        "exceptions",
        f"{count} thing(s) need you — run `brain exceptions --open` "
        f"(or --text), or read {page}", name)]


def degradation_alerts(
    vault: Path, today: datetime.date, name: str,
) -> list[dict[str, str]]:
    """What is degraded RIGHT NOW, from the feed the last maintain run wrote.

    Three outcomes, and none of them is a bare silence:

    * feed present and fresh — report the keys it holds;
    * feed present and stale — `maintain` has stopped, so say THAT instead of
      reciting findings frozen days ago;
    * feed absent on a vault that has run `maintain` at all — the engine
      writing that vault's runs predates this feed, so degradation is
      unreported until it next runs. A vault where nothing has ever run is
      genuinely quiet and stays quiet.
    """
    feed = _read_json(vault / ".brain" / "notify-sent" / CURRENT_FINDINGS_FILE)
    if not isinstance(feed, dict):
        if (vault / ".brain" / "maintain-state.json").exists():
            return [_alert("maintain:no-feed",
                           "no current-findings feed — the engine running this "
                           "vault's `brain maintain` predates it, so degradation "
                           "is UNREPORTED until the next run", name)]
        return []

    at = _date(feed.get("at"))
    if at is None:
        # Missing or malformed `at` — the host's own writer always sets it, so
        # a forged/broken feed fails CLOSED to stale rather than being read as
        # permanently current (a missing timestamp must not buy permanence).
        return [_alert("maintain:unparseable-feed",
                       "current-findings feed has a missing or malformed "
                       "timestamp — treating it as stale", name)]
    if (today - at).days > FINDINGS_STALE_DAYS:
        return [_alert("maintain:stale",
                       f"`brain maintain` last ran {at.isoformat()} "
                       f"({(today - at).days}d ago) — nothing is watching this "
                       "vault and its findings are that old", name)]

    raw_keys = {str(item.get("key")) for item in feed.get("findings") or []
                if isinstance(item, dict) and item.get("key")}
    # The synthesis watchdog duplicates `synthesis_alerts` above; the blocked,
    # trend and invariant keys are news.
    raw_keys.discard("synthesis-watchdog")

    keys = set()
    unrecognised = 0
    for key in raw_keys:
        if _FINDING_KEY_RE.fullmatch(key):
            keys.add(key)
        else:
            unrecognised += 1

    # REG-01/REG-02: a key no remediation disposition covers is DRIFT, and it
    # says so on its own line. Folding it into the roll-up would hide the one
    # thing the registry exists to make loud — a finding shipped without anyone
    # deciding what happens to it. Resolution is pure data (no I/O), so this
    # stays at the import floor `brain alerts` promises.
    untriaged_keys = {k for k in keys if _untriaged(k)}
    keys -= untriaged_keys
    # REG-03 stamps the key `untriaged:<key>` in the FEED, so a consumer that
    # is not this module still sees that nothing declared it. Unwrapped for
    # display: the reader wants the finding's own name, not the wrapper.
    untriaged = sorted(_unwrap_untriaged(k) for k in untriaged_keys)

    out = []
    if keys:
        out.append(_alert("degradation",
                          "degradation finding(s) now: " + ", ".join(sorted(keys)), name))
    if untriaged:
        out.append(_alert(_untriaged_key(),
                          f"{len(untriaged)} finding(s) have NO declared "
                          "remediation disposition — nothing decided whether "
                          "the engine fixes, asks, or logs these: "
                          + ", ".join(untriaged), name))
    if unrecognised:
        out.append(_alert("degradation:unrecognised-key",
                          f"{unrecognised} finding(s) in the current-findings "
                          "feed had a key outside the recognised alphabet and "
                          "were withheld", name))
    return out


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
        alerts += staging_alerts(home)
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
        alerts += vault_alerts(path, today, role=role)

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
