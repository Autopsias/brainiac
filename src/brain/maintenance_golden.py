"""The Sunday cross-family golden-probe execution fold (WD-03)."""
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


# ---------------------------------------------------------------------------
# WD-03 (2026-07-12) — Sunday cross-family golden-probe EXECUTION. Codex (the
# family that did NOT build the retrieval engine) shells the SAME `brain`
# CLI the probes exercise — this is cross-family EXECUTION of a deterministic
# scorer, correction 5: NEVER "independent verification" / "independent
# eyes" / "Codex grades retrieval". A shared retrieval bug is invisible to
# both invokers; only the INVOKER differs, not the measurement.
#
# Pure helpers only, here (parsing/validation/marker arithmetic) — the actual
# `codex exec` / self-run subprocess calls are host I/O and live on
# `BrainCore._run_golden_probe` (mirrors `_run_bounded_graphify`'s split).
# The 4 exit codes mirror `brain.golden_probe`'s own contract BY HAND (that
# module is deliberately engine-decoupled/stdlib-only and never imports
# anything from this package, so this fold never imports it either — see
# golden_probe.py's own VALID_TIERS for the same by-hand-sync precedent).
# ---------------------------------------------------------------------------
GOLDEN_EXIT_OK = 0
GOLDEN_EXIT_REGRESSION = 1
GOLDEN_EXIT_ACTION_REQUIRED = 2
GOLDEN_EXIT_TRANSIENT = 3
_GOLDEN_VALID_EXIT_CODES = (GOLDEN_EXIT_OK, GOLDEN_EXIT_REGRESSION,
                            GOLDEN_EXIT_ACTION_REQUIRED, GOLDEN_EXIT_TRANSIENT)
_GOLDEN_VALID_DISPOSITIONS = ("ok", "regression", "action_required", "transient")

GOLDEN_RETRY_BASE_MINUTES_ENV = "BRAIN_GOLDEN_RETRY_BASE_MINUTES"
# 6h base (re-review): the old 60m EQUALLED the hourly maintain cadence, so a
# run repeatedly killed mid-`codex exec` re-fired every hour despite the
# provisional backoff. golden is a WEEKLY branch — a base well above the
# cadence, escalating on consecutive failures (incl. kills), is the point.
DEFAULT_GOLDEN_RETRY_BASE_MINUTES = 360
GOLDEN_RETRY_MAX_MULTIPLIER = 8  # capped exponential backoff, same shape as graphify's

GOLDEN_CODEX_TIMEOUT_SECONDS_ENV = "BRAIN_GOLDEN_CODEX_TIMEOUT_SECONDS"
# HARD cap <=10min (correction 1) — strictly below the 2h maintain-lock stale
# window, so a wedged codex child can never itself become the reason a
# concurrent maintain run thinks the lock is abandoned.
DEFAULT_GOLDEN_CODEX_TIMEOUT_SECONDS = 600
MAX_GOLDEN_CODEX_TIMEOUT_SECONDS = 600


def golden_codex_timeout_seconds() -> int:
    import os as _os

    raw = int(_os.environ.get(GOLDEN_CODEX_TIMEOUT_SECONDS_ENV, DEFAULT_GOLDEN_CODEX_TIMEOUT_SECONDS))
    return max(1, min(raw, MAX_GOLDEN_CODEX_TIMEOUT_SECONDS))


def golden_probes_path(vault: Path) -> Path:
    """Per-vault probes file (WD-02) — absence is a loud SKIP, never an
    error (session context bundle: 'skips loudly when codex is absent' /
    here, when the probes file itself is absent)."""
    return Path(vault) / "eval" / "golden-probes.json"


