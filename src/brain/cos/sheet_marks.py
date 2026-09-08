"""THE THREE MARKS, and the file the owner's browser saves (FB-03).

The morning sheet asked ONE question — "was this the right call?" — with three
possible answers, and it judged only the ACTION. It could not say *right
archive, wrong label* or *right to draft, but that draft is too long*, so an
owner who wanted to say either had one word available and picked whichever was
least wrong. Three independent closed questions replace it:

    JUDGMENT   right | should-be <act|read|noise> at <P0..P3>
    LABEL      right | should-be <a category from the owner's own taxonomy>
    DRAFT      send-as-is | needs <tone|facts|too-long|missing-point>

plus one optional NOTE. **UNSET is a first-class fourth value on every one of
them, and it is never filed.** An unmarked row is UNKNOWN — not agreement and
not disagreement — so the saved file carries only the rows the owner actually
marked, alongside the count of rows he was SHOWN, which is the denominator any
later rate needs. Silence is never promoted into a confirmation.

THE SHAPE IS BORROWED, THE TOOLS ARE NOT. Argilla and Label Studio both settled
the same problem — a human correcting a model's output at scale — on the same
four-part shape: a RECORD, a closed QUESTION per column, the model's own output
as a SUGGESTION, and the human's answer as a RESPONSE. Neither is adoptable
here (both want a running server, and this page has to open from `file://` on
one Mac with nothing running), but the shape is exactly right and is what these
vocabularies implement: `action_taken` + `reason` is the record, the three
columns are the questions, the porter's verdict is the suggestion shown as
TEXT, and the owner's mark is the response.

The old three-word verdict is DERIVED from these columns rather than replaced,
so the record stays ONE record: `docs/cos-feedback-record.md` §10.
"""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from .feedback_rows import ACTIONS, FeedbackRowInvalid, _SHA256_RE

#: The judge's own triage buckets and tiers, spelled the way
#: `tools/cos_judge_rules.py` spells them. A fourth bucket added there and not
#: here would reach this page as an unmarkable row, so the test asserts the
#: equality against that module rather than trusting this comment.
BUCKETS = ("act", "read", "noise")
TIERS = ("P0", "P1", "P2", "P3")

#: JUDGMENT: `right`, or `<bucket>:<tier>` — twelve should-be values plus one.
#: Rendered as a `<select>`, not twelve radios: thirteen radios per row over a
#: hundred rows is the wall this session exists to remove.
JUDGMENT_VALUES = ("right",) + tuple(
    f"{bucket}:{tier}" for bucket in BUCKETS for tier in TIERS)

#: DRAFT: what is wrong with the reply the porter wrote, in four words the
#: drafting prompt can act on. `send-as-is` is the explicit agreement — it is a
#: MARK, not the absence of one.
DRAFT_VALUES = ("send-as-is", "tone", "facts", "too-long", "missing-point")

#: LABEL takes `right` or any entry in the sheet's own `label_vocabulary`,
#: which is the owner's `overlay/cos/ingest.md` taxonomy. It is therefore
#: checked against the sheet, not against a constant here.
LABEL_RIGHT = "right"

MARKS_SCHEMA = "cos-marks/1"

#: Every key a saved marks file may carry, and every key one of its mark rows
#: may carry. CLOSED in both directions, exactly like the record's own row
#: sets: a file carrying a key this reader does not know is a version skew or a
#: hand edit, and reading it as "the owner marked nothing extra" is how a
#: silent partial read becomes an owner ruling nobody made.
MARKS_FILE_KEYS = frozenset({
    "schema", "sheet_id", "date", "run", "transport", "rows_shown",
    "content_sha256", "marks", "revoked", "feedback_text",
})
MARK_KEYS = frozenset({
    "conversation_id", "subject_sha256", "action_taken", "category",
    "judgment", "label", "draft", "draft_text", "note", "rules",
})
REVOKE_KEYS = frozenset({"rule_key", "rule", "conversation_id",
                         "subject_sha256"})
RULE_KEYS = frozenset({"column", "value", "text"})

COLUMNS = ("judgment", "label", "draft")

