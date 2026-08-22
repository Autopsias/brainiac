"""EXC-02 — the exceptions page: everything that needs the owner, one file.

One HTML page per vault, regenerated at the end of every ``brain maintain``
run, showing: open owner questions (options/default/expiry), dead/escalated
automation, untriaged keys, and what the branches have healed lately. A
desktop ping points at it whenever a NEW exception appears.

TWO ARTIFACTS, tier-split (grill ruling, 2026-08-20 — binding).  A cross-tier
exception question NAMES two notes and one of them may sit above whatever
tier this vault's Cowork VM is allowed to read, so ONE page cannot serve both
audiences:

* the MOUNT page (``<vault>/.brain/exceptions.html``) — VM-readable, so it
  never carries a note above the VM egress ceiling. A pair whose exposure
  tier is above the ceiling renders as a ``withheld (tier)`` row, never the
  note names; every OTHER owner-class finding is reduced to a generic,
  key-derived sentence (never the raw ``question``/``context`` text a
  detector wrote, since that text is free-form and may itself quote a path).
  Every question id shown here is an OPAQUE random token — never the real
  key, never a note slug — with a host-only reverse map alongside it
  (HARDENED:adv-2026-08-20, "OPAQUE IDS").
* the FULL page (``config.index_dir()/exceptions-full.html``) — host-only,
  real ids, real text, real options. The desktop ping opens THIS one.

A third artifact, ``<vault>/.brain/exceptions.json``, is a SIGNED machine
summary (HARDENED:adv-2026-08-20, "SIGNED SUMMARY") a later session
(``brain --role vm alerts``, EXC-03) verifies before trusting: an invalid,
stale, unverifiable or future-schema summary must report `unreachable`,
never zero. Signing here only produces the artifact; VERIFYING it against a
runtime-pinned public key is that later session's job (HARDENED:codex-verify-r1)
— this module has no reader of its own to keep consistent with.

Dead-automation / untriaged findings are read from the SAME
``notify-sent/current.json`` feed ``brain alerts`` already renders on the
mount today (see ``alerts.degradation_alerts``) — this page adds no new
exposure there, it only re-groups an already mount-visible feed.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from . import alerts as _alerts
from . import alerts_staging as _alerts_staging
from . import classification as _classification
from . import config as _config
from . import inbox as _inbox
from . import maintenance as _maintenance
from . import maintenance_notify as _notify
from . import remediation as _rem
from . import remediation_exceptions as _rex
from .exceptions_render import render_page  # re-exported: the page's one
# renderer, split out on 2026-08-22 when this file hit the 500-line bound.

SCHEMA = "brain-exceptions/v1"
MOUNT_FILENAME = "exceptions.html"
JSON_FILENAME = "exceptions.json"
FULL_FILENAME = "exceptions-full.html"
TOKEN_MAP_FILENAME = "mount-tokens.json"
PING_STATE_FILENAME = "ping-state.json"

# ---------------------------------------------------------------------------
# Data collection — reads existing state, writes nothing.
# ---------------------------------------------------------------------------
def _current_findings(vault: Path, today: datetime.date) -> dict[str, Any]:
    """Split the SAME mount-visible feed ``brain alerts`` reads into the
    groups this page renders. ``stale``/``missing`` mirror
    ``alerts.degradation_alerts``'s own fail-closed reading of a forged or
    aged feed — this page must not read an untrustworthy feed as "all clear"
    either."""
    out: dict[str, Any] = {
        "dead_automation": [], "untriaged": [], "other": [],
        "stale": None, "missing": False,
    }
    feed = _alerts._read_json(vault / ".brain" / "notify-sent"
                              / _alerts.CURRENT_FINDINGS_FILE)
    if not isinstance(feed, dict):
        # Same distinction `alerts.degradation_alerts` makes: a vault that
        # has never run `maintain` at all is genuinely quiet, not degraded —
        # only a vault WITH a maintain-state.json but no feed predates the
        # feed (an engine upgrade mid-history) and that IS reportable.
        out["missing"] = _config.maintain_state_path(vault).exists()
        return out
    at = _alerts._date(feed.get("at"))
    if at is None or (today - at).days > _alerts.FINDINGS_STALE_DAYS:
        out["stale"] = at.isoformat() if at else feed.get("at")
        return out
    for item in feed.get("findings") or []:
        if not isinstance(item, dict):
            continue
        key, text = str(item.get("key") or ""), str(item.get("text") or "")
        if not key or key == "synthesis-watchdog":
            continue
        if not _alerts._FINDING_KEY_RE.fullmatch(key):
            continue  # a forged/malformed key is dropped, never rendered
        if _alerts_staging._untriaged(key):
            out["untriaged"].append((_alerts_staging._unwrap_untriaged(key), text))
        elif _rem.is_unsuppressible(key):
            out["dead_automation"].append((key, text))
        else:
            out["other"].append((key, text))
    return out


def _healed_summary(vault: Path) -> dict[str, Any]:
    """Today's per-branch snapshot (``maintain-state.json``'s
    ``_remediation`` row) plus a 7-day AGGREGATE healed total from
    ``health-history.jsonl`` — the history only carries the branches' SUMMED
    total per run (``maintenance_folds_2._remediation_fields``), never a
    per-branch breakdown, so the two are reported side by side rather than
    merged into one number that would misrepresent either."""
    per_branch: dict[str, dict[str, Any]] = {}
    try:
        state = json.loads(_config.maintain_state_path(vault).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    branches_now = (state.get("_remediation") or {}).get("branches") or {}
    for name in sorted(_rem.BRANCH_CADENCE_DAYS):
        row = branches_now.get(name) if isinstance(branches_now.get(name), dict) else {}
        per_branch[name] = {
            "healed": int((row or {}).get("healed", 0) or 0),
            "skipped": int((row or {}).get("skipped", 0) or 0),
            "remaining": int((row or {}).get("remaining", 0) or 0),
            "mode": str((row or {}).get("mode") or "n/a"),
        }
    total_7d = 0
    try:
        for rec in _maintenance.read_health_history(vault, window_days=7):
            value = rec.get("remediation_healed")
            if isinstance(value, int):
                total_7d += value
    except Exception:  # noqa: BLE001 — a trend is a bonus, never a blocker
        total_7d = 0
    return {"branches": per_branch, "healed_last_7_days_total": total_7d}


def _cost_trend(vault: Path) -> dict[str, Any] | None:
    """Remediation-cost records do not exist yet (a later session adds
    them); this degrades to "not yet available" rather than fabricate a
    number, and the section stays so the page's shape does not change again
    when that session lands.
    # ponytail: placeholder until remediation cost tracking ships.
    """
    return None


def collect_exceptions_data(core: Any, today: datetime.date | None = None) -> dict[str, Any]:
    """Everything the page renders, gathered from state this vault already
    maintains. Best-effort by design — a missing/unreadable source degrades
    to an empty section, never an exception that aborts the run."""
    vault = Path(core.vault)
    d = today or datetime.date.today()
    open_qs = _inbox.open_questions(core._read_inbox())
    pending_by_key = {str(m.get("question_key")): m for m in _rex.read_pending(vault)}
    questions = []
    for q in open_qs:
        key = str(q.get("key") or "")
        questions.append({
            "key": key,
            "question": str(q.get("question") or ""),
            "context": str(q.get("context") or ""),
            "options": list(q.get("options") or []),
            "default": q.get("default"),
            "source": str(q.get("source") or ""),
            "batch": pending_by_key.get(key),
        })
    return {
        "today": d,
        # The workspace folder name, exactly as `alerts.py` derives it — one
        # user has many vaults, and the page must say WHICH one it is about or
        # two open tabs are indistinguishable.
        "vault_name": vault.parent.name or str(vault),
        "questions": questions,
        "findings": _current_findings(vault, d),
        "healed": _healed_summary(vault),
        "cost_trend": _cost_trend(vault),
    }


def exception_keys(data: dict[str, Any]) -> list[str]:
    """The STABLE, real identity of every item on the page — never display
    text, never a count/cost/timestamp (grill ruling: "marker inputs must
    never embed counts, costs or timestamps"). This is what the ping hashes
    and what ``exceptions.json`` reports as ``keys``; it is computed BEFORE
    any opaque-token substitution, which is a display-only, per-render
    concern that must never affect whether an unchanged set re-pings."""
    keys = {str(q["key"]) for q in data["questions"]}
    findings = data["findings"]
    keys |= {k for k, _t in findings.get("dead_automation") or []}
    keys |= {k for k, _t in findings.get("untriaged") or []}
    keys |= {k for k, _t in findings.get("other") or []}
    return sorted(keys)


# ---------------------------------------------------------------------------
# The signed machine summary (HARDENED:adv-2026-08-20, "SIGNED SUMMARY").
# Producing it is this session's job; VERIFYING it against a runtime-pinned
# public key is EXC-03's (a later session) — see the module docstring.
# ---------------------------------------------------------------------------
def build_json_summary(
    vault: Path, *, html_bytes: bytes, count: int, keys: list[str],
    page_path: str, generated_at: datetime.date, ceiling: str,
) -> dict[str, Any]:
    """The signed ``exceptions.json`` payload. A signing failure (no key
    resolvable, e.g. a headless/CI run with no OS secret store) degrades to
    an UNSIGNED payload with ``sign_error`` set — never a crash, and never a
    forged signature. An unsigned summary is exactly what the future
    VM-side verifier must refuse and report `unreachable`, same as an
    invalid one."""
    from . import audit as _audit
    from . import snapshot as _snapshot
    from ._version import __version__ as _engine_version

    vault_id = _config.vault_id(vault, create=True) or ""
    gen_info = _snapshot.snapshot_status(_config.snapshot_dir(vault))
    generation = int(gen_info.get("generation") or 0)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA,
        "vault_id": vault_id,
        "generated_at": generated_at.isoformat(),
        "generation": generation,
        "egress_ceiling": ceiling,
        "html_hash": hashlib.sha256(html_bytes).hexdigest(),
        "min_engine": str(_engine_version or ""),
        "count": count,
        "keys": list(keys),
        "page_path": page_path,
    }
    try:
        key, _source = _audit.resolve_signing_key()
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        payload["signature"] = base64.b64encode(key.sign(canonical)).decode("ascii")
    except Exception as exc:  # noqa: BLE001 — unsigned, never unsafe
        payload["signature"] = ""
        payload["sign_error"] = f"{type(exc).__name__}: {exc}"
    return payload


# ---------------------------------------------------------------------------
# The ping — dedup on the STABLE key set, never on display text.
# ---------------------------------------------------------------------------
def _ping_state_path(vault: Path) -> Path:
    return _rex.exceptions_dir(vault) / PING_STATE_FILENAME


def _keys_hash(keys: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(keys)).encode("utf-8")).hexdigest()


def maybe_ping(vault: Path, keys: list[str], full_path: Path) -> str | None:
    """Fire the desktop ping iff the exception KEY SET changed AND is
    non-empty — an unchanged set never re-pings, and a set that went back to
    empty pings nothing (the empty page is the news there, not a
    notification). Returns the ``fire_notification`` result, or ``None`` when
    nothing was sent."""
    state_path = _ping_state_path(vault)
    try:
        prev = json.loads(state_path.read_text(encoding="utf-8"))
        prev_hash = str(prev.get("hash") or "") if isinstance(prev, dict) else ""
    except (OSError, ValueError):
        prev_hash = ""
    current_hash = _keys_hash(keys)
    fresh = bool(keys) and current_hash != prev_hash
    out = _notify.fire_notification(
        f"{len(keys)} thing(s) need you — open {full_path}") if fresh else None
    if not str(out or "").startswith("skipped"):  # a SKIP defers, never spends
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = state_path.with_name(state_path.name + ".tmp")
            tmp.write_text(json.dumps({"hash": current_hash}), encoding="utf-8")
            os.replace(tmp, state_path)
        except OSError:
            pass  # best-effort; a lost marker just risks one extra ping later
    return out


# ---------------------------------------------------------------------------
# Orchestration — the one call `folds/reporting.py` makes at the end of a
# maintain run, right beside `health_report()`.
# ---------------------------------------------------------------------------
def generate(core: Any, today: datetime.date | None = None) -> dict[str, Any]:
    """Regenerate both pages + the signed summary, then ping on change.
    Best-effort end to end: the caller wraps this the same way it wraps
    ``health_report`` — a rendering bug must never fail the maintain run."""
    vault = Path(core.vault)
    d = today or datetime.date.today()
    data = collect_exceptions_data(core, d)
    keys = exception_keys(data)
    ceiling = _classification.vm_egress_ceiling()

    mount_html, tokens = render_page(data, full=False, ceiling=ceiling)
    full_html, _empty_map = render_page(data, full=True)

    runtime_dir = _config.brain_runtime_dir(vault)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    mount_path = runtime_dir / MOUNT_FILENAME
    _atomic_write(mount_path, mount_html)

    # The host-only reverse map for the OPAQUE mount tokens (HARDENED:
    # adv-2026-08-20, "OPAQUE IDS") — thrown away and rewritten whole every
    # run, same as the tokens themselves: nothing outside THIS render needs
    # one to stay stable, so there is nothing to merge or accumulate.
    try:
        token_map_path = _rex.exceptions_dir(vault) / TOKEN_MAP_FILENAME
        token_map_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(token_map_path, json.dumps(tokens, indent=1, sort_keys=True))
    except OSError:
        pass  # best-effort; losing the map costs traceability, never safety

    full_dir = _config.proven_off_mount(
        _config.index_dir(vault), vault, what="exceptions full page")
    full_dir.mkdir(parents=True, exist_ok=True)
    full_path = full_dir / FULL_FILENAME
    _atomic_write(full_path, full_html)

    html_bytes = mount_html.encode("utf-8")
    try:
        page_path = str(mount_path.relative_to(vault))
    except ValueError:
        page_path = mount_path.name
    summary = build_json_summary(
        vault, html_bytes=html_bytes, count=len(keys), keys=keys,
        page_path=page_path, generated_at=d, ceiling=ceiling)
    json_path = runtime_dir / JSON_FILENAME
    _atomic_write(json_path, json.dumps(summary, indent=1, sort_keys=True))

    ping = maybe_ping(vault, keys, full_path)
    return {
        "mount_path": str(mount_path), "full_path": str(full_path),
        "json_path": str(json_path), "count": len(keys), "ping": ping,
    }


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
