"""Degradation notifications: the maintain notify fold and marker retention."""
from __future__ import annotations

import datetime
import itertools
import json
import logging
import re
import shlex
from pathlib import Path
from typing import Any
import os
import platform
import subprocess
import hashlib


# ---------------------------------------------------------------------------
# OBS-02 — macOS degradation alarm. A trend regression, a tripped watchdog
# finding, or blocked>0 fires a single osascript notification per finding per
# day (dedup marker under ``.brain/notify-sent/``); ``BRAIN_NOTIFY=off`` kills
# it; non-macOS degrades to a log-only no-op (the Windows installer path is a
# separate, already-covered surface — WD-01's cloud push is the other leg).
# ---------------------------------------------------------------------------
NOTIFY_ENV = "BRAIN_NOTIFY"
NOTIFY_MARKER_RETENTION_DAYS_ENV = "BRAIN_NOTIFY_MARKER_RETENTION_DAYS"
DEFAULT_NOTIFY_MARKER_RETENTION_DAYS = 30


def degradation_findings(
    outcomes: dict[str, Any], trend_findings: list[dict[str, Any]],
    maintain_state: dict[str, Any] | None = None, today: datetime.date | None = None,
) -> list[tuple[str, str]]:
    """``(dedup_key, display_text)`` pairs, in priority order: blocked>0 (from
    ``outcomes`` — the ritual-level count, distinct from ``health_trend``'s
    own per-record ``blocked`` metric check so a blocked item is never
    reported twice), the synthesis watchdog (if tripped this run), then every
    trend regression. Pure — no I/O, no dedup bookkeeping (that's
    ``should_notify``).

    The KEY is a STABLE per-finding-identity string (``"blocked"``,
    ``"synthesis-watchdog"``, ``"trend:<metric>"``); the TEXT is the
    human-readable notification body, which DELIBERATELY carries the
    fluctuating measured values (``540ms vs baseline 480ms``). Review finding
    [1]: the dedup marker must hash the KEY, never the value-bearing text —
    otherwise the same ongoing regression, whose daily-median ``current``
    shifts each hourly run, hashes a different marker every hour and re-fires
    a fresh notification hourly, defeating "one notification per finding per
    day."

    Fix [3]: ``health_trend`` ALSO appends its own ``metric: "blocked"`` entry
    to ``trend_findings`` whenever the latest record shows blocked>0 — that
    entry is skipped here so the SAME blocked condition never produces two
    findings in one run.

    ES-01: also folds in ``maintain_escalation(maintain_state)`` when
    ``maintain_state`` is given — a branch crossing the loud-escalation
    threshold (repeated failure, a stuck writer-lock skip, or a stale
    ``last_run`` liveness failure) notifies exactly like any other
    degradation finding, keyed ``branch-escalate:<branch>`` so it dedups
    per branch per day same as everything else here."""
    pairs: list[tuple[str, str]] = []
    blocked = (outcomes.get("counts") or {}).get("blocked", 0)
    if blocked:
        pairs.append(("blocked", f"{blocked} blocked finding(s) this maintain run"))
    for item in outcomes.get("action_required") or []:
        finding = str(item.get("finding", ""))
        if finding.startswith("brain-synthesis watchdog:"):
            pairs.append(("synthesis-watchdog", finding))
        elif item.get("notify_key"):
            # An action_required item may carry its OWN stable dedup key
            # (`ingest_quarantine_findings` does). Without one an item stays
            # in the maintain result and never reaches `brain alerts`.
            pairs.append((str(item["notify_key"]), finding))
    for f in trend_findings:
        metric = str(f.get("metric") or "")
        if metric == "blocked":
            continue  # already reported via outcomes.counts above
        text = str(f.get("summary") or f"{metric} regression")
        pairs.append((f"trend:{metric}", text))
    if maintain_state:
        esc = maintain_escalation(maintain_state, today)
        for b in esc["branches"]:
            text = f"maintain branch '{b['branch']}' escalated: {'; '.join(b['reasons'])}"
            pairs.append((f"branch-escalate:{b['branch']}", text))
        # WAT-01 dead-man's switch, lane 3: a stale/missing corpus-invariants
        # row and any recorded invariant regression become ordinary
        # degradation findings, so they ride the existing notify-marker path
        # that the SessionStart alerts hook already reads. This is deliberately
        # keyed on the EXPECTED state row rather than on the rows that happen
        # to be present — a fold that died cannot report its own death.
        from . import invariants as _inv

        live = _inv.liveness_finding(maintain_state, today)
        if live:
            pairs.append(live)
        for r in _inv.state_regressions(maintain_state):
            pairs.append((f"invariant:{r.get('metric')}", str(r.get("summary") or "")))
    return pairs


