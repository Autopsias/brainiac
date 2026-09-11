"""The morning sheet's EMBEDDED STATE — the transport half of FB-01.

`feedback.py` freezes the durable RECORD. This freezes the JSON the sheet page
carries inside itself, which is a different object with a different lifetime:

    sheet build (s07)  ->  <script type="application/json" id="cos-sheet-state">
    owner marks + Save ->  the page republishes itself with the marks filled in
    read-back (s05)    ->  `brain cos feedback --from-sheet <html>`
                           parses this element and appends record rows

Nothing in this module writes a sheet — `src/brain/cos/sheet.py` does that.
What it does is make the interface CHECKABLE from both ends, so a field the
writer forgets or the reader misspells fails a test rather than silently
rendering an empty block on the one page the owner reads.

Every field carries its producer in `SHEET_STATE_PRODUCERS`. Three of them come
from the RUN LEDGER (s06 writes them there), not from the feedback record, and
saying so here is the whole point: a sheet block sourced from the wrong file is
how a heartbeat ends up watching a directory nothing writes.
"""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from .feedback import (ACTIONS, SHEET_STATE_SCHEMA, VERDICTS, _SHA256_RE,
                       FeedbackRowInvalid, rule_key, thread_digest)

#: One producer for the S06 batch-ledger schema that S07 consumes. The
#: standalone controller imports this instead of spelling the writer and
#: reader independently.
BATCH_LEDGER_SCHEMA = "cos-batch-ledger/1"

#: The door check's three outcomes, as s06 writes them into the run ledger.
#: `skipped-not-signed-in` is a first-class verdict, not an absence: a night
#: that never opened the door must SAY so on the sheet, or silence has two
#: meanings again.
DOOR_VERDICTS = ("open", "closed", "skipped-not-signed-in")

#: s06's named stop conditions, plus the never-started case. `broker-failed`
#: exists because a broker drain that exits non-zero after a HEALTHY child is
#: not a dead session — on 2026-08-31 three chains were logged `session-died`
#: for an index-schema error in the broker's enrichment stage, and the wrong
#: word sent the diagnosis to the browser lane for hours.
#: The six below were added 2026-09-03 for the same reason `broker-failed`
#: exists: the nightly's own header table already names what each child exit
#: code means, but every code without a carve-out landed on `session-died`.
#: Run 250 stopped at rc=4 (the mailbox session lapsed) and run 251 at rc=20
#: (one attachment of 44 did not download); both were logged as a dead session
#: and both sent the reader to the browser lane. A closed vocabulary is the
#: point — it is why a new word has to be added HERE, deliberately, rather
#: than any string reaching the ledger.
STOP_REASONS = ("backlog-empty", "batches-reached", "door-closed",
                "session-died", "broker-failed", "bridge-blocked",
                "not-started", "session-lapsed", "browser-silent",
                "apply-partial", "rehearsal-failed", "bearer-expired",
                "attachment-incomplete", "read-stopped")

#: k for the held-out block: archived-not-drafted threads sampled uniformly at
#: random and shown REGARDLESS of confidence — the only unbiased denominator
#: s09's overturn rate has. The sheet-wide rate falls by construction, because
#: the owner can only overturn what the sheet shows.
#:
#: RAISED FROM 10 TO 25 ON 2026-09-05 (FB-04), and the reason is a denominator
#: rather than a preference. The number this block has to be able to bound is
#: the MISSED-ACT rate: a thread wrongly archived as noise is never surfaced
#: and never marked, so it is invisible to every other block on the page and
#: would read zero forever. A sample is the only instrument that can see it,
#: and a sample only bounds a rate it observes zero of by the rule of three:
#: at k=10 a clean night bounds the missed-act rate at 3/10 = 30%, which is
#: not a bound at all; at k=25 it bounds it at 12%. 25 rows is also still
#: SHORT — the row set FB-04 defaults to is roughly a draft count plus this
#: sample plus the P0/P1 archives, not the 118-row wall.
HELD_OUT_K = 25

