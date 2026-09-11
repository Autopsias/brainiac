"""WHICH ROWS THE OWNER IS ASKED TO LOOK AT, and what his last marks changed.

The last three sheets carried 156, 120 and 118 threads. A hundred and eighteen
rows a night is a wall, and a wall is read once — the sheet was published on
2026-08-27 and used exactly once, for 23 marks.

Two answers here, and they are the two halves of FB-04.

**Selection.** A row earns the owner's attention when a mark on it CHANGES
something. Four rules, unioned, each counted by name so the page can say why a
row is there:

  * every DRAFT — the porter wrote in his voice and nothing else on the page
    can catch a bad one (every draft WRITTEN, not every draft that landed);
  * the HELD-OUT SAMPLE — the only unbiased denominator on the page, and the
    only instrument that can see a thread wrongly archived as noise (which is
    never surfaced, never marked, and would otherwise read zero forever);
  * P0/P1 ARCHIVES — the archives whose cost of being wrong is highest;
  * threads the owner TOUCHED IN OUTLOOK since the last sheet — he has already
    said something about these with his hands.

Everything else is still RENDERED, behind one toggle. Short is measured in rows
he must LOOK AT, not rows the page contains: a row hidden behind a toggle costs
nothing to skip and is one click away when he wants it.

**Effect.** Every sheet opens with what the PREVIOUS sheet's marks changed,
with the denominator IN THE SENTENCE — "of the N rows you were shown" — never
a bare number. When no marks preceded it, it says so rather than printing a
zero, because a zero from "you have never marked anything" and a zero from
"you marked nine things and none of them changed anything" are opposite
findings and must never render the same.

The consumed-marks ledger lives beside the record, HOST-PRIVATE, for the same
reason the record does: it decides whether a marks file is filed at all, and a
file an untrusted leg could delete is a second filing of marks already filed.
"""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._io import _append_jsonl, _read_nofollow
from .feedback import feedback_dir, read_record, sheets_dir

CONSUMED_SCHEMA = "cos-marks-consumed/1"
_CONSUMED_NAME = "sheets-consumed.jsonl"

#: The archive tiers a mistake costs most on. `P0`/`P1` are the judge's own
#: words (`tools/cos_judge_rules.TIERS`); a thread archived at either is one
#: the porter believed was urgent AND disposable, which is the combination
#: most worth a second pair of eyes.
LOUD_TIERS = ("P0", "P1")

_ARCHIVE_ACTIONS = ("archived", "stale-archived")


def sheet_id(date: str, generated_at: str, run_ids: list[str]) -> str:
    """THE SHEET'S OWN IDENTITY — deterministic, and never the filename.

    A browser saving the same download twice writes `cos-marks-<date> (1).json`
    the second time, so a reader keyed on the name would see two different
    files and file the same marks twice. Keyed on the build instead: same date,
    same build stamp, same runs, same id. Rebuilding the night (a newer batch,
    a repaired ledger) produces a NEW id, which is correct — those are
    different sheets and their marks are different marks.
    """
    return sha256_text("\x00".join(
        [str(date), str(generated_at), ",".join(str(r) for r in run_ids)])
    )[:16]


# --------------------------------------------------------------------------
# the consumed-marks ledger
# --------------------------------------------------------------------------
def consumed_path(vault: Any = None) -> Path:
    return feedback_dir(vault) / _CONSUMED_NAME


def read_consumed(vault: Any = None) -> list[dict[str, Any]]:
    """Every marks file this vault has already filed, oldest first.

    An unparseable line is DROPPED here rather than counted, and that is the
    safe direction for this one file: its only job is to say "already filed",
    so a line nobody can read makes the reader do LESS deduplication, never
    more — it can cost a refused second filing, never a silent double one.
    """
    path = consumed_path(vault)
    if not path.exists():
        return []
    out = []
    for line in _read_nofollow(path).decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("schema") == CONSUMED_SCHEMA:
            out.append(row)
    return out


def already_consumed(vault: Any, ident: str) -> dict[str, Any] | None:
    for row in read_consumed(vault):
        if str(row.get("sheet_id")) == str(ident):
            return row
    return None


