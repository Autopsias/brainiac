"""PEN 1, ONCE, FOR BOTH TRANSPORTS — a marks payload turned into record rows.

`feedback_cli.record_from_sheet` used to read a published page's embedded state
and file it directly. FB-03 adds a second way for the same marks to arrive: a
plain JSON file the browser downloads, because the page has to work from
`file://` on the owner's own Mac with nothing running. Two arrivals must not
become two implementations of what a mark MEANS, so both now normalise to the
one payload `sheet_marks.marks_from_state` produces and land here.

WHAT A MARK BECOMES, and it is the record's existing two kinds, unchanged:

  * a per-thread mark is a THREAD RULING — keyed on the conversation, never
    deduplicated, never expired. The old three-word verdict is DERIVED from the
    three columns (`sheet_marks.derive_verdict`), so a mark made in the new
    vocabulary and a mark made in the old one are the same row;
  * a suggested rule sentence is a RULE row — deduplicated by `rule_key`,
    capped, expiring at 90 days.

Nothing here invents a third kind, and that is the point: the shape the sheet
asks in changed, the shape the record keeps did not.

FILING IS ONCE PER SHEET. A browser asked to save the same download twice
writes `cos-marks-<date> (1).json`, so a reader keyed on the filename would
file the same marks twice and inflate every counter they touch. The key is the
sheet's own `sheet_id`, and `sheet_select`'s consumed ledger is where it is
remembered.
"""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._layout import _ts, _utcnow
from .feedback import (DO_NOT_TOUCH_VERDICT, WANTED_MORE_VERDICT,
                       FeedbackRowInvalid, read_record, record_rule,
                       record_thread_ruling)
from .feedback_render import held_threads, live_rules
from .sheet_marks import (contradicts, derive_verdict, rule_origin,
                          validate_marks)
from .sheet_select import already_consumed, record_consumed


def _counters(written: dict[str, Any]) -> dict[str, Any]:
    """What a caller needs to see about a rule this filing touched.

    `expires_at` is in here because a confirmation's whole point is that the
    expiry moved; the two counters are here because a revoke decision is made
    on the pair, never on the confirmation count alone.
    """
    return {"rule_key": written["rule_key"],
            "confirmations": written["confirmations"],
            "expires_at": written["expires_at"],
            "fired_count": written["fired_count"],
            "contradicted_count": written["contradicted_count"]}


def _origins(mark: dict[str, Any]) -> list[tuple[str, str, str]]:
    """`(column, value, origin)` for every column this mark ANSWERED.

    `right` and `send-as-is` are agreement and produce no origin: there is no
    correction to make a rule from, and no live rule they can disagree with.
    UNSET produces nothing at all — the whole point of the fourth value.
    """
    category = str(mark.get("category") or "")
    out = []
    for column, value in (("judgment", mark["judgment"]),
                          ("label", mark["label"]),
                          ("draft", mark["draft"])):
        value = str(value or "")
        if not value or value in ("right", "send-as-is"):
            continue
        out.append((column, value, rule_origin(column, value, category)))
    return out


def _counter_updates(vault: Any, origin: str, minted_key: str,
                     common: dict[str, Any], out: dict[str, Any]) -> None:
    """Every LIVE rule this one answer either agrees or disagrees with.

    A rule FIRED on a thread when it came off the same column and the same
    category — that is what `rule_origin` is for, and it is the only join
    available: nothing records which rules the judge actually weighed on which
    thread. So `fired_count` reads as "threads this rule applied to that the
    owner has since answered", which is the honest observable and the one the
    revoke decision needs.

    The value then decides which counter moves. Same value: the owner agreed,
    and it confirms. Different value: `contradicted_count`, and NOT a
    confirmation — counting a disagreement as agreement would push the rule up
    the ranking and out another 90 days.
    """
    # ponytail: re-reads the ledger per answered column. It has to see a rule
    # minted moments ago in this same call, and the record is a few hundred
    # rows against a few dozen marks. Fold the live set into the loop if a
    # ledger ever gets big enough for it to show.
    for live in live_rules(read_record(vault)["rows"]):
        other = str(live.get("rule_origin") or "")
        if not other or str(live["rule_key"]) == minted_key:
            continue
        agrees = other == origin
        if not agrees and not contradicts(origin, other):
            continue
        written = record_rule(vault, rule=str(live["rule"]),
                              rule_origin=other, fired=1,
                              contradicted=0 if agrees else 1,
                              confirm=agrees, **common)
        (out["confirmed"] if agrees else out["contradicted"]).append(
            _counters(written))