SHEET_STATE_KEYS = frozenset({
    "schema", "date", "generated_at", "run_ids", "counts", "threads",
    "held_out", "stale_archived", "standing_rulings", "overturned_last_time",
    "excluded", "door_check", "batch_stop", "feedback_text",
    "category_legend", "sheet_id", "label_vocabulary", "rule_templates",
    "effect", "selection", "questions",
})

#: One line per field: who WRITES it. `feedback_render.render_budget` and the
#: run ledger are two different producers and the sheet must not confuse them.
SHEET_STATE_PRODUCERS = {
    "schema": "s07 sheet builder (constant)",
    "date": "s07 sheet builder (the run's date)",
    "generated_at": "s07 sheet builder",
    "run_ids": "s07 sheet builder, from the run ledger's run ids",
    "counts": "s07 sheet builder: archived/drafted from confirmed "
              "undo rows, ingested from signed_ingested_catching_up, held "
              "from the run's judged dispositions, stale from judge-02",
    "threads": "s07 sheet builder, joining the run's judgments to capture-"
               "corpus subjects and confirmed action records; `verdict`, "
               "`note` and `rule` are filled by the OWNER on Save and read "
               "back by s05; `received`, `category`, `reason` and `files` are "
               "display-only context read from THE RUN'S INGESTION LEDGER "
               "(`received`, `disposition`, `held_reason`, `category`, "
               "`attachment_manifest`) — never from the feedback record, and "
               "never read back",
    "held_out": "s07 sheet builder — SystemRandom.sample over confirmed "
                "archived minus confirmed drafted conversation ids, k=10",
    "stale_archived": "s07 sheet builder, intersecting judge-02's typed "
                      "stale-act signal with confirmed archives",
    "standing_rulings": "feedback_render.ranked_rules (s04); `revoke` is "
                        "filled by the OWNER on Save and read back by s05",
    "overturned_last_time": "feedback rows whose verdict was `wrong`/`missed` "
                            "on the previous sheet (s07 renders, s05 supplies)",
    "excluded": "feedback_render.render_budget — the rulings stored but not "
                "prompt-rendered, counted by kind rather than silently dropped",
    "door_check": "THE RUN LEDGER (s06) — not the feedback record",
    "batch_stop": "THE RUN LEDGER (s06) — not the feedback record",
    "feedback_text": "the OWNER's free-text box, read back by s05",
    "sheet_id": "s07 sheet builder — a deterministic digest of the sheet's "
                "own date, build stamp and run ids. THE IDENTITY OF A SAVED "
                "MARKS FILE, which is never its filename: the browser writes "
                "`cos-marks-<date> (1).json` for a second save, and a reader "
                "keying on the name would file the same marks twice",
    "label_vocabulary": "s07 sheet builder, from `_taxonomy.ingest_taxonomy` "
                        "— every category the owner's own taxonomy defines, "
                        "which is what a should-be LABEL mark may name. "
                        "WIDER than `category_legend`, which explains only "
                        "the categories present on this page",
    "rule_templates": "`sheet_marks.RULE_TEMPLATES` — the sentence each "
                      "should-be value proposes, embedded so the page can "
                      "offer it without a model call and without a second "
                      "copy of the wording",
    "effect": "s07 sheet builder, from the consumed-marks ledger x the "
              "feedback record x tonight's run — what the PREVIOUS sheet's "
              "marks changed, with its denominator",
    "selection": "s07 sheet builder — how many rows the owner is asked to "
                 "LOOK AT and why, versus how many are rendered behind the "
                 "show-all toggle",
    "category_legend": "s07 sheet builder, from `_taxonomy.ingest_taxonomy` — "
                       "THE OWNER'S OWN `overlay/cos/ingest.md`. The engine "
                       "holds no definition of a category beyond the "
                       "disposition that file gives it, so the legend states "
                       "the disposition and never invents a meaning. Empty "
                       "when the taxonomy is off or unparseable",
    "questions": "the owner-interview lane (`interview.sheet_block`, INT-01, "
                 "2026-09-09): the OPEN questions the vault asks the owner, "
                 "each with its evidence, fixed option actions and a default "
                 "of skip, plus the lines the last answers produced. Present "
                 "and empty when the lane has nothing to ask",
}

