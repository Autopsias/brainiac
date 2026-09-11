"""Owner interview lane (INT-01): the vault asks the owner what it cannot settle.

The folds already know when the vault is in doubt: a decision with newer
sources against it, a source with decision language and no decision note, a
commitment past its date, a source nothing cites, a central note gone stale.
Until 2026-09-09 those doubts landed in ``hot.md`` as chores ("review by
hand"), which the owner ruled nobody will read (2026-07-13). This lane turns
each one into ONE decidable question with enumerated options and a default,
puts it on the morning sheet beside the verdict marks, and applies the answer
on the next night through the audited write path (``interview_apply``).

State: ``<vault>/.brain/interview/state.json`` — every question ever asked,
its status, and the last applied lines. Host-only, never indexed.

The budget is what keeps this from becoming a nag: at most ``MAX_PER_DAY`` new
questions a day and ``MAX_OPEN`` open at once; a question expires after
``EXPIRE_DAYS``; the same evidence is never asked twice; a skip or an expiry
silences that evidence for ``SUPPRESS_DAYS``; and when nothing was answered
for ``QUIET_AFTER_DAYS`` the lane drops to one question a day.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

SCHEMA = "brain-interview/1"
STATE_RELPATH = ".brain/interview/state.json"
MAX_PER_DAY = 3
MAX_OPEN = 5
EXPIRE_DAYS = 14
SUPPRESS_DAYS = 30
QUIET_AFTER_DAYS = 7
KEEP_CLOSED_DAYS = 90
APPLIED_SHOWN = 5

SHAPES = ("tension", "decision", "late", "orphan", "stale")
SKIP = "skip"
#: Per shape, the fixed actions the applier knows how to execute. The model
#: phrasing leg may reword a LABEL, never an action.
OPTIONS: dict[str, list[tuple[str, str]]] = {
    "tension": [("stands", "It stands as decided"),
                ("changed", "The newer source changed it — say how below"),
                (SKIP, "Skip for now")],
    "decision": [("record", "Yes, record it as a decision"),
                 ("no", "Not a decision"),
                 (SKIP, "Skip for now")],
    "late": [("done", "Done"), ("dropped", "Dropped"),
             ("reschedule", "New date — write it below (YYYY-MM-DD)"),
             (SKIP, "Skip for now")],
    "orphan": [("noise", "Noise — ignore it"), (SKIP, "Skip for now")],
    "stale": [("current", "Still current"),
              ("outdated", "Outdated — say what changed below"),
              (SKIP, "Skip for now")],
}
ROW_KEYS = frozenset({
    "key", "shape", "asked_on", "expires_on", "question", "evidence",
    "options", "default", "target", "change", "answer", "note", "status",
})
OPEN, ANSWERED, EXPIRED, SKIPPED = "open", "answered", "expired", "skipped"
_KEY_RE = re.compile(r"^iq-[0-9a-f]{12}$")
_ACTION_RE = re.compile(r"^[a-z]+(:[A-Za-z0-9._-]{1,120})?$")


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------
def state_path(vault: Any) -> Path:
    return Path(vault) / STATE_RELPATH


def empty_state() -> dict[str, Any]:
    return {"schema": SCHEMA, "generated": "", "rows": [], "applied": [],
            "last_answer_on": ""}


def read_state(vault: Any) -> dict[str, Any]:
    p = state_path(vault)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_state()
    if not isinstance(d, dict) or d.get("schema") != SCHEMA:
        return empty_state()
    base = empty_state()
    base.update({k: d.get(k, v) for k, v in base.items()})
    return base


def write_state(vault: Any, state: dict[str, Any]) -> Path:
    p = state_path(vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True,
                              indent=1) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return p


def question_key(shape: str, evidence_ids: list[str]) -> str:
    body = shape + "\n" + "\n".join(sorted(evidence_ids))
    return "iq-" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def _date(s: str) -> _dt.date | None:
    try:
        return _dt.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def open_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in state["rows"] if r.get("status") == OPEN]


def expire(state: dict[str, Any], today: _dt.date) -> int:
    """Close open questions past their date; drop closed rows older than
    ``KEEP_CLOSED_DAYS`` so the file stays a queue, not an archive."""
    closed = 0
    for r in state["rows"]:
        exp = _date(r.get("expires_on", ""))
        if r.get("status") == OPEN and exp and exp < today:
            r["status"] = EXPIRED
            r["closed_on"] = today.isoformat()
            closed += 1
    keep = []
    for r in state["rows"]:
        closed_on = _date(r.get("closed_on", ""))
        if (r.get("status") in (EXPIRED, SKIPPED) and closed_on
                and (today - closed_on).days > KEEP_CLOSED_DAYS):
            continue
        keep.append(r)
    state["rows"] = keep
    return closed


def blocked(state: dict[str, Any], key: str, target_id: str,
            today: _dt.date) -> bool:
    """The same evidence is never asked twice; a skip or an expiry silences
    it for ``SUPPRESS_DAYS``; one open question per target note."""
    for r in state["rows"]:
        if r.get("key") == key:
            if r.get("status") in (OPEN, ANSWERED):
                return True
            closed_on = _date(r.get("closed_on", ""))
            if closed_on and (today - closed_on).days < SUPPRESS_DAYS:
                return True
        if (r.get("status") == OPEN and target_id
                and (r.get("target") or {}).get("id") == target_id):
            return True
    return False


def day_budget(state: dict[str, Any], today: _dt.date) -> dict[str, Any]:
    """How many questions may be asked today, and why."""
    rows = state["rows"]
    open_now = len(open_rows(state))
    asked_today = sum(1 for r in rows if r.get("asked_on") == today.isoformat())
    last = _date(state.get("last_answer_on", ""))
    oldest = min((d for d in (_date(r.get("asked_on", "")) for r in rows)
                  if d), default=None)
    quiet = bool(oldest and (today - oldest).days >= QUIET_AFTER_DAYS
                 and (not last or (today - last).days >= QUIET_AFTER_DAYS))
    per_day = 1 if quiet else MAX_PER_DAY
    room = max(0, min(per_day - asked_today, MAX_OPEN - open_now))
    return {"room": room, "open": open_now, "asked_today": asked_today,
            "quiet": quiet, "per_day": per_day}


def generate(core: Any, today: _dt.date | None = None) -> dict[str, Any]:
    """Expire, then ask up to the day's budget, round-robin across shapes so
    one noisy detector never crowds the others out. Every detector failure is
    a host-authored line in ``errors``, never an exception: this runs inside
    the nightly and nothing here may kill the night."""
    today = today or _dt.date.today()
    state = read_state(core.vault)
    expired = expire(state, today)
    budget = day_budget(state, today)
    asked: list[dict[str, Any]] = []
    errors: list[str] = []
    from .interview_detect import DETECTORS                  # noqa: PLC0415
    gens: dict[str, Any] = {}
    for shape in SHAPES:
        try:
            gens[shape] = DETECTORS[shape](core, state, today)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{shape}: {type(exc).__name__}")
    while len(asked) < budget["room"] and gens:
        for shape in list(gens):
            if len(asked) >= budget["room"]:
                break
            try:
                row = next(gens[shape])
            except StopIteration:
                del gens[shape]
                continue
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{shape}: {type(exc).__name__}")
                del gens[shape]
                continue
            if blocked(state, row["key"], row["target"]["id"], today):
                continue
            state["rows"].append(row)
            asked.append(row)
    state["generated"] = today.isoformat()
    write_state(core.vault, state)
    return {"date": today.isoformat(), "asked": len(asked),
            "keys": [r["key"] for r in asked], "expired": expired,
            "budget": budget, "errors": errors}


# --------------------------------------------------------------------------
# phrasing — one small model leg rewords the questions; the actions stay fixed
# --------------------------------------------------------------------------
def phrase_prompt(vault: Any, rows: list[dict[str, Any]]) -> str:
    """Host-authored prompt for the phrasing leg: the evidence, an excerpt
    of the note in doubt, the current wording, the fixed option actions."""
    parts = [
        "You phrase questions a personal knowledge vault asks its owner on a",
        "morning sheet. For each question below, rewrite QUESTION as at most",
        "two plain sentences a busy person can answer at a glance: name the",
        "note, the date, and exactly what is in doubt. Keep every [[note-id]]",
        "exactly as given. Reword each option LABEL to fit the question, at",
        "most 12 words, keeping its action code and its meaning. Never add,",
        "drop or reorder options. Never state a fact the evidence does not",
        "show. Answer with one JSON object per question, one per line, and",
        "nothing else. Put the question's key in the field named",
        "conversation_id (the reader admits only objects carrying it):",
        '{"conversation_id": "<key>", "question": "...", "options": '
        '[{"action": "...", "label": "..."}]}', "",
    ]
    for i, r in enumerate(rows, 1):
        t = r["target"]
        parts.append(f"=== Q{i} key={r['key']} shape={r['shape']}")
        parts.append(f"target: [[{t['id']}]] — {t.get('title') or t['id']}")
        parts.append("evidence: " + "; ".join(
            f"[[{e['id']}]] ({e.get('date')}) {e.get('title') or ''}".strip()
            for e in r["evidence"]))
        from .interview_detect import note_excerpt               # noqa: PLC0415
        excerpt = note_excerpt(vault, t.get("path") or "")
        if excerpt:
            parts.append(f"excerpt of the target note: {excerpt}")
        parts.append(f"current question: {r['question']}")
        parts.append("options: " + "; ".join(
            f"action={o['action']} label={o['label']}" for o in r["options"]))
        parts.append("")
    return "\n".join(parts)


def apply_phrasing(vault: Any, answers: list[Any]) -> dict[str, Any]:
    """Take the leg's rewording into the open rows — only where it kept every
    action, every note id, and a sane length. Anything else leaves the host
    wording in place; the sheet never waits on the model."""
    state = read_state(vault)
    by_key = {r["key"]: r for r in open_rows(state)}
    updated: list[str] = []
    rejected: list[str] = []
    for a in answers:
        if not isinstance(a, dict):
            continue
        # `conversation_id` is the one field the hardened envelope reader
        # admits an object on; the phrasing leg carries the question key there.
        key = a.get("key") or a.get("conversation_id")
        if key not in by_key:
            continue
        row = by_key[key]
        q = str(a.get("question") or "").strip()
        opts = a.get("options")
        ids = {e["id"] for e in row["evidence"]} | {row["target"]["id"]}
        # The leg may name a note by its title instead of its id — the
        # evidence line under the question carries the ids anyway — but it may
        # not cite an id the evidence does not hold.
        cited = set(re.findall(r"\[\[([^\]]+)\]\]", q))
        ok = (0 < len(q) <= 400 and cited <= ids
              and isinstance(opts, list)
              and [o.get("action") for o in opts if isinstance(o, dict)]
              == [o["action"] for o in row["options"]]
              and all(isinstance(o.get("label"), str)
                      and 0 < len(o["label"].strip()) <= 120 for o in opts))
        if not ok:
            rejected.append(row["key"])
            continue
        row["question"] = q
        for o, new in zip(row["options"], opts):
            o["label"] = new["label"].strip()
        updated.append(row["key"])
    if updated:
        write_state(vault, state)
    return {"phrased": updated, "kept_host_wording": rejected}


# --------------------------------------------------------------------------
# the sheet's view
# --------------------------------------------------------------------------
def sheet_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [{k: r.get(k, "") for k in ROW_KEYS} for r in open_rows(state)]


def sheet_block(vault: Any) -> dict[str, Any]:
    """What the morning sheet renders: the open questions, and the lines the
    last answers produced. Absent state renders as nothing to ask."""
    state = read_state(vault)
    return {"rows": sheet_rows(state),
            "applied": list(state.get("applied") or [])[-APPLIED_SHOWN:],
            "quiet": bool(day_budget(state, _dt.date.today())["quiet"])}


def validate_rows(rows: Any) -> None:
    """The closed shape the sheet state carries (and refuses on the way back)."""
    if not isinstance(rows, list):
        raise ValueError("questions.rows must be a list")
    for i, r in enumerate(rows):
        if not isinstance(r, dict) or set(r) != ROW_KEYS:
            raise ValueError(f"questions.rows[{i}] must carry exactly "
                             f"{sorted(ROW_KEYS)}")
        if not _KEY_RE.fullmatch(str(r["key"])):
            raise ValueError(f"questions.rows[{i}].key is not an interview key")
        if r["shape"] not in SHAPES or not str(r["question"]).strip():
            raise ValueError(f"questions.rows[{i}] has no shape or no question")
        opts = r["options"]
        if (not isinstance(opts, list) or len(opts) < 2
                or any(set(o) != {"action", "label"}
                       or not _ACTION_RE.fullmatch(str(o["action"]))
                       for o in opts)):
            raise ValueError(f"questions.rows[{i}].options must be >= 2 "
                             "{action, label} pairs")
        if r["default"] not in {o["action"] for o in opts}:
            raise ValueError(f"questions.rows[{i}].default is not an option")
        if not isinstance(r["answer"], str) or not isinstance(r["note"], str):
            raise ValueError(f"questions.rows[{i}] answer and note must be "
                             "strings, empty rather than absent")


__all__ = ["SCHEMA", "STATE_RELPATH", "MAX_PER_DAY", "MAX_OPEN", "EXPIRE_DAYS",
           "SUPPRESS_DAYS", "QUIET_AFTER_DAYS", "SHAPES", "SKIP", "OPTIONS",
           "ROW_KEYS", "OPEN", "ANSWERED", "EXPIRED", "SKIPPED", "state_path",
           "read_state", "write_state", "question_key", "open_rows", "expire",
           "blocked", "day_budget", "generate", "phrase_prompt",
           "apply_phrasing", "sheet_rows", "sheet_block", "validate_rows"]