def build_codex_golden_prompt(probes_path: Path, vault: Path, python_exe: str) -> str:
    """The FIXED instruction handed to `codex exec` (correction 1): run
    ONLY the golden-probe scorer, read-only, and return ONLY its JSON — no
    prose wrapper — so the caller's strict shape/range validation is
    checking the scorer's own emitted document, not codex's summary of it.

    ``python_exe`` (review fixes [2] + the re-review's OUTER-interpreter fix)
    is the ABSOLUTE host interpreter that has ``brain`` importable
    (``sys.executable`` from the running maintain). BOTH the outer
    ``-m brain.golden_probe`` invocation AND the inner ``--brain-cmd`` use it:
    a bare ``python3`` here would ``ModuleNotFoundError`` under codex's ambient
    interpreter on a uv-tool/pipx-isolated brain install (the recommended
    channels), so the codex leg would fail every Sunday to the degraded
    self-run and cross-family EXECUTION would never actually happen."""
    brain_cmd = shlex.join([python_exe, "-m", "brain.cli"])
    return (
        "Run exactly this command and reply with ONLY its stdout, verbatim, "
        "and nothing else before or after it:\n\n"
        f"{shlex.quote(python_exe)} -m brain.golden_probe {shlex.quote(str(probes_path))} "
        f"--vault {shlex.quote(str(vault))} "
        f"--brain-cmd {shlex.quote(brain_cmd)}\n\n"
        "Do not modify any files. Do not run any other command. Do not "
        "interpret, summarize, explain, or comment on the result — your "
        "entire reply must be exactly that command's JSON stdout."
    )


def parse_codex_final_message(stdout: str) -> str | None:
    """Extract the text of the LAST `item.completed` event whose
    `item.type == "agent_message"` from a `codex exec --json` JSONL stream
    (correction 1): the stream interleaves thread/turn/tool-call/error
    events, so a caller must never treat the first (or only) JSON-shaped
    line as the answer — a run can emit an `item.type: "error"` info event
    before its real final message. Returns ``None`` when no agent_message
    event is found (or the stream is not JSONL at all); the caller treats
    that as a codex-path failure and falls back to the self-run."""
    last_text: str | None = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str):
                last_text = text
    return last_text


def validate_golden_probe_doc(doc: Any) -> str | None:
    """Strict shape/range check on a parsed golden-probe result document
    (from either the codex path or the self-run fallback) — the
    "exit-0-with-garbage" trap (correction 1): a codex run can exit 0 while
    its final message is empty prose, a truncated fragment, or a
    well-formed-but-nonsensical object. Returns an error string, or
    ``None`` when the doc is trustworthy enough to source a score from."""
    if not isinstance(doc, dict):
        return f"not a JSON object: {type(doc).__name__}"
    if "disposition" not in doc or "exit_code" not in doc:
        return "missing disposition/exit_code key(s)"
    disposition = doc.get("disposition")
    if disposition not in _GOLDEN_VALID_DISPOSITIONS:
        return f"unrecognized disposition: {disposition!r}"
    exit_code = doc.get("exit_code")
    if isinstance(exit_code, bool) or exit_code not in _GOLDEN_VALID_EXIT_CODES:
        return f"unrecognized exit_code: {exit_code!r}"
    score = doc.get("score")
    if score is not None:
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            return f"score is not a number: {score!r}"
        if not (0.0 <= float(score) <= 1.0):
            return f"score out of [0,1]: {score!r}"
    return None


def golden_retry_backoff_minutes(base_minutes: int, consecutive_transient: int) -> int:
    """Capped exponential backoff, same shape as `graphify_backoff_days`:
    each consecutive TRANSIENT failure doubles the effective wait, capped at
    `GOLDEN_RETRY_MAX_MULTIPLIER`x, so a flaky codex/CLI backs off instead of
    re-attempting (and re-failing) every single hourly maintain run."""
    multiplier = min(2 ** max(0, consecutive_transient - 1), GOLDEN_RETRY_MAX_MULTIPLIER)
    return base_minutes * multiplier


def golden_attempt_due(marker: dict[str, Any] | None, now: datetime.datetime) -> bool:
    """True iff the persisted `_golden_attempt` marker's `next_retry_at` has
    elapsed (or there is none yet — never attempted, or the last attempt
    resolved deterministically and cleared it). A corrupt/unparsable
    timestamp degrades to "due now" rather than permanently wedging the
    branch (mirrors `graphify_drift_marker_due`'s same fail-open posture)."""
    nxt = (marker or {}).get("next_retry_at")
    if not nxt:
        return True
    try:
        nxt_dt = datetime.datetime.fromisoformat(str(nxt).replace("Z", "+00:00"))
    except ValueError:
        return True
    if nxt_dt.tzinfo is None:
        nxt_dt = nxt_dt.replace(tzinfo=datetime.timezone.utc)
    return now >= nxt_dt


