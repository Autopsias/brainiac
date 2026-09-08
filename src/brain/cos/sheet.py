"""Build the durable, self-contained COS morning sheet (SHEET-01).

The sheet is a view over records other legs already wrote.  It never infers a
mailbox mutation from a model verdict: landed actions come from the undo
ledgers, signed ingestion comes from the ingestion join used by the mutation
planner, subjects come from the capture corpus, and the door/stop blocks are
copied from the batch ledger's named fields.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import random
from importlib import resources
from pathlib import Path
from typing import Any

from .. import cos, cos_corpus
from ..cos_echecks import STALE_ACT_SIGNAL

from .feedback import (
    ACTION_FOR_VERB,
    SHEET_STATE_ELEMENT_ID,
    SHEET_STATE_SCHEMA,
    read_record,
    sheets_dir,
    subject_digest,
    thread_digest,
)
from .feedback_cli import (
    applied_mutations,
    sheet_state_from_html,
    overturned_last_time,
)
from .feedback_render import ranked_rules, render_budget
from .feedback_sheet import BATCH_LEDGER_SCHEMA, HELD_OUT_K, validate_sheet_state
from .sheet_marks import RULE_TEMPLATES
from .sheet_render import render_html  # noqa: F401  re-exported
from .sheet_select import (
    apply_selection,
    effect_block,
    outlook_touched_since,
    previous_sheet_date,
    sheet_id as _sheet_id,
)
from .sheet_threads import (
    # `HELD_BECAUSE` is RE-EXPORTED, not used here. It was defined in this
    # module until the FB-03 split moved the thread block to `sheet_threads`,
    # and it is the plain-words map a hold reason reaches the owner through —
    # `tests/test_cos_ground_ledger.py` reads it from this name to prove a new
    # `held_reason` is not left as a bare slug. Dropping the name would have
    # made that check pass by disappearing.
    HELD_BECAUSE,
    _category_legend,
    _thread_state,
    draft_texts,
    label_vocabulary,
)

_BATCH_LEDGER = "_cos_batch_ledger.jsonl"


def _checked_sheet_date(value: Any) -> str:
    """Return one exact ISO date, safe to use as the sheet filename."""
    text = str(value or "")
    try:
        parsed = dt.date.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is None or parsed.isoformat() != text:
        raise ValueError(f"sheet date must be YYYY-MM-DD, got {text!r}")
    return text


def _read_jsonl_strict(path: Path, *, required: bool) -> list[dict[str, Any]]:
    """Read one mount-side JSONL strictly and without following a leaf symlink."""
    if not path.exists() and not path.is_symlink():
        if required:
            raise ValueError(f"required sheet input is missing: {path}") from None
        return []
    raw = cos._read_nofollow(path).decode("utf-8")
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"{path.name}:{number} is unreadable: {exc}") from None
        if not isinstance(row, dict):
            raise ValueError(f"{path.name}:{number} is not an object")
        rows.append(row)
    return rows


def _batch_for_date(vault: Path, date: str | None) -> tuple[str, dict[str, Any]]:
    path = cos.run_ops_dir(vault) / _BATCH_LEDGER
    rows = _read_jsonl_strict(path, required=True)
    valid = [r for r in rows if r.get("schema") == BATCH_LEDGER_SCHEMA]
    if not valid:
        raise ValueError(f"{path.name} has no {BATCH_LEDGER_SCHEMA} row")

    def row_date(row: dict[str, Any]) -> str:
        runs = row.get("run_ids") or []
        return (
            str(runs[0])[:10]
            if runs
            else str(row.get("finished_at") or row.get("started_at") or "")[:10]
        )

    # This value becomes `<sheets>/<date>.html`. The default is still tainted:
    # `started_at` and `run_ids` live in the mount-resident batch ledger.
    wanted = _checked_sheet_date(date or row_date(valid[-1]))
    matches = [row for row in valid if row_date(row) == wanted]
    if not matches:
        raise ValueError(f"no batch-ledger row for {wanted}")
    return wanted, matches[-1]


def _run_rows(vault: Path, run_ids: list[str]) -> list[dict[str, Any]]:
    """Newest occurrence of each judged conversation across the batch's runs."""
    latest: dict[str, dict[str, Any]] = {}
    ops = cos.run_ops_dir(vault)
    for run_id in run_ids:
        run_id = cos.checked_run_id(run_id)
        path = ops / f"_cos_ingestion_ledger_{run_id}.jsonl"
        for row in _read_jsonl_strict(path, required=True):
            cid = str(row.get("conversation_id") or "")
            if not cid:
                raise ValueError(f"{path.name} carries a row without conversation_id")
            latest[cid] = row
    return list(latest.values())