#: The rule sentence each should-be value proposes, BEFORE the owner edits it.
#: A template, never a model call: the sheet has to work with nothing running.
#: `{category}` is the thread's own category word; a thread the ledger gave no
#: category for renders `uncategorised` so the sentence still parses as English
#: instead of collapsing to "Threads in  are noise".
#:
#: These are STANDING instructions quoted verbatim into every future night's
#: prompt, so they name a CLASS (the category) and never this one thread — a
#: rule that can only ever match one conversation is a thread ruling wearing a
#: rule's clothes, and the record already has the right row kind for that.
RULE_TEMPLATES = {
    "judgment:act": "Threads categorised {category} need a reply drafted, "
                    "at priority {tier}.",
    "judgment:read": "Threads categorised {category} are for reading only — "
                     "leave them in the Inbox rather than archiving them.",
    "judgment:noise": "Threads categorised {category} are noise — archive "
                      "them.",
    "label": "Threads like this one belong in the {value} category, not "
             "{category}.",
    "draft:tone": "Reply drafts on {category} threads should match my own "
                  "tone more closely.",
    "draft:facts": "Reply drafts on {category} threads must state only facts "
                   "the vault can support.",
    "draft:too-long": "Reply drafts on {category} threads should be shorter.",
    "draft:missing-point": "Reply drafts on {category} threads must answer "
                           "the main ask directly.",
}

UNCATEGORISED = "uncategorised"


def sheet_run(run_ids: Any) -> str:
    """The run a mark is judging, or `""` when the sheet cannot say.

    A night is one sheet and may be several runs. With more than one, the sheet
    carries no per-thread run id, so attributing every mark to the first of
    them would be an invention — and `run` is documented as `""` when unknown,
    which is a fact the week report can act on. It travels IN the marks file
    (and inside `content_sha256`) because the file transport has nothing else
    to recover it from: the page is the only thing that knows which runs it was
    built from, and by read-back time the owner may have rebuilt the night.
    """
    runs = [str(r) for r in (run_ids or []) if str(r)]
    return runs[0] if len(runs) == 1 else ""


def _template_key(column: str, value: str) -> str:
    """`judgment:act:P1` -> `judgment:act`; `label:x` -> `label`."""
    if column == "judgment":
        return f"judgment:{value.split(':')[0]}"
    if column == "draft":
        return f"draft:{value}"
    return "label"


def suggested_rule(column: str, value: str, category: str) -> str:
    """The sentence a should-be mark proposes. ONE producer, in Python.

    The page substitutes the same template table (it is embedded in the sheet
    state under `rule_templates`, and a render test asserts the strings appear
    there verbatim), so the sentence the owner reads and the sentence this
    module would produce cannot drift into two different offers.
    """
    template = RULE_TEMPLATES.get(_template_key(column, value))
    if not template:
        return ""
    parts = str(value).split(":")
    return template.format(category=str(category) or UNCATEGORISED,
                           value=parts[0],
                           tier=parts[1] if len(parts) > 1 else "")


def rule_origin(column: str, value: str, category: str) -> str:
    """`<column>:<value>:<category>` — what a proposed rule was made FROM.

    It is what makes `contradicted_count` computable: two rules CONTRADICT when
    they came off the same column and the same category with a DIFFERENT value
    ("{X} threads are noise" against "{X} threads need a reply"). Without it a
    later mark disagreeing with a live rule is indistinguishable from a mark
    about something else entirely, and the counter could only ever be zero.
    Empty for a rule the owner typed himself — those have no origin to compare.
    """
    return f"{column}:{value}:{str(category) or UNCATEGORISED}"


def contradicts(origin: str, other: str) -> bool:
    """Same column, same category, different value. Empty origins never do."""
    if not origin or not other or origin == other:
        return False
    left, right = origin.split(":"), other.split(":")
    if len(left) < 3 or len(right) < 3:
        return False
    return (left[0] == right[0] and left[-1] == right[-1]
            and left[1:-1] != right[1:-1])


#: What the porter's own action says about the bucket it chose. `chipped` and
#: `ingested` moved no mail, so neither is an archive; `held`/`none` left the
#: thread where it was.
_ARCHIVED = ("archived", "stale-archived")