def update_golden_attempt_marker(
    marker: dict[str, Any] | None, now: datetime.datetime, *,
    transient: bool, base_minutes: int | None = None,
) -> dict[str, Any]:
    """The `_golden_attempt` marker to persist AFTER an attempt (the caller
    persists `last_attempt` itself BEFORE the shell-out, mirroring
    `_run_bounded_graphify`'s crash-safety ordering). `transient` is True
    ONLY for exit 3 — every other resolved outcome (ok/regression/
    action_required) is a DETERMINISTIC answer and resets the backoff, since
    the branch got its weekly answer whatever it was."""
    import os as _os

    base = base_minutes if base_minutes is not None else int(
        _os.environ.get(GOLDEN_RETRY_BASE_MINUTES_ENV, DEFAULT_GOLDEN_RETRY_BASE_MINUTES))
    prev = dict(marker or {})
    consecutive = int(prev.get("consecutive_transient_failures", 0))
    consecutive = consecutive + 1 if transient else 0
    out = dict(prev)
    out["consecutive_transient_failures"] = consecutive
    if transient:
        backoff_min = _maintenance.golden_retry_backoff_minutes(base, consecutive)
        out["next_retry_at"] = (now + datetime.timedelta(minutes=backoff_min)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    else:
        out["next_retry_at"] = None
    return out


def render_promote_scan_hot_entry(candidates: list[dict[str, Any]], today: datetime.date) -> str:
    # Neutral label (see render_curation_hot_entry) — catch-up runs fire on
    # non-Sunday weekdays, so a hardcoded "Sunday" mislabels them (field bug 1).
    lines = [f"## {today.isoformat()} — promotion-scan"]
    lines.append(
        f"- **Context:** {len(candidates)} `raw/` source(s) not yet promoted "
        "into a typed `brain/` note."
    )
    # Note id only — never the absolute path. The stored path is absolute, so
    # echoing it into hot.md left every entry stale after a vault move (field
    # bug 3); the id is a stable, move-proof handle.
    for c in candidates[:10]:
        lines.append(f"  - `{c.get('id')}`")
    if len(candidates) > 10:
        lines.append(f"  - … {len(candidates) - 10} more")
    lines.append(
        "- **Tier-1 (auto-resolved by the weekly synthesis session):** the "
        "obviously-promotable candidates are promoted into typed notes on the "
        "audited path; this is the LOG. A genuinely owner-only call is enqueued "
        "to the `brain inbox` instead."
    )
    return "\n".join(lines) + "\n"


def render_cos_waiting_hot_entry(
    waiting: list[str], today: datetime.date,
) -> str:
    """Log line for COS proposals held back by batch backpressure (ing-02).

    The owner queue holds at most one broker slot, so proposals claimed while
    a batch is open wait for the next one. That is correct — but it was
    INVISIBLE: two proposals sat unseen for two days behind an unanswered
    batch (measured 2026-07-27). This is a LOG entry, never an owner queue
    item: answering the open batch releases them automatically."""
    lines = [f"## {today.isoformat()} — COS proposals waiting"]
    lines.append(
        f"- **Context:** {len(waiting)} COS proposal(s) are held behind the "
        "currently-open ingestion batch (one owner-inbox broker slot at a "
        "time). Answering the open batch releases them into the next one."
    )
    for pid in waiting[:10]:
        lines.append(f"  - `{pid}`")
    if len(waiting) > 10:
        lines.append(f"  - … {len(waiting) - 10} more")
    lines.append(
        "- **No action needed:** this is the LOG, not a queue — the next "
        "broker run batches them once the open slot clears."
    )
    return "\n".join(lines) + "\n"

# Cross-section binds, deferred past this module's own defs.

# Parent-namespace bind, deferred past this module's own defs (the facade is
# where tests monkeypatch golden_retry_backoff_minutes).
from . import maintenance as _maintenance  # noqa: E402