def _subjects(vault: Path, run_ids: list[str]) -> dict[str, str]:
    """Subjects from corpus provenance only; message bodies are never read."""
    out: dict[str, str] = {}
    for run_id in run_ids:
        run_id = cos.checked_run_id(run_id)
        for row in cos_corpus.read_corpus(vault, run_id):
            cid = str(row.get("conversation_id") or "")
            subject = str((row.get("provenance") or {}).get("subject") or "").strip()
            if cid and subject:
                out[cid] = subject
    return out


def _landed(vault: Path, run_ids: list[str]) -> dict[tuple[str, str], dict[str, Any]]:
    """Strictly read rows, then delegate the fact to its canonical producer."""
    rows: list[dict[str, Any]] = []
    ops = cos.run_ops_dir(vault)
    for run_id in run_ids:
        run_id = cos.checked_run_id(run_id)
        path = ops / f"_cos_undo_ledger_{run_id}.jsonl"
        for row in _read_jsonl_strict(path, required=False):
            cid, verb = (
                str(row.get("conversation_id") or ""),
                str(row.get("verb") or ""),
            )
            if not cid or not verb:
                raise ValueError(f"{path.name} carries an unkeyed mutation row")
            rows.append(row)
    return applied_mutations(rows, verbs=tuple(ACTION_FOR_VERB))


def _overturn_state(vault: Path, date: str) -> list[dict[str, Any]]:
    """Project S05's canonical previous-overturn fold into sheet display rows."""
    return [
        {
            k: row.get(k)
            for k in (
                "conversation_id_digest",
                "subject_sha256",
                "action_taken",
                "verdict",
                "note",
                "ts",
            )
        }
        for row in overturned_last_time(vault, before=date)
    ]


def _standing_rulings(vault: Path, *,
                      now: dt.datetime | None) -> tuple[dict, list[dict]]:
    """EVERY LIVE RULE, with the counters the revoke decision is made on.

    `render_budget` applies the same cap the judge's prompt does, so
    `in_prompt` is that cap's OWN answer rather than a second count that can
    disagree with it. The sheet still lists every live rule — one outside the
    cap needs its revoke control just as much — and says of each whether it is
    driving anything tonight.
    """
    feedback = read_record(vault)
    if feedback["unreadable"]:
        raise ValueError(
            "feedback record has unreadable rows; every live ruling "
            "cannot be rendered safely"
        )
    budget = render_budget(vault, now=now)
    in_prompt = {str(row["rule_key"]) for row in budget["rules"]["rendered"]}
    all_rules = ranked_rules(
        feedback["rows"], now=now, cap=max(len(feedback["rows"]), 1)
    )["rendered"]
    return budget, [
        {
            "rule": str(row["rule"]),
            "rule_key": str(row["rule_key"]),
            "confirmations": int(row.get("confirmations") or 0),
            "fired_count": int(row.get("fired_count") or 0),
            "contradicted_count": int(row.get("contradicted_count") or 0),
            "in_prompt": str(row["rule_key"]) in in_prompt,
            "expires_at": str(row["expires_at"]),
            "revoke": False,
            "conversation_id": str(row["thread"]["conversation_id"]),
            "subject_sha256": str(row["thread"]["subject_sha256"]),
        }
        for row in all_rules
    ]