def derive_verdict(action_taken: str, judgment: str, label: str,
                   draft: str) -> str:
    """THE OLD THREE WORDS, derived — so old rows and new rows stay one record.

    The direction of the correction decides the word, never the bucket alone:

    * `missed` — the owner wants MORE than happened. He asked for a reply on a
      thread nothing was drafted for, or for an archive on a thread still in
      the Inbox.
    * `wrong` — the owner wants LESS. He asked for `read` on a thread the
      porter archived, drafted for or chipped, and `wrong` is the do-not-touch
      that actually stops tonight's run doing it again.
    * `right` — everything else, INCLUDING a row whose judgment is right and
      whose LABEL or DRAFT is not.

    That last clause is deliberate and it is the conservative reading. `wrong`
    is a PERMANENT do-not-touch on the conversation, and a mis-filed label or a
    too-long draft is not a reason to stop the porter ever touching a thread
    again. Those corrections travel as RULE rows, which change what the judge
    is told without freezing a thread — which is precisely the split the record
    already draws between its two row kinds.
    """
    if judgment == "right" or ":" not in str(judgment):
        return "right"
    want = str(judgment).split(":")[0]
    if want == "act":
        return "right" if action_taken == "drafted" else "missed"
    if want == "noise":
        return "right" if action_taken in _ARCHIVED else "missed"
    # `read`: leave it alone. Only a thread the porter MOVED or answered is a
    # correction; one it already left where it was is agreement.
    return "wrong" if action_taken in (*_ARCHIVED, "drafted", "chipped") \
        else "right"


def _require(cond: Any, msg: str) -> None:
    if not cond:
        raise FeedbackRowInvalid(msg)


