"""Apply the owner's sheet answers (INT-01, the write half).

The answers ride the marks file the owner already saves off the morning
sheet, land on the consumed-sheets ledger (host-private, the same row that
carries ``feedback_text``), and are applied HERE, on the next night, before
the new questions are drawn. Every action is mechanical and named on the
sheet before the owner answers: an owner-review line, an owner-update
section, a new decision note anchored to its source, a Sources line, or a
commitment event. A skip changes nothing and silences the evidence for
``interview.SUPPRESS_DAYS``.

What was answered is also captured as a ``raw/`` source through the normal
drop-zone ingest, so the interview itself is searchable and citable.
"""
from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from typing import Any

from . import interview as _iv
from .interview_detect import note_path as _note_path

_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_UPDATED_RE = re.compile(r"^updated:.*$", re.M)


# --------------------------------------------------------------------------
# what the owner answered
# --------------------------------------------------------------------------
def pending_answers(vault: Any, state: dict[str, Any]) -> list[dict[str, Any]]:
    """The answers on consumed sheets whose question is still open. The
    newest consumed row wins when a key was answered twice."""
    from .cos.sheet_select import read_consumed  # noqa: PLC0415
    open_keys = {r["key"]: r for r in _iv.open_rows(state)}
    latest: dict[str, dict[str, Any]] = {}
    for row in read_consumed(vault):
        for a in row.get("answers") or []:
            key = str(a.get("key") or "")
            if key in open_keys and str(a.get("action") or ""):
                latest[key] = {"key": key, "action": str(a["action"]),
                               "note": str(a.get("note") or "")[:2000],
                               "sheet_date": str(row.get("sheet_date") or "")}
    return list(latest.values())


# --------------------------------------------------------------------------
# the mechanical edits
# --------------------------------------------------------------------------
def _bump_updated(text: str, today: _dt.date) -> str:
    if text.startswith("---\n") and _UPDATED_RE.search(text):
        return _UPDATED_RE.sub(f'updated: "{today.isoformat()}"', text, count=1)
    return text


def append_section(core: Any, path: str, heading: str, lines: list[str],
                   today: _dt.date, reason: str) -> str:
    """Add ``lines`` under ``heading`` in a brain/ note (creating the heading
    at the end when absent), bump ``updated``, and commit through
    ``write_note`` so the edit is signed and audited like any other."""
    if not path:
        raise ValueError("no note path to edit")
    text = (Path(core.vault) / path).read_text(encoding="utf-8")
    block = "\n".join(lines)
    marker = f"\n{heading}\n"
    if marker in text:
        head, tail = text.split(marker, 1)
        text = f"{head}{marker}{block}\n{tail}"
    else:
        text = text.rstrip("\n") + f"\n\n{heading}\n{block}\n"
    core.write_note(path, _bump_updated(text, today), reason=reason)
    return path


def _slug(text: str, n: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:n].rstrip("-") or "decision"


def write_decision_note(core: Any, row: dict[str, Any], note: str,
                        today: _dt.date) -> str:
    """A decision note from a source that carried decision language, in the
    kernel template's shape, anchored to the source (TMP-05)."""
    src = row["target"]["id"]
    phrase = row["question"].split('"')[1] if row["question"].count('"') >= 2 else ""
    title = (note.strip().splitlines()[0][:120] if note.strip()
             else phrase[:120] or src)
    stem = f"decision-{today.isoformat()}-{_slug(title)}"
    rel = f"brain/resources/{stem}.md"
    cls = "Internal"
    try:
        hit = core.bases_query({"id": src}, k=1)
        cls = str((hit or [{}])[0].get("classification") or cls)
    except Exception:  # noqa: BLE001
        pass
    body = "\n".join([
        "---", f'id: "{stem}"', f'title: "Decision - {title}"', "type: decision",
        f"classification: {cls}", f'created: "{today.isoformat()}"',
        f'updated: "{today.isoformat()}"', "tags: []",
        f'document_date: "{row["evidence"][0].get("date") or today.isoformat()}"',
        f'effective_date: "{today.isoformat()}"', 'context: ""', 'project: ""',
        "stakeholders: []", f'source: "[[{src}]]"', "related: []", "---", "",
        f"# Decision - {title}", "", "## Context", "",
        f"Recorded from [[{src}]], which carries the decision language "
        f"\"{phrase}\"; confirmed by the owner on {today.isoformat()} "
        "(owner interview).", "", "## Decision", "",
        note.strip() or phrase or "See the source.", "", "## Rationale", "",
        "See [[" + src + "]].", "", "## Consequences", "", "", "",
    ])
    core.write_note(rel, body, reason=f"owner interview {today.isoformat()}")
    return rel