def build_state(
    vault: Path,
    *,
    date: str | None = None,
    now: dt.datetime | None = None,
    rng: random.Random | random.SystemRandom | None = None,
) -> dict[str, Any]:
    """Assemble and validate the frozen sheet state from its real producers."""
    sheet_date, batch = _batch_for_date(vault, date)
    run_ids = [str(run) for run in (batch.get("run_ids") or [])]
    rows = _run_rows(vault, run_ids)
    subjects = _subjects(vault, run_ids)
    landed = _landed(vault, run_ids)
    archived = {cid for cid, verb in landed if verb == "archive"}
    drafted = {cid for cid, verb in landed if verb == "draft"}
    signed = (
        cos.signed_ingested_catching_up(vault, run_ids[-1], rows) if run_ids else set()
    )
    stale = {
        str(row["conversation_id"])
        for row in rows
        if row.get("noise_signal") == STALE_ACT_SIGNAL
    } & archived
    held = {
        str(row["conversation_id"]) for row in rows if row.get("disposition") == "held"
    }
    held_population = sorted(archived - drafted)
    sampler = rng or random.SystemRandom()
    held_out = sorted(
        sampler.sample(held_population, min(HELD_OUT_K, len(held_population)))
    )

    thread_state = _thread_state(
        rows, subjects, landed, signed, stale, held, held_out,
        # THE FILE LANE'S OWN AUTHORITY, separate from the text lane's
        # `signed` above. The owner asked on 2026-09-07 to see the two
        # apart, and they genuinely differ: a thread can carry a signed
        # note for its BODY while its attachment never reached one.
        files_signed=(cos.signed_attachment_conversations(vault, run_ids[-1])
                      if run_ids else set()),
        drafts=draft_texts(vault, run_ids),
    )
    selection = apply_selection(
        thread_state,
        outlook_touched_since(vault, previous_sheet_date(vault, sheet_date)),
    )

    budget, rulings = _standing_rulings(vault, now=now)
    stamp = now or dt.datetime.now(dt.timezone.utc)
    generated_at = stamp.isoformat().replace("+00:00", "Z")
    state = {
        "schema": SHEET_STATE_SCHEMA,
        "date": sheet_date,
        "generated_at": generated_at,
        "sheet_id": _sheet_id(sheet_date, generated_at, run_ids),
        "run_ids": run_ids,
        "counts": {
            "total": len(rows),
            "archived": len(archived),
            "ingested": len(signed),
            "drafted": len(drafted),
            "held": len(held),
            "stale_archived": len(stale),
        },
        "threads": thread_state,
        "held_out": {
            "k": HELD_OUT_K,
            "conversation_ids": held_out,
            "population": len(held_population),
        },
        "stale_archived": sorted(stale),
        "standing_rulings": rulings,
        "overturned_last_time": _overturn_state(vault, sheet_date),
        "excluded": {
            "rules": budget["rules"]["excluded"],
            "thread_rulings": (
                budget["thread_rulings"]["excluded"]
                + budget["thread_rulings"]["wanted_more_excluded"]
            ),
        },
        "door_check": dict(batch.get("door_check") or {}),
        "batch_stop": dict(batch.get("batch_stop") or {}),
        "feedback_text": "",
        "category_legend": _category_legend(vault, thread_state),
        "label_vocabulary": label_vocabulary(vault, thread_state),
        "rule_templates": dict(RULE_TEMPLATES),
        "selection": selection,
        "effect": effect_block(vault, thread_state, sheet_date),
    }
    return validate_sheet_state(state)


#: Each verdict's plain meaning, on the control itself. The three words are
#: meaningless without them: `wrong` is not "bad call", it is a permanent
#: do-not-touch on this one thread, and `missed` asks for MORE rather than
#: less. Stated where the owner clicks, not only in a panel further up.
def write_sheet(
    vault: Path,
    *,
    date: str | None = None,
    now: dt.datetime | None = None,
    rng: random.Random | random.SystemRandom | None = None,
) -> dict[str, Any]:
    state = build_state(vault, date=date, now=now, rng=rng)
    page = render_html(state)
    path = sheets_dir(vault) / f"{state['date']}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    cos._write_atomic(path, page.encode("utf-8"), mode=0o600)
    write_today_pointer(vault, path, threads=len(state.get("threads") or []))
    return _sheet_result(path, state)