def record_consumed(vault: Any, payload: dict[str, Any], *, ts: str,
                    marks_filed: int) -> dict[str, Any]:
    row = {
        "schema": CONSUMED_SCHEMA,
        "sheet_id": str(payload["sheet_id"]),
        "sheet_date": str(payload["date"]),
        "transport": str(payload["transport"]),
        "content_sha256": str(payload["content_sha256"]),
        "rows_shown": int(payload["rows_shown"]),
        "marks": len(payload["marks"]),
        "marks_filed": int(marks_filed),
        "consumed_at": str(ts),
        # The page-wide free-text box. It names no thread, so it mints no
        # rule; it reaches the judge as a page note (`render_budget`).
        "feedback_text": str(payload.get("feedback_text") or "")[:600],
        # The owner's answers to the questions the vault asked (INT-01). They
        # name no thread and mint no rule; `interview_apply` reads them off
        # this row on the next night and executes each one.
        "answers": [{"key": str(a["key"]), "action": str(a["action"]),
                     "note": str(a.get("note") or "")[:600]}
                    for a in (payload.get("answers") or [])[:10]],
    }
    _append_jsonl(consumed_path(vault), row, vault=vault)
    return row


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------
def previous_sheet_date(vault: Any, date: str) -> str:
    """The newest sheet built BEFORE this one, by its own filename date."""
    directory = sheets_dir(vault)
    if not directory.exists():
        return ""
    dates = sorted(p.stem for p in directory.glob("*.html")
                   if p.stem < str(date))
    return dates[-1] if dates else ""


def outlook_touched_since(vault: Any, since: str) -> set[str]:
    """Conversations the OWNER moved in Outlook since the last sheet.

    Read off the record's own `source: outlook` thread rulings — pen 2's
    output — rather than re-diffing the mailbox, so "the owner touched this"
    has one producer. `since` is a date; an empty one means every such ruling
    the record holds, which is the honest reading for a first sheet.
    """
    out = set()
    for row in read_record(vault)["rows"]:
        if row.get("kind") != "thread" or row.get("source") != "outlook":
            continue
        if since and str(row.get("ts") or "")[:10] < str(since):
            continue
        out.add(str(row.get("conversation_id") or ""))
    out.discard("")
    return out


def marked_threads(vault: Any, before: str) -> dict[str, str]:
    """conversation id -> the action the owner already ruled on, from every
    sheet FILED before `before`.

    The record row carries the `sheet_id`; the consumed ledger carries that
    sheet's date. Joined exactly as `effect_block` joins them, so the selection
    rule and the effect sentence read one truth.
    """
    earlier = {str(c.get("sheet_id") or "") for c in read_consumed(vault)
               if str(c.get("sheet_date") or "") < str(before)} - {""}
    if not earlier:
        return {}
    out: dict[str, str] = {}
    for row in read_record(vault)["rows"]:
        if row.get("revoked") or str(row.get("sheet_id") or "") not in earlier:
            continue
        thread = row.get("thread") or row
        cid = str(thread.get("conversation_id") or "")
        if cid:
            out[cid] = str(row.get("action_taken") or "")
    return out


def apply_selection(threads: list[dict[str, Any]],
                    touched: set[str],
                    marked: dict[str, str] | None = None) -> dict[str, Any]:
    """Set `shown` on every row and return the counts, BY REASON.

    Mutates the rows because `shown` is part of the frozen state — the saved
    page has to carry which rows it asked about, or `rows_shown` on the marks
    file is a number nobody can check.
    """
    reasons = {"draft": 0, "held_out": 0, "loud_archive": 0, "outlook": 0,
               "already_marked": 0}
    marked = marked or {}
    shown = 0
    for row in threads:
        hit = []
        # DO NOT ASK TWICE (owner, 2026-09-08: "I expect the sheet to be
        # updated so I don't need to evaluate the same emails"). A thread he
        # has already ruled on stays off the sheet — UNLESS the porter did
        # something different to it since, because then the ruling has been
        # tested and he should see the result. `effect_block` measures that
        # same difference; this is the selection side of the same join.
        cid = row["conversation_id"]
        if cid in marked and marked[cid] == row["action_taken"] \
                and cid not in touched:
            row["shown"] = False
            reasons["already_marked"] += 1
            continue
        # A DRAFT, not "a draft that landed". The drafting leg writes the text
        # before the mutation lane saves anything, so a night that wrote a
        # reply and failed to save it still produced a reply in the owner's
        # voice — measured on 2026-09-04: 25 threads carried draft text and 20
        # of them landed. Selecting only the landed ones hid 5 real drafts.
        if row["action_taken"] == "drafted" or row.get("draft_text"):
            hit.append("draft")
        if row["held_out"]:
            hit.append("held_out")
        if row["action_taken"] in _ARCHIVE_ACTIONS and row["tier"] in LOUD_TIERS:
            hit.append("loud_archive")
        if row["conversation_id"] in touched:
            hit.append("outlook")
        row["shown"] = bool(hit)
        shown += bool(hit)
        for reason in hit:
            reasons[reason] += 1
    return {"shown": shown, "total": len(threads), "reasons": reasons}