def _ev_links(row: dict[str, Any]) -> str:
    return ", ".join(f"[[{e['id']}]] ({e.get('date')})" for e in row["evidence"])


def _apply_tension(core, row, action, note, today, reason):
    path = row["target"]["path"]
    if action == "stands":
        append_section(core, path, "## Owner review", [
            f"- {today}: stands as decided, checked against {_ev_links(row)} "
            "(owner interview)."], today, reason)
        return "owner-review line added: stands"
    if action == "changed":
        body = [f"The owner says this changed after {_ev_links(row)}."]
        if note.strip():
            body.append(note.strip())
        append_section(core, path, f"## Owner update ({today})", body, today, reason)
        return "owner-update section added: changed"
    return None


def _apply_decision(core, row, action, note, today, reason):
    if action == "record":
        rel = write_decision_note(core, row, note, today)
        return f"decision note written: {rel}"
    if action == "no":
        return "not a decision (recorded, never asked again)"
    return None


def _apply_late(core, row, action, note, today, reason):
    cid = row["target"]["id"]
    if action == "done":
        core.cos_spine_record(event="completed", commitment_id=cid, note=reason)
        return "commitment completed"
    if action == "dropped":
        core.cos_spine_record(event="cancelled", commitment_id=cid, note=reason)
        return "commitment cancelled"
    if action == "reschedule":
        m = _DATE_RE.search(note)
        if not m:
            raise ValueError("no YYYY-MM-DD date in the note; left open")
        core.cos_spine_record(event="rescheduled", commitment_id=cid,
                              due=m.group(1), note=reason)
        return f"commitment moved to {m.group(1)}"
    return None


def _apply_orphan(core, row, action, note, today, reason):
    if action.startswith("link:"):
        target_id = action[5:]
        path = _note_path(core, target_id)
        src = row["target"]
        append_section(core, path, "## Sources", [
            f"- [[{src['id']}]] — {src.get('title') or src['id']} "
            f"(linked by the owner, {today})"], today, reason)
        return f"cited from [[{target_id}]]"
    if action == "noise":
        return "noise (never asked again)"
    return None


def _apply_stale(core, row, action, note, today, reason):
    path = row["target"]["path"]
    if action == "current":
        append_section(core, path, "## Owner review", [
            f"- {today}: reviewed by the owner, still current."], today, reason)
        return "owner-review line added: current"
    if action == "outdated":
        body = [note.strip() or "The owner marked this note outdated."]
        append_section(core, path, f"## Owner update ({today})", body, today, reason)
        return "owner-update section added: outdated"
    return None


_APPLY = {"tension": _apply_tension, "decision": _apply_decision,
          "late": _apply_late, "orphan": _apply_orphan, "stale": _apply_stale}


def apply_one(core: Any, row: dict[str, Any], action: str, note: str,
              today: _dt.date) -> str | None:
    """Execute one answer. Returns the applied line, or None for a skip."""
    if action == _iv.SKIP:
        return None
    if action not in {o["action"] for o in row["options"]}:
        raise ValueError("answer is not one of the question's options")
    reason = f"owner interview {today.isoformat()}"
    return _APPLY[row["shape"]](core, row, action, note, today, reason)