#: `rule` IS AN s05 EXTENSION to the frozen shape, and it is the field without
#: which the sheet cannot produce a rule row at all (recorded as a named
#: deviation in s05's closeout, and in `docs/cos-feedback-record.md` §9). The
#: record documents `rule` as coming from "the owner / s05"; the sheet as s04
#: froze it gave the owner a verdict, a note and one page-wide free-text box,
#: and none of those is a per-thread ruling. `feedback_text` still mints
#: nothing — a rule row is keyed on a thread and a page-wide box names none.
#: `received`, `category`, `reason` and `files` are the THIRD extension
#: (2026-08-27, owner feedback on the first published sheet). They carry no new
#: capture: every one is already on the run's ingestion-ledger row. They exist
#: because a verdict on an action the owner cannot see the grounds for is not a
#: verdict — the owner reported "it's unclear what I'm saying is right or
#: wrong", and the missing context was the mail's date, the files that came
#: with it, and WHY the porter did what it did. Display-only: `s05`'s
#: `--from-sheet` reader picks the fields it needs by name and never reads
#: these, but the key set is CLOSED, so they must be declared here or a saved
#: sheet is refused on read-back.
#: THE SIXTH EXTENSION (2026-09-05, FB-03/FB-04) is the biggest one, and it
#: replaces rather than adds: the single `verdict` + `rule` pair became THREE
#: independent marks (`judgment`, `label`, `draft_mark`), the draft's own text
#: (`draft_text`), a LIST of proposed rules (`rules`) instead of one box, the
#: judge's tier (`tier`) so a P0/P1 archive can be selected for the short row
#: set, and `shown` — whether this row is one the owner is asked to LOOK AT.
#: `verdict` stays, DERIVED from the three columns by
#: `sheet_marks.derive_verdict`, so the record keeps one vocabulary. `rule` is
#: GONE, and that is why `SHEET_STATE_SCHEMA` went to `/2`: a page that still
#: carries it cannot express the new columns and must refuse, not degrade.
_THREAD_KEYS = frozenset({"conversation_id", "conversation_id_digest",
                          "subject_sha256", "subject", "action_taken", "tier",
                          "verdict", "judgment", "label", "draft_mark",
                          "draft_text", "rules", "note", "held_out", "shown",
                          "stale_archived", "received", "category", "reason",
                          "files",
                          # WHAT REACHED THE VAULT, as two separate answers —
                          # `/3`, owner request 2026-09-07: "important to
                          # differentiate email body ingestion and file
                          # attachments ingestion too". Display only: it adds
                          # no mark column, so a marks FILE is unaffected and
                          # `sheet_marks.validate_marks` did not move. The
                          # exact-set rule still means an older PAGE refuses
                          # rather than degrades, which is this file's own
                          # standing choice.
                          "capture"})
#: `conversation_id` and `subject_sha256` are the SECOND s05 extension, for the
#: revoke control. A revoke is appended as a rule row and a rule row is keyed on
#: a thread, so the retirement has to carry the provenance of the rule it
#: retires. Without them a revoke would have to invent a conversation id and
#: stamp `sha256("")` as the subject of a thread that had one.
#: `fired_count`, `contradicted_count` and `in_prompt` are the sixth
#: extension's other half: a rule NOTHING CAN RETIRE keeps driving the judge
#: long after the owner stopped meaning it, and the owner cannot decide to
#: retire one without seeing how often it has been in force and how often a
#: later mark disagreed with it. `in_prompt` says whether this rule is inside
#: the capped 40 the judge actually reads — the sheet lists every live rule so
#: each keeps its revoke control, and a rule the owner revokes believing it is
#: in force tonight, when it is not, is a wasted decision.
_RULING_KEYS = frozenset({"rule", "rule_key", "confirmations", "expires_at",
                          "revoke", "conversation_id", "subject_sha256",
                          "fired_count", "contradicted_count", "in_prompt"})