CURRENT_FINDINGS_FILE = "current.json"


def record_current_findings(
    vault: Path, findings: list[tuple[str, str]], today: datetime.date,
) -> None:
    """Overwrite the currently-true findings feed that ``brain alerts`` reads.

    The per-day ``*.marker`` files beside this one are the GUI notification's
    dedup: a marker records that a finding was ANNOUNCED today, never that it
    still holds. ``brain alerts`` read those markers as its feed until
    2026-08-20, so a condition fixed at noon kept alerting for two days — in
    one measured session three of the four reported lines were invariants
    already back at zero, and an alert that is wrong most of the time is an
    alert nobody reads.

    This file is rewritten from scratch on every maintain run, so a cleared
    condition clears here within the hour. Best-effort: a failed write must
    never fail a maintain run, and both the missing and the stale case are
    alerts of their own on the reading side — silence never means "all clear".
    """
    path = _notify_marker_dir(vault) / CURRENT_FINDINGS_FILE
    payload = {
        "at": today.isoformat(),
        "findings": [{"key": key, "text": text} for key, text in findings],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        _log.warning("[degradation] current-findings feed unwritable (%s); "
                     "`brain alerts` will report it as missing", path.parent)


def _notify_marker_dir(vault: Path) -> Path:
    from . import config as _config

    return _config.brain_runtime_dir(vault) / "notify-sent"


def _notify_marker_path(vault: Path, key: str, today: datetime.date) -> Path:
    """Marker path for a STABLE dedup ``key`` (not the value-bearing display
    text — review finding [1]). One marker per key per day."""

    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return _notify_marker_dir(vault) / f"{today.isoformat()}-{digest}.marker"


def _prune_notify_markers(vault: Path, retention_days: int | None = None) -> None:
    """Delete per-day dedup markers older than ``retention_days`` (default
    30, env-overridable — fix [7]): one marker file accumulates per unique
    finding per day forever otherwise. Best-effort, never raises."""

    retention = retention_days if retention_days is not None else int(
        os.environ.get(NOTIFY_MARKER_RETENTION_DAYS_ENV, DEFAULT_NOTIFY_MARKER_RETENTION_DAYS))
    _prune_old_files(_notify_marker_dir(vault), "*.marker", retention)


def should_notify(vault: Path, key: str, today: datetime.date) -> bool:
    """True iff this ``key`` has NOT yet been surfaced today. PURE READ — no
    marker write (review finding [1]: the check used to write the marker
    eagerly). Pair with ``mark_notified``."""
    return not _notify_marker_path(vault, key, today).exists()


def mark_notified(vault: Path, key: str, today: datetime.date) -> str:
    """ATOMICALLY claim the per-day dedup marker for ``key`` via an
    ``O_CREAT | O_EXCL`` create. Returns:

    - ``"claimed"`` — this call created the marker (we own the notification);
    - ``"exists"``  — the marker already existed, so an earlier run today (or a
      concurrently-overlapping maintain) already surfaced this key;
    - ``"unwritable"`` — the marker dir could not be written.

    The exclusive create closes the check-then-write TOCTOU (review finding
    [1]): the coarse 2h maintain-lock auto-break can let two maintains overlap
    (the health-history append is locked for exactly this reason), and a
    plain ``should_notify`` read + later write let both fire the same banner.
    Now only ONE overlapping run wins the create; the other sees ``"exists"``
    and skips.

    ``"unwritable"`` is best-effort (review finding [1] sibling): on a
    read-only/full ``.brain`` dedup state cannot be persisted by ANY design —
    the caller still fires (the finding must not be lost) and it may re-surface
    next run until the dir recovers. The durable WARNING ``[degradation]`` log
    line fires regardless, so the finding is never lost."""

    marker = _notify_marker_path(vault, key, today)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return "exists"
    except OSError:
        _log.warning("[degradation] dedup marker unwritable (%s); "
                     "finding may re-surface next run", marker.parent)
        return "unwritable"
    try:
        os.write(fd, key.encode("utf-8"))
    finally:
        os.close(fd)
    return "claimed"


def fire_notification(text: str, *, title: str = "Brainiac health") -> str:
    """Best-effort ``osascript`` notification on macOS. Returns
    ``"skipped (non-macOS)"`` off Darwin and ``"failed: …"`` on a send error,
    never raises — a GUI ping is pure convenience on top of the durable log
    line the caller always emits. Slowing or failing the maintain run over a
    notification is never acceptable."""

    if platform.system() != "Darwin":
        return "skipped (non-macOS)"
    # The kill switch lives HERE, not only in `pending_notifications`: every
    # banner goes through this one call, and a caller reaching it directly
    # (`fire_and_mark_notifications`, a test, future code) used to sail past
    # the gate and write to the owner's real Notification Center. Measured
    # 2026-08-21: 28 identical fixture banners in one day from one test.
    if os.environ.get(NOTIFY_ENV, "").strip().lower() == "off":
        return "skipped (BRAIN_NOTIFY=off)"
    try:
        result = subprocess.run(
            ["osascript", "-e",
             f'display notification {json.dumps(text)} with title {json.dumps(title)}'],
            check=False, capture_output=True, timeout=5,
        )
        if result.returncode != 0:
            return "failed: osascript rc={}".format(result.returncode)
        return "notified"
    except Exception as exc:  # noqa: BLE001 — a notification failure is cosmetic
        return f"failed: {type(exc).__name__}"


def pending_notifications(
    vault: Path, outcomes: dict[str, Any], trend_findings: list[dict[str, Any]],
    today: datetime.date, maintain_state: dict[str, Any] | None = None,
    router: Any = None,
) -> list[tuple[str, str]]:
    """The ``(key, text)`` findings still pending notification for TODAY —
    empty when ``BRAIN_NOTIFY=off`` or when every candidate was already
    surfaced today (dedup by stable KEY). Does not MARK — pair with
    ``fire_and_mark_notifications``. Two side effects, both because this is
    called exactly once per maintain run: it prunes old markers (fix [7]) and
    it overwrites the currently-true findings feed ``brain alerts`` reads
    (``record_current_findings``).

    ``maintain_state`` (optional, ES-01) folds branch-escalation findings in —
    see ``degradation_findings``.

    ``router`` (optional, REG-03) is the remediation routing step
    (``remediation_routing.router_for``): it takes the raw findings and returns
    only the ones that still BANNER, having sent the rest to ``hot.md`` or to
    the owner inbox on the way. It is applied BEFORE ``record_current_findings``
    so the feed `brain alerts` reads carries the banner set — a finding a live
    branch is fixing is a log line, not a session-start alarm. No router (every
    caller that does not pass one) is exactly the behaviour that shipped
    before, and a router that raises is caught inside itself and returns the
    findings untouched."""

    _prune_notify_markers(vault)
    findings = degradation_findings(outcomes, trend_findings, maintain_state, today)
    if router is not None:
        findings = router(findings)
    # Recorded BEFORE the BRAIN_NOTIFY check: `brain alerts` is a separate
    # channel from the GUI ping, and turning the ping off must not blind it.
    record_current_findings(vault, findings, today)
    if os.environ.get(NOTIFY_ENV, "").strip().lower() == "off":
        return []
    return [(k, t) for (k, t) in findings if should_notify(vault, k, today)]


def fire_and_mark_notifications(
    vault: Path, findings: list[tuple[str, str]], today: datetime.date,
) -> list[str]:
    """Surface each pending ``(key, text)`` finding, then persist its per-day
    dedup marker. Returns the display texts surfaced this call.

    Two review fixes converge here:

    - **Fix [4] / durable half of [2]:** a WARNING log line is emitted for
      every finding FIRST, independent of the GUI channel and platform, so a
      failed ``osascript`` (headless/non-Aqua launchd) or a non-macOS host
      never means the degradation went unrecorded. The GUI ``osascript`` ping
      is best-effort convenience on top; its result is intentionally ignored.
    - **Fix [2]:** the marker is written REGARDLESS of the GUI send result, so
      the finding is surfaced at most ONCE per key per day. ``osascript`` on a
      headless/non-Aqua launchd session is PERMANENTLY unavailable, not
      transient — the prior "withhold the marker on failure and retry next
      run" behavior just respawned a subprocess that can never succeed, every
      hour, forever. The durable WARNING log above is the channel that
      guarantees the finding is never lost; the once-a-day GUI ping is not."""
    sent: list[str] = []
    for key, text in findings:
        # Claim the day's marker FIRST (atomic O_EXCL). "exists" means a
        # concurrently-overlapping maintain already surfaced this key today —
        # skip, so one condition never double-fires (finding [1]). "claimed"
        # and "unwritable" both fire: "unwritable" is the degraded read-only
        # `.brain` case where the finding must still reach the durable log
        # (finding [0] tradeoff — it may re-surface next run).
        if mark_notified(vault, key, today) == "exists":
            continue
        _log.warning("[degradation] %s", text)
        _maintenance.fire_notification(text)  # best-effort GUI ping; result ignored by design
        sent.append(text)
    return sent

# Cross-section binds, deferred past this module's own defs.
from .maintenance_escalation import maintain_escalation as maintain_escalation  # noqa: E402
from .maintenance_healthlog import _prune_old_files as _prune_old_files  # noqa: E402
from .maintenance import _log as _log  # noqa: E402
from . import maintenance as _maintenance  # noqa: E402