# --------------------------------------------------------------------------
# the record — the interview itself becomes a raw/ source
# --------------------------------------------------------------------------
def record_text(entries: list[dict[str, Any]], today: _dt.date) -> str:
    """No frontmatter: the drop-zone ingest prepends its own (type, tier,
    origin) and a second block would corrupt the note."""
    parts = [f"# Owner interview — {today.isoformat()}", "",
             f"Interview: brain-interview · Questions answered: {len(entries)}",
             ""]
    for i, e in enumerate(entries, 1):
        r = e["row"]
        parts += [f"## Q{i} · {r['shape']} · [[{r['target']['id']}]]",
                  f"**Asked:** {r['question']}",
                  f"**Evidence:** {_ev_links(r)}",
                  f"**Answer:** {e['label']}"]
        if e["note"].strip():
            parts.append(f"**Note (verbatim):** {e['note'].strip()}")
        parts += [f"**Applied:** {e['line']}", ""]
    return "\n".join(parts)


def _drop_record(core: Any, text: str, today: _dt.date) -> dict[str, Any]:
    inbox = Path(core.vault) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"owner-interview-{today.isoformat()}.md").write_text(
        text, encoding="utf-8")
    res = core.ingest_dropzone()
    return {k: len(res.get(k) or []) for k in ("ingested", "quarantined")
            if isinstance(res, dict)}


def _settle(core: Any, state: dict[str, Any], row: dict[str, Any], action: str,
            note: str, today: _dt.date) -> dict[str, Any] | None:
    """Apply one answer to one open row and close it in ``state``. Raises when
    the action fails, leaving the row open."""
    line = apply_one(core, row, action, note, today)
    row["answer"], row["note"] = action, note
    row["closed_on"] = today.isoformat()
    if line is None:
        row["status"] = _iv.SKIPPED
        return None
    row["status"] = _iv.ANSWERED
    state["last_answer_on"] = today.isoformat()
    label = next((o["label"] for o in row["options"] if o["action"] == action),
                 action)
    state["applied"].append({
        "date": today.isoformat(), "key": row["key"],
        "line": f"{row['target'].get('title') or row['target']['id']}: {line}"})
    state["applied"] = state["applied"][-20:]
    return {"row": row, "label": label, "note": note, "line": line}


def _finish(core: Any, state: dict[str, Any], applied: list[dict[str, Any]],
            skipped: int, failed: list[str], today: _dt.date) -> dict[str, Any]:
    record: dict[str, Any] = {}
    if applied:
        try:
            record = _drop_record(core, record_text(applied, today), today)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"record: {type(exc).__name__}: {exc}")
    _iv.write_state(core.vault, state)
    return {"date": today.isoformat(), "applied": len(applied),
            "skipped": skipped, "failed": failed, "record": record,
            "lines": [e["line"] for e in applied]}


def apply_answers(core: Any, today: _dt.date | None = None) -> dict[str, Any]:
    """Apply every pending answer, close its question, capture the record.
    A failing action closes nothing: the question stays open and the line
    says why, so the next sheet shows it again rather than losing it."""
    today = today or _dt.date.today()
    state = _iv.read_state(core.vault)
    applied: list[dict[str, Any]] = []
    failed: list[str] = []
    skipped = 0
    rows = {r["key"]: r for r in _iv.open_rows(state)}
    for a in pending_answers(core.vault, state):
        try:
            entry = _settle(core, state, rows[a["key"]], a["action"], a["note"], today)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{a['key']}: {type(exc).__name__}: {exc}")
            continue
        if entry is None:
            skipped += 1
        else:
            applied.append(entry)
    return _finish(core, state, applied, skipped, failed, today)


def apply_answer(core: Any, key: str, action: str, note: str = "",
                 today: _dt.date | None = None) -> dict[str, Any]:
    """One answer given in a session rather than on the sheet (the
    `/brain-grill` fallback and `brain interview --answer`). Same path, same
    record, applied now."""
    today = today or _dt.date.today()
    state = _iv.read_state(core.vault)
    row = next((r for r in _iv.open_rows(state) if r["key"] == key), None)
    if row is None:
        raise ValueError(f"no open interview question with key {key}")
    entry = _settle(core, state, row, action, note, today)
    return _finish(core, state, [entry] if entry else [], 0 if entry else 1, [], today)


__all__ = ["pending_answers", "append_section", "write_decision_note",
           "apply_one", "record_text", "apply_answers", "apply_answer"]