def _require(cond: Any, msg: str) -> None:
    if not cond:
        raise FeedbackRowInvalid(msg)


def _check_marks(row: Any, index: int) -> None:
    """The three columns, each of which has FOUR states, not three.

    `""` is UNSET and is a first-class value: it means the owner has not
    answered this question, which is neither agreement nor disagreement. It is
    also the only state a freshly built sheet may carry — a pre-selected
    control cannot be told apart from a mark the owner made, so a sheet that
    shipped one would manufacture the very agreement `verdict is None` already
    exists to prevent.
    """
    from .sheet_marks import (DRAFT_VALUES, JUDGMENT_VALUES,  # noqa: PLC0415
                              RULE_KEYS, COLUMNS)

    for field, allowed in (("judgment", JUDGMENT_VALUES),
                           ("draft_mark", DRAFT_VALUES)):
        _require(row.get(field) == "" or row.get(field) in allowed,
                 f"threads[{index}].{field} must be '' (UNSET — the owner has "
                 f"not answered) or one of {allowed}")
    for field in ("label", "draft_text", "tier"):
        _require(isinstance(row.get(field), str),
                 f"threads[{index}].{field} must be a string, empty when the "
                 "run ledger or the owner gave none")
    rules = row.get("rules")
    _require(isinstance(rules, list),
             f"threads[{index}].rules must be a list — one entry per column "
             "the owner corrected, empty when he corrected none")
    for j, rule in enumerate(rules):
        _require(isinstance(rule, dict) and set(rule) == RULE_KEYS,
                 f"threads[{index}].rules[{j}] must carry exactly "
                 f"{sorted(RULE_KEYS)}")
        _require(rule["column"] in COLUMNS,
                 f"threads[{index}].rules[{j}].column must be one of "
                 f"{COLUMNS}")


def _check_thread(row: Any, index: int) -> None:
    _require(isinstance(row, dict) and set(row) == _THREAD_KEYS,
             f"threads[{index}] must carry exactly {sorted(_THREAD_KEYS)}")
    _require(bool(str(row.get("conversation_id") or "")),
             f"threads[{index}].conversation_id is required")
    _require(row["conversation_id_digest"]
             == thread_digest(row["conversation_id"]),
             f"threads[{index}].conversation_id_digest is not the digest of "
             "its own conversation_id")
    _require(row["action_taken"] in ACTIONS,
             f"threads[{index}].action_taken must be one of {ACTIONS}")
    # An UNMARKED row is `None`, never a default verdict. A sheet that ships
    # every row pre-marked `right` would manufacture agreement the owner never
    # gave, and s09 would score it as a real denominator.
    _require(row["verdict"] is None or row["verdict"] in VERDICTS,
             f"threads[{index}].verdict must be null or one of {VERDICTS}")
    _require(isinstance(row.get("note"), str),
             f"threads[{index}].note must be a string")
    _check_marks(row, index)
    for flag in ("held_out", "stale_archived", "shown"):
        _require(isinstance(row.get(flag), bool),
                 f"threads[{index}].{flag} must be a bool")
    # PRESENT AND POSSIBLY EMPTY, for the same reason `rule` is: a sheet built
    # before these existed must be REFUSED, not read as "this thread had no
    # date and no files". An empty string is the honest "the ledger did not
    # carry one"; an absent key is a version skew.
    for field in ("received", "category", "reason"):
        _require(isinstance(row.get(field), str),
                 f"threads[{index}].{field} must be a string, empty when the "
                 "run ledger carried none")
    _require(isinstance(row.get("files"), list)
             and all(isinstance(name, str) for name in row["files"]),
             f"threads[{index}].files must be a list of filenames")