# --------------------------------------------------------------------------
# effect
# --------------------------------------------------------------------------
_NOTHING = ("You have not read any marks back into the record before this "
            "sheet, so there is nothing to report on yet — this is the first "
            "one.")


def effect_block(vault: Any, threads: list[dict[str, Any]],
                 date: str) -> dict[str, Any]:
    """WHAT THE LAST SHEET'S MARKS CHANGED, and against what denominator.

    The join is by conversation, from the record's own rows: every row carrying
    the previous sheet's `sheet_id` names a thread the owner marked, and
    tonight's run says what the porter did to that thread THIS time. A thread
    the porter has not seen since is reported as exactly that — not as a
    failure and not as a success, because nothing has tested the mark yet.

    STATED CEILING, so nobody reads more into the number than it carries: this
    measures that the porter BEHAVED DIFFERENTLY, not that it behaved
    differently BECAUSE of the mark. A thread can change action for reasons
    that have nothing to do with a ruling. It is the honest observable, and it
    is the one the owner asked for — "show me what my marks did".
    """
    previous = [row for row in read_consumed(vault)
                if str(row.get("sheet_date") or "") < str(date)]
    if not previous:
        return {"previous_sheet_id": "", "previous_date": "", "rows_shown": 0,
                "marks": 0, "ruled": 0, "seen_again": 0, "changed": 0,
                "sentence": _NOTHING}
    prev = previous[-1]
    ident = str(prev.get("sheet_id") or "")
    # HOW MANY HE ANSWERED comes from the CONSUMED LEDGER, not from counting
    # record rows, because those two numbers are not the same number and the
    # ledger's is the true one. A mark of `right` on a thread nobody is holding
    # is a real answer that mints NO row, and a revoke tick mints a row against
    # a thread he never marked. Counting rows reported both wrong at once:
    # measured on the 2026-09-04 sheet, 3 answered rows counted as 3 only by
    # coincidence — an agreement dropped out and a revoked rule's thread
    # dropped in.
    ruled: dict[str, str] = {}
    for row in read_record(vault)["rows"]:
        if str(row.get("sheet_id") or "") != ident or row.get("revoked"):
            continue
        thread = row.get("thread") or row
        cid = str(thread.get("conversation_id") or "")
        if cid:
            ruled[cid] = str(row.get("action_taken") or "")
    tonight = {str(row["conversation_id"]): str(row["action_taken"])
               for row in threads}
    seen = {cid: was for cid, was in ruled.items() if cid in tonight}
    changed = sum(1 for cid, was in seen.items() if tonight[cid] != was)
    shown = int(prev.get("rows_shown") or 0)
    marks = int(prev.get("marks") or 0)
    return {
        "previous_sheet_id": ident,
        "previous_date": str(prev.get("sheet_date") or ""),
        "rows_shown": shown, "marks": marks, "ruled": len(ruled),
        "seen_again": len(seen), "changed": changed,
        "sentence": (
            f"Of the {shown} rows you were shown on "
            f"{prev.get('sheet_date')}, you marked {marks}. "
            f"{len(ruled)} of those marks left a standing ruling; "
            f"{len(seen)} of those threads came back on a later night and the "
            f"porter did something different on {changed} of them. The "
            f"remaining {len(ruled) - len(seen)} have not been seen since, so "
            "nothing has tested those marks yet."),
    }


__all__ = ['CONSUMED_SCHEMA', 'LOUD_TIERS', 'sheet_id', 'consumed_path',
           'read_consumed', 'already_consumed', 'record_consumed',
           'previous_sheet_date', 'outlook_touched_since', 'apply_selection',
           'effect_block']