def today_pointer_path(vault: Any) -> Path:
    """`<vault>/.brain/cos/today.html` — the ONE address that never changes.

    DELIBERATELY BESIDE `sheets/`, NOT INSIDE IT. Every sheet in that directory
    is named for its own date and `sheet_select.previous_sheet_date` globs
    `*.html` there and compares stems as dates; a file called `today` in that
    glob is a filename waiting to be mistaken for one. One directory up it
    collides with nothing.
    """
    from .feedback import sheets_dir as _sd                      # noqa: PLC0415
    return _sd(vault).parent / "today.html"


def write_today_pointer(vault: Any, sheet: Path, *, threads: int = 0) -> Path:
    """Point the stable address at the sheet just written.

    WHY THIS EXISTS. Every sheet is named for its date, so there has never been
    anything to bookmark: the owner had to find `2026-09-06.html` today and
    `2026-09-07.html` tomorrow. The marks on that page are the only evidence
    that makes the judge less conservative — measured 2026-09-06, it proposed
    archiving 2 of 20 `read` threads — so the cost of finding it is the cost of
    the whole feedback loop.

    A REAL FILE, NOT A SYMLINK. `publishable_sheet` refuses a symlinked page
    and the sheets are read with `_read_nofollow`, because a writable link in a
    mount-visible directory is a way to serve one page while a reader believes
    it validated another. This writes an ordinary redirect page instead: it
    carries no thread text of its own, so it never becomes a second, unaudited
    copy of the mail.

    Best-effort by construction: a sheet that was written is the deliverable,
    and failing to write a convenience pointer must never fail the night.
    """
    target = today_pointer_path(vault)
    try:
        href = html.escape(sheet.name, quote=True)
        count = f"{threads} thread(s)" if threads else "your sheet"
        body = (
            "<!doctype html>\n<html><head><meta charset=\"utf-8\">"
            f"<meta http-equiv=\"refresh\" content=\"0; url=sheets/{href}\">"
            "<title>Brainiac — today's sheet</title></head>"
            f"<body><p>Opening {html.escape(count)} — "
            f"<a href=\"sheets/{href}\">sheets/{href}</a></p></body></html>\n"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        cos._write_atomic(target, body.encode("utf-8"), mode=0o600)
    except OSError:
        pass
    return target


def _sheet_result(path: Path, state: dict[str, Any]) -> dict[str, Any]:
    """One path handoff for the nightly and the attended mail/Artifact sessions."""
    return {
        "path": str(path),
        "mail_summary_path": str(path),
        "state": state,
        "artifact": {"capabilities": {"artifact": {}}},
    }


def publishable_sheet(vault: Path, *, date: str | None = None) -> dict[str, Any]:
    """Return the nightly's exact sheet, building it when none exists.

    The attended ``--publish`` handoff reuses a validated page so its random
    held-out sample cannot drift. A newer same-day batch is rebuilt; a damaged
    or symlinked page refuses rather than being silently blessed.
    """
    sheet_date, batch = _batch_for_date(vault, date)
    path = sheets_dir(vault) / f"{sheet_date}.html"
    if not path.exists() and not path.is_symlink():
        return write_sheet(vault, date=sheet_date)
    page = cos._read_nofollow(path).decode("utf-8")
    state = sheet_state_from_html(page)
    expected_runs = [str(run) for run in (batch.get("run_ids") or [])]
    if (
        state.get("date") != sheet_date
        or state.get("run_ids") != expected_runs
        or state.get("door_check") != dict(batch.get("door_check") or {})
        or state.get("batch_stop") != dict(batch.get("batch_stop") or {})
    ):
        return write_sheet(vault, date=sheet_date)
    return _sheet_result(path, state)


__all__ = ["build_state", "render_html", "write_sheet", "publishable_sheet", "HELD_BECAUSE"]