def _check_ruling(row: Any, index: int) -> None:
    _require(isinstance(row, dict) and set(row) == _RULING_KEYS,
             f"standing_rulings[{index}] must carry exactly "
             f"{sorted(_RULING_KEYS)} — the revoke control included")
    _require(row["rule_key"] == rule_key(row.get("rule", "")),
             f"standing_rulings[{index}].rule_key must be rule_key(rule)")
    _require(isinstance(row.get("revoke"), bool),
             f"standing_rulings[{index}].revoke must be a bool")
    _require(isinstance(row.get("in_prompt"), bool),
             f"standing_rulings[{index}].in_prompt must be a bool — whether "
             "this rule is inside the capped set the judge actually reads")
    for field in ("fired_count", "contradicted_count"):
        count = row.get(field)
        _require(isinstance(count, int) and not isinstance(count, bool)
                 and count >= 0,
                 f"standing_rulings[{index}].{field} must be a non-negative "
                 "int — the governance counters beside the revoke control")
    _require(isinstance(row.get("confirmations"), int)
             and not isinstance(row.get("confirmations"), bool),
             f"standing_rulings[{index}].confirmations must be an int")
    _require(bool(str(row.get("conversation_id") or "")),
             f"standing_rulings[{index}].conversation_id is required — a "
             "revoke is appended as a rule row, and a rule row is keyed on the "
             "thread the rule came off")
    _require(_SHA256_RE.fullmatch(str(row.get("subject_sha256") or "")),
             f"standing_rulings[{index}].subject_sha256 must be the record's "
             "own full sha256, carried forward so a revoke retires the rule "
             "with the provenance it was minted under")


def _check_blocks(state: dict[str, Any]) -> None:
    held = state.get("held_out")
    _require(isinstance(held, dict) and set(held) == {"k", "conversation_ids",
                                                      "population"},
             "held_out must carry k, conversation_ids and population")
    _require(len(held["conversation_ids"]) == min(held["k"], held["population"]),
             "held_out.conversation_ids must hold k ids, or the whole "
             "population when it is smaller — a short sample is a smaller "
             "denominator, not a full one")
    door = state.get("door_check")
    _require(isinstance(door, dict) and door.get("verdict") in DOOR_VERDICTS,
             f"door_check.verdict must be one of {DOOR_VERDICTS} "
             "(read from the RUN LEDGER, not from the feedback record)")
    stop = state.get("batch_stop")
    _require(isinstance(stop, dict) and stop.get("reason") in STOP_REASONS,
             f"batch_stop.reason must be one of {STOP_REASONS} "
             "(read from the RUN LEDGER, not from the feedback record)")
    excluded = state.get("excluded")
    _require(isinstance(excluded, dict)
             and set(excluded) == {"rules", "thread_rulings"},
             "excluded must count BOTH kinds the render ceiling left out — a "
             "ruling excluded from the prompt is named, never silently dropped")
    for kind in ("rules", "thread_rulings"):
        _require(isinstance(excluded[kind], int)
                 and not isinstance(excluded[kind], bool)
                 and excluded[kind] >= 0,
                 f"excluded.{kind} must be a non-negative integer count")
    # PRESENT AND POSSIBLY EMPTY. An empty legend is the honest "this vault's
    # category taxonomy is off or unparseable"; an ABSENT key is a version
    # skew, and the two must never read the same.
    legend = state.get("category_legend")
    _require(isinstance(legend, list), "category_legend must be a list, empty "
             "when the owner's ingest taxonomy is off or unparseable")
    for index, entry in enumerate(legend):
        _require(isinstance(entry, dict)
                 and set(entry) == {"category", "disposition", "means"},
                 f"category_legend[{index}] must carry exactly ['category', "
                 "'disposition', 'means']")
        _require(all(isinstance(entry[key], str) and entry[key]
                     for key in entry),
                 f"category_legend[{index}] values must be non-empty strings")