def _file_mark(vault: Any, mark: dict[str, Any], *, common: dict[str, Any],
               by_subject: dict[str, list[dict[str, Any]]], held: set[str],
               out: dict[str, Any]) -> None:
    """One mark -> the rows it justifies."""
    cid = str(mark["conversation_id"])
    verdict = derive_verdict(str(mark["action_taken"]), str(mark["judgment"]),
                             str(mark["label"]), str(mark["draft"]))
    row = dict(common, conversation_id=cid,
               subject_sha256=str(mark["subject_sha256"]),
               action_taken=str(mark["action_taken"]), verdict=verdict,
               note=str(mark.get("note") or ""))
    if verdict in (DO_NOT_TOUCH_VERDICT, WANTED_MORE_VERDICT):
        record_thread_ruling(vault, **row)
        out["thread_rulings"] += 1
    elif verdict == "right" and cid in held:
        # THE RELEASE. Unchanged from the single-verdict pen: a `right` on a
        # thread currently under a do-not-touch is the only way back, and a
        # `right` on a thread nobody is holding mints nothing.
        record_thread_ruling(vault, **row)
        held.discard(cid)
        out["released"].append(cid)
    texts = {str(r["column"]): str(r["text"]).strip()
             for r in (mark.get("rules") or [])}
    answered = _origins(mark)
    for column, _value, origin in answered:
        minted = ""
        if texts.get(column):
            written = record_rule(vault, rule=texts[column],
                                  rule_origin=origin, fired=1, **row)
            minted = str(written["rule_key"])
            out["rules"].append(minted)
        _counter_updates(vault, origin, minted, row, out)
    if answered or verdict != "right":
        return
    # THE SUBJECT-DIGEST CONFIRMATION DETECTOR, carried forward unchanged: a
    # row the owner agreed with, with no correction on it, confirms every live
    # rule minted off the same subject digest. The recurring weekly digest came
    # back, the porter archived it again, and the owner agreed again.
    for live in by_subject.get(str(mark["subject_sha256"]), []):
        written = record_rule(vault, rule=str(live["rule"]),
                              rule_origin=str(live.get("rule_origin") or ""),
                              **row)
        out["confirmed"].append(_counters(written))


def record_marks(vault: Any, payload: dict[str, Any], *,
                 labels: tuple[str, ...] | None = None,
                 rows_unmarked: int | None = None,
                 now: _dt.datetime | None = None) -> dict[str, Any]:
    """File one marks payload. THE ONE WRITER both transports go through.

    Refuses, rather than skips, three ways, and each refusal is the same
    principle: a page this reader cannot fully understand must never become a
    partial owner ruling.

    * a payload that fails `validate_marks` — an unknown mark value, a digest
      that does not cover the marks, a key nobody knows;
    * a `sheet_id` this vault has already filed — the browser's second save;
    * a ledger with unreadable rows, because `held_threads` and `live_rules`
      are both read off it and a line nobody can parse silently shrinks both.

    A payload carrying NO marks is not a refusal and not an agreement: it files
    nothing and reports zero, which is what an owner who opened the sheet and
    answered nothing actually did.
    """
    now = now or _utcnow()
    validate_marks(payload, labels=labels)
    seen = already_consumed(vault, str(payload["sheet_id"]))
    if seen is not None:
        raise FeedbackRowInvalid(
            f"sheet {payload['sheet_id']} was already filed at "
            f"{seen.get('consumed_at')} ({seen.get('marks_filed')} row(s), "
            f"transport {seen.get('transport')}) — a second save of the same "
            "sheet is the browser writing the download twice, not a second "
            "set of marks")
    record = read_record(vault)
    if record["unreadable"]:
        raise FeedbackRowInvalid(
            f"the feedback record has {record['unreadable']} unreadable "
            "row(s); a mark filed against a ledger this reader cannot fully "
            "parse would be judged against a shrunken set of live rules")
    by_subject: dict[str, list[dict[str, Any]]] = {}
    for live in live_rules(record["rows"], now=now):
        by_subject.setdefault(
            str(live["thread"]["subject_sha256"]), []).append(live)
    common = {"source": "sheet", "run": str(payload["run"]), "now": now,
              "transport": str(payload["transport"]),
              "sheet_id": str(payload["sheet_id"])}
    out: dict[str, Any] = {
        "path": record["path"], "unreadable": record["unreadable"],
        "sheet_id": str(payload["sheet_id"]), "date": str(payload["date"]),
        "run": str(payload["run"]), "transport": str(payload["transport"]),
        "rows_shown": int(payload["rows_shown"]),
        "threads_marked": len(payload["marks"]),
        "threads_unmarked": (int(rows_unmarked) if rows_unmarked is not None
                             else max(int(payload["rows_shown"])
                                      - len(payload["marks"]), 0)),
        "thread_rulings": 0, "released": [], "rules": [], "confirmed": [],
        "contradicted": [], "revoked": [],
        # READ BACK, AND IT MINTS NOTHING. A rule row is keyed on a thread and
        # a page-wide box names none, so it travels to the run report as the
        # owner's words and stops there.
        "feedback_text": str(payload.get("feedback_text") or ""),
    }
    held = {str(r["conversation_id"]) for r in held_threads(record["rows"])}
    for mark in payload["marks"]:
        _file_mark(vault, mark, common=common, by_subject=by_subject,
                   held=held, out=out)
    for entry in payload["revoked"]:
        record_rule(vault, conversation_id=str(entry["conversation_id"]),
                    subject_sha256=str(entry["subject_sha256"]),
                    action_taken="none", verdict="wrong",
                    rule=str(entry["rule"]), revoked=True, **common)
        out["revoked"].append(str(entry["rule_key"]))
    out["consumed"] = record_consumed(
        vault, payload, ts=_ts(now),
        marks_filed=len(payload["marks"]) + len(payload["revoked"]))
    return out


__all__ = ['record_marks']