def canonical_payload(payload: dict[str, Any]) -> str:
    """The exact bytes `content_sha256` covers, in one place.

    Sorted keys, compact separators, no ASCII escaping — the same three
    settings the page's own serializer uses, because the digest has to match a
    string a browser produced. `transport`, `schema` and `content_sha256`
    itself are OUTSIDE it: the digest identifies the MARKS, so the same marks
    saved through the Artifact path and through the file path hash the same and
    a later reader can say so.
    """
    body = {k: payload.get(k) for k in
            ("sheet_id", "date", "run", "rows_shown", "marks", "revoked",
             "feedback_text")}
    return json.dumps(body, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def marks_content_sha256(payload: dict[str, Any]) -> str:
    return sha256_text(canonical_payload(payload))


def _check_rules(mark: dict[str, Any], index: int) -> None:
    rules = mark.get("rules")
    _require(isinstance(rules, list),
             f"marks[{index}].rules must be a list, empty when the owner "
             "proposed no standing rule off this row")
    for j, rule in enumerate(rules):
        _require(isinstance(rule, dict) and set(rule) == RULE_KEYS,
                 f"marks[{index}].rules[{j}] must carry exactly "
                 f"{sorted(RULE_KEYS)}")
        _require(rule["column"] in COLUMNS,
                 f"marks[{index}].rules[{j}].column must be one of {COLUMNS}")
        _require(isinstance(rule["value"], str) and isinstance(rule["text"],
                                                               str),
                 f"marks[{index}].rules[{j}] value and text must be strings")


def _check_mark(mark: Any, index: int, labels: tuple[str, ...] | None) -> None:
    _require(isinstance(mark, dict) and set(mark) == MARK_KEYS,
             f"marks[{index}] must carry exactly {sorted(MARK_KEYS)}")
    _require(bool(str(mark.get("conversation_id") or "")),
             f"marks[{index}].conversation_id is required")
    _require(_SHA256_RE.fullmatch(str(mark.get("subject_sha256") or "")),
             f"marks[{index}].subject_sha256 must be a full sha256 — the "
             "record carries subject HASHES, and a short or non-hex value is "
             "text that leaked")
    _require(mark.get("action_taken") in ACTIONS,
             f"marks[{index}].action_taken must be one of {ACTIONS}")
    # THE CLOSED VOCABULARIES, REFUSED RATHER THAN SKIPPED. A file carrying an
    # unknown mark value is a hand edit or a version skew; filing the rows
    # around it would turn a page this reader does not understand into a
    # partial owner ruling, which is the same class of failure as reading an
    # unreadable ledger line as "the owner never ruled on that".
    _require(mark.get("judgment") == "" or mark.get("judgment")
             in JUDGMENT_VALUES,
             f"marks[{index}].judgment {mark.get('judgment')!r} is not '' "
             f"(UNSET) and is not one of {JUDGMENT_VALUES}")
    _require(mark.get("draft") == "" or mark.get("draft") in DRAFT_VALUES,
             f"marks[{index}].draft {mark.get('draft')!r} is not '' (UNSET) "
             f"and is not one of {DRAFT_VALUES}")
    label = mark.get("label")
    _require(isinstance(label, str),
             f"marks[{index}].label must be a string, '' when UNSET")
    if labels is not None and label:
        _require(label == LABEL_RIGHT or label in labels,
                 f"marks[{index}].label {label!r} is not `right` and is not "
                 f"in this sheet's taxonomy {labels}")
    # A MARK IS AT LEAST ONE ANSWERED COLUMN. Each column may be UNSET on its
    # own — an owner who says the archive was right and says nothing about the
    # label has answered one question, not three — but a row with all three
    # unset is not a mark, and the writer drops it before the file is written.
    # Finding one here means a page filed silence as an answer.
    _require(_is_marked({"judgment": mark.get("judgment"),
                         "label": mark.get("label"),
                         "draft_mark": mark.get("draft")}),
             f"marks[{index}] has all three columns UNSET — an unmarked row "
             "is UNKNOWN and is never filed")
    for field in ("draft_text", "note", "category"):
        _require(isinstance(mark.get(field), str),
                 f"marks[{index}].{field} must be a string")
    _check_rules(mark, index)


def validate_marks(payload: Any, *,
                   labels: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Refuse a marks file the owner could have saved but this pen cannot file.

    THE SAME FUNCTION ON BOTH ENDS, exactly like `validate_sheet_state`: the
    sheet builder calls it on the payload it derives from a published page, and
    `brain cos-feedback --from-marks` calls it on the bytes the browser wrote.
    A shape checked at only one end still lets a truncated download look like
    an owner who marked nothing.

    `content_sha256` is verified HERE, against `canonical_payload`. It is not a
    security control — anyone who can write the file can write the digest — it
    is an INTEGRITY one: a download the browser cut short, or a file the owner
    hand-edited past what the page can express, fails it and says so instead of
    filing whatever survived.
    """
    _require(isinstance(payload, dict), "a marks file must be an object")
    missing = MARKS_FILE_KEYS - set(payload)
    extra = set(payload) - MARKS_FILE_KEYS
    _require(not missing, f"the marks file is missing {sorted(missing)}")
    _require(not extra, f"the marks file carries unknown keys {sorted(extra)}")
    _require(payload["schema"] == MARKS_SCHEMA,
             f"schema must be {MARKS_SCHEMA!r}, got {payload['schema']!r}")
    from .feedback_rows import TRANSPORTS                     # noqa: PLC0415
    _require(payload.get("transport") in TRANSPORTS,
             f"transport must be one of {TRANSPORTS} — a mark whose route "
             "nobody can name cannot answer which one it came from")
    _require(bool(str(payload.get("sheet_id") or "")),
             "sheet_id is required — a marks file is identified by the SHEET "
             "IT CAME OFF, never by the filename it was saved under")
    _require(isinstance(payload.get("date"), str) and payload["date"],
             "date is required")
    _require(isinstance(payload.get("run"), str),
             "run must be a string, EMPTY rather than absent when the night "
             "ran more than once — the record documents `run: \"\"` as "
             "\"the sheet could not say\", and an absent key would make that "
             "indistinguishable from a file written before it existed")
    shown = payload.get("rows_shown")
    _require(isinstance(shown, int) and not isinstance(shown, bool)
             and shown >= 0,
             "rows_shown must be a non-negative int — it is the DENOMINATOR "
             "every rate computed off this file needs, and a file that does "
             "not carry it can only report bare numbers")
    _require(isinstance(payload.get("marks"), list), "marks must be a list")
    _require(isinstance(payload.get("feedback_text"), str),
             "feedback_text must be a string, present and empty rather than "
             "absent")
    # NOT `len(marks) <= rows_shown`, and that was a real bug in the first
    # cut. `rows_shown` counts the SHORT set the sheet asked about; the page
    # still renders every other row behind one toggle, and an owner who opens
    # it and marks three of them has made three real marks on rows the
    # denominator does not count. The two numbers measure different things and
    # neither bounds the other.
    for index, mark in enumerate(payload["marks"]):
        _check_mark(mark, index, labels)
    revoked = payload.get("revoked")
    _require(isinstance(revoked, list), "revoked must be a list")
    for index, entry in enumerate(revoked):
        _require(isinstance(entry, dict) and set(entry) == REVOKE_KEYS,
                 f"revoked[{index}] must carry exactly {sorted(REVOKE_KEYS)} "
                 "— a revoke is appended as a rule row, and a rule row is "
                 "keyed on the thread the rule came off")
        _require(_SHA256_RE.fullmatch(str(entry.get("subject_sha256") or "")),
                 f"revoked[{index}].subject_sha256 must be a full sha256")
    want = marks_content_sha256(payload)
    _require(payload.get("content_sha256") == want,
             "content_sha256 does not cover these marks (expected "
             f"{want}, file says {payload.get('content_sha256')!r}) — a "
             "truncated download or a hand edit, not an owner ruling")
    return payload


def marks_from_state(state: dict[str, Any], *,
                     transport: str = "artifact") -> dict[str, Any]:
    """The marks a SAVED SHEET STATE carries, as the same payload the file has.

    This is what keeps the Artifact route alive as a second transport rather
    than a second implementation: the published page carries its marks inside
    its own embedded state, and this projects them into the one payload shape
    `record_marks` files. UNSET rows are dropped here, which is where the
    "absence of a mark is not agreement" rule is actually enforced — the count
    of rows the owner was SHOWN travels separately as `rows_shown`.
    """
    marks = []
    for row in state["threads"]:
        if not _is_marked(row):
            continue
        marks.append({
            "conversation_id": str(row["conversation_id"]),
            "subject_sha256": str(row["subject_sha256"]),
            "action_taken": str(row["action_taken"]),
            "category": str(row.get("category") or ""),
            "judgment": str(row["judgment"]),
            "label": str(row["label"]),
            "draft": str(row["draft_mark"]),
            "draft_text": str(row.get("draft_text") or ""),
            "note": str(row.get("note") or ""),
            "rules": [dict(rule) for rule in (row.get("rules") or [])],
        })
    payload = {
        "schema": MARKS_SCHEMA, "sheet_id": str(state["sheet_id"]),
        "date": str(state["date"]), "run": sheet_run(state.get("run_ids")),
        "transport": transport,
        "rows_shown": sum(1 for row in state["threads"] if row["shown"]),
        "marks": marks,
        "revoked": [{"rule_key": str(r["rule_key"]), "rule": str(r["rule"]),
                     "conversation_id": str(r["conversation_id"]),
                     "subject_sha256": str(r["subject_sha256"])}
                    for r in state["standing_rulings"] if r.get("revoke")],
        "feedback_text": str(state.get("feedback_text") or ""),
    }
    payload["content_sha256"] = marks_content_sha256(payload)
    return payload


def _is_marked(row: dict[str, Any]) -> bool:
    """A row is MARKED when at least one of the three columns is NOT UNSET.

    Nothing else counts — not a note, not an edited draft: the record's rows
    are keyed on a verdict, and a note beside three unset columns names no
    correction to derive one from."""
    return bool(row.get("judgment") or row.get("label")
                or row.get("draft_mark"))


__all__ = ['BUCKETS', 'TIERS', 'JUDGMENT_VALUES', 'DRAFT_VALUES',
           'LABEL_RIGHT', 'MARKS_SCHEMA', 'MARKS_FILE_KEYS', 'MARK_KEYS',
           'REVOKE_KEYS', 'RULE_KEYS', 'COLUMNS', 'RULE_TEMPLATES',
           'UNCATEGORISED', 'sheet_run', 'suggested_rule', 'rule_origin', 'contradicts',
           'derive_verdict', 'canonical_payload', 'marks_content_sha256',
           'validate_marks', 'marks_from_state']