def _check_new_blocks(state: dict[str, Any]) -> None:
    """`sheet_id`, the two vocabularies the page needs, `effect`, `selection`."""
    _require(bool(str(state.get("sheet_id") or "")),
             "sheet_id is required — it is the identity a saved marks file is "
             "matched on, and without it the only identity left is the "
             "filename, which the browser rewrites on a second save")
    labels = state.get("label_vocabulary")
    _require(isinstance(labels, list)
             and all(isinstance(x, str) and x for x in labels),
             "label_vocabulary must be a list of non-empty category names, "
             "empty when the owner's ingest taxonomy is off or unparseable")
    templates = state.get("rule_templates")
    _require(isinstance(templates, dict)
             and all(isinstance(k, str) and isinstance(v, str)
                     for k, v in templates.items()),
             "rule_templates must map a (column, value) key to its sentence")
    selection = state.get("selection")
    _require(isinstance(selection, dict)
             and set(selection) == {"shown", "total", "reasons"},
             "selection must carry shown, total and reasons — 'short' is "
             "measured in rows the owner must LOOK AT, not rows rendered, so "
             "both counts have to be on the page")
    _require(isinstance(selection["reasons"], dict)
             and all(isinstance(v, int) and not isinstance(v, bool)
                     for v in selection["reasons"].values()),
             "selection.reasons must count the rows each selection rule "
             "brought in")
    effect = state.get("effect")
    _require(isinstance(effect, dict)
             and set(effect) == {"previous_sheet_id", "previous_date",
                                 "rows_shown", "marks", "ruled",
                                 "seen_again", "changed", "sentence"},
             "effect must carry the previous sheet's identity, ITS "
             "DENOMINATOR (rows_shown), the marks made on it, how many of "
             "those left a ruling, and what changed — a bare number with no "
             "denominator is the thing this block exists to stop printing")
    _require(isinstance(effect["sentence"], str) and effect["sentence"],
             "effect.sentence must state its own scope in words; when no "
             "marks preceded this sheet it SAYS so rather than printing zero")


def _check_questions(state: dict[str, Any]) -> None:
    """The interview block: open questions in the lane's closed row shape."""
    from .. import interview as _interview                    # noqa: PLC0415
    q = state.get("questions")
    _require(isinstance(q, dict) and set(q) == {"rows", "applied", "quiet"},
             "questions must carry rows, applied and quiet — the interview "
             "lane's open questions, present and empty rather than absent")
    try:
        _interview.validate_rows(q["rows"])
    except ValueError as exc:
        _require(False, str(exc))
    _require(isinstance(q["applied"], list)
             and all(isinstance(x, dict) for x in q["applied"]),
             "questions.applied must be a list of the lines the last answers "
             "produced")


def validate_sheet_state(state: Any) -> dict[str, Any]:
    """Refuse a sheet state the owner could act on but s05 could not read back.

    Called by s07 before it writes a sheet, and by s05 after it parses one. The
    same function on both ends is the point: a shape checked only at write time
    still lets a hand-edited or truncated page look like an owner who marked
    nothing.
    """
    _require(isinstance(state, dict), "sheet state must be an object")
    missing = SHEET_STATE_KEYS - set(state)
    extra = set(state) - SHEET_STATE_KEYS
    _require(not missing, f"sheet state is missing {sorted(missing)}")
    _require(not extra, f"sheet state carries unknown keys {sorted(extra)}")
    _require(state["schema"] == SHEET_STATE_SCHEMA,
             f"schema must be {SHEET_STATE_SCHEMA!r}")
    _require(isinstance(state.get("feedback_text"), str),
             "feedback_text must be a string — the free-text box, present and "
             "empty rather than absent")
    for name in ("threads", "held_out", "excluded", "door_check", "batch_stop"):
        _require(state.get(name) is not None, f"{name} is required")
    for i, row in enumerate(state["threads"]):
        _check_thread(row, i)
    for i, row in enumerate(state["standing_rulings"]):
        _check_ruling(row, i)
    _check_blocks(state)
    _check_new_blocks(state)
    _check_questions(state)
    return state


__all__ = ['BATCH_LEDGER_SCHEMA', 'DOOR_VERDICTS', 'STOP_REASONS', 'HELD_OUT_K',
           'SHEET_STATE_KEYS', 'SHEET_STATE_PRODUCERS', 'validate_sheet_state']
