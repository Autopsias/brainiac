"""The FROZEN SHAPE of a feedback row — vocabularies, keys, validator.

Split out of `feedback.py` on 2026-09-05, when the marks rewrite (FB-03/FB-04)
took that module past the 500-LOC file-size bound. The seam is the one the file
already had in its own section comments: this module says WHAT A ROW IS and
what its words may be; `feedback.py` keeps WHERE THE RECORD LIVES and HOW A ROW
IS WRITTEN AND READ BACK.

Imports flow one way and there is no cycle: nothing here reads `feedback`.
`feedback` re-exports every public name below with `from .feedback_rows import
*` plus an explicit line for the underscore names, so every existing
`from .feedback import ACTIONS` keeps working unchanged.
"""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._layout import _parse_ts

FEEDBACK_SCHEMA = "cos-feedback/1"

#: The DOM id of the `<script type="application/json">` the sheet embeds its
#: state in. s07 writes it, s05's `--from-sheet` reads it back out of the saved
#: HTML. One name, stated once, so the writer and the reader cannot drift.
SHEET_STATE_ELEMENT_ID = "cos-sheet-state"
#: BUMPED TO /2 ON 2026-09-05 (FB-03). The thread row's single `verdict` +
#: `rule` pair became three independent marks, so a `/1` page cannot express a
#: `/2` state and a `/2` reader cannot honestly read a `/1` one. The bump is
#: what makes an old saved sheet REFUSE instead of reading as an owner who
#: marked nothing — the same reason the thread key set is closed. The RECORD's
#: own schema (`cos-feedback/1`) is deliberately NOT bumped: its 23 existing
#: rows still validate, which is the whole point of the new fields being
#: optional on read.
#: `/3` since 2026-09-07: thread rows carry `capture`, the two-lane answer
#: to "what reached the vault". Display only — no mark column moved and
#: `sheet_marks.validate_marks` is untouched — but the thread-row key set
#: is exact, so an older PAGE refuses rather than degrades.
SHEET_STATE_SCHEMA = "cos-sheet-state/4"

KIND_RULE = "rule"
KIND_THREAD = "thread"
KINDS = (KIND_RULE, KIND_THREAD)

#: Where the ruling came from. `sheet` = the owner marked it on the morning
#: sheet; `outlook` = the run's enumeration diffed the undo ledger against
#: mailbox state and found the owner had reversed it by hand.
SOURCES = ("sheet", "outlook")

#: `missed` is the third verdict on purpose: "you did nothing and should have"
#: is a correction the two-value right/wrong pair cannot express.
VERDICTS = ("right", "wrong", "missed")

#: THE ONE VERDICT THAT TAKES A THREAD OFF THE TABLE, stated here rather than in
#: either consumer, because the host guard and the prompt block both read it and
#: a second spelling is how they come to disagree. `wrong` is the owner
#: reversing what the porter did. `right` is agreement and must block nothing —
#: it is also the RELEASE: a later `right` ruling lifts an earlier `wrong` one,
#: because the fold takes the latest row per conversation.
DO_NOT_TOUCH_VERDICT = "wrong"

#: `missed` asks for MORE, not less, so it is NOT a do-not-touch and no host
#: guard refuses anything on it. It is rendered to the judge as its own list.
WANTED_MORE_VERDICT = "missed"

#: What the porter did to the thread, in the sheet's own outcome words. This is
#: NOT the undo ledger's `verb` vocabulary (`archive`/`categorize`/`draft`) —
#: that is the mutation layer. The mapping is one-way and stated in
#: `docs/cos-feedback-record.md`; do not invent a second one.
ACTIONS = ("archived", "ingested", "drafted", "held", "chipped",
           "stale-archived", "none")

#: The one-way mapping from the mutation layer's verb to this vocabulary, so
#: s07 and s09 do not each invent one. Keys are `cos_reconcile_metrics
#: .MUTATION_VERBS`; the test asserts that, so a verb added there fails here
#: rather than quietly reaching the sheet as an unknown word. `ingested`,
#: `held`, `stale-archived` and `none` have no verb — no mailbox mutation
#: happened — which is why this is a mapping and not a rename.
ACTION_FOR_VERB = {"archive": "archived", "categorize": "chipped",
                   "draft": "drafted"}

#: HOW A MARK REACHED THE RECORD. `file` is the owner's own Mac: the sheet
#: saves `cos-marks-<date>.json` through the browser's Downloads path and
#: `brain cos-feedback --from-marks` reads it. `artifact` is the published-page
#: route, kept as an optional second one. A row carrying NEITHER predates the
#: field — the 23 rows this record already holds do — and reads as `""`, which
#: means UNKNOWN and is never retro-labelled with a guess.
TRANSPORTS = ("file", "artifact")

#: How long a standing rule lives without being re-confirmed. 90 days matches
#: `DEFAULT_AUTOCAP_WINDOW_DAYS`, the window this vault already uses for "how
#: far back does behavioural evidence count"; a ruling about the owner's mail
#: is the same sort of claim. A confirmation pushes it out by another window.
RULE_EXPIRY_DAYS_ENV = "BRAIN_COS_FEEDBACK_RULE_DAYS"
DEFAULT_RULE_EXPIRY_DAYS = 90

#: The per-kind RENDERED caps. Storing every ruling and rendering every ruling
#: are different things. The measured cliff is ~250 rows in one model message
#: (258 verdicts, 24 of them non-compliant), and s06's per-batch thread cap sits
#: "well under 250" — so the rulings block must fit in what is left. 40 + 80 =
#: 120 rendered ruling rows leaves headroom for a 120-thread batch under the
#: cliff. Both are read in the pass that assembles the prompt, never earlier.
RULE_CAP_ENV = "BRAIN_COS_FEEDBACK_RULE_CAP"
DEFAULT_RULE_CAP = 40
THREAD_CEILING_ENV = "BRAIN_COS_FEEDBACK_THREAD_CEILING"
DEFAULT_THREAD_RENDER_CEILING = 80

#: CLOSED field sets, one per kind, enforced where the row is SERIALIZED.
#: `expires_at` and `confirmations` are absent from the thread set on purpose:
#: "never expired, never deduplicated" is then structural, not prose — a thread
#: row that carries an expiry is REFUSED rather than quietly honoured.
#:
#: THE FIVE FIELDS ADDED 2026-09-05 ARE OPTIONAL ON READ, REQUIRED ON WRITE.
#: `set(row) - keys` is what the closed set enforces, so a row that OMITS one
#: still validates — which is the whole point: this record already holds 23
#: rows written before any of them existed, and a schema bump that made them
#: unreadable would erase the only owner feedback the porter has ever had.
#: Every row this session writes carries all five; a row that does not is
#: read as UNKNOWN (`""` / `0`), never as a value.
RULE_ROW_KEYS = frozenset({
    "kind", "schema", "ts", "source", "thread", "action_taken", "verdict",
    "note", "rule", "rule_key", "confirmations", "expires_at", "revoked", "run",
    "transport", "sheet_id", "rule_origin", "fired_count",
    "contradicted_count",
})
THREAD_ROW_KEYS = frozenset({
    "kind", "schema", "ts", "source", "conversation_id",
    "conversation_id_digest", "subject_sha256", "action_taken", "verdict",
    "note", "run", "transport", "sheet_id",
})
_THREAD_KEY_SUB = frozenset({"conversation_id", "conversation_id_digest",
                             "subject_sha256"})

_WS_RE = re.compile(r"\s+")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class FeedbackRowInvalid(ValueError):
    """A row that would have been written, or read back, outside the schema."""


# --------------------------------------------------------------------------
# the two derived keys — one producer each
# --------------------------------------------------------------------------
def thread_digest(value: str) -> str:
    """A mailbox id as its 16-hex SHA-256 prefix.

    The SAME convention (and therefore the same values) as the undo ledger's
    `conversation_id_digest`, so a feedback row and a mutation row for one
    thread quote the same short id in evidence. `tests/test_cos_feedback.py`
    pins that equality against `tools/cos_driver_transport.short` rather than
    trusting this sentence.
    """
    return sha256_text(str(value))[:16]


def subject_digest(subject: str) -> str:
    """A subject line as a full SHA-256. Hashes, never text — §"the record
    carries subject hashes". Whitespace-normalized first so the same subject
    reached through two mailbox reads hashes the same."""
    return sha256_text(_WS_RE.sub(" ", str(subject)).strip())


def rule_key(rule: str) -> str:
    """The dedupe key for a standing rule: casefolded, whitespace-collapsed,
    trailing punctuation stripped. Two sheets that phrase one ruling with a
    different full stop are ONE rule, not two competing for the same cap."""
    text = _WS_RE.sub(" ", str(rule)).strip().casefold()
    return text.rstrip(" .;:!,")



# --------------------------------------------------------------------------
# validation — the closed field set, enforced at serialize AND at read
# --------------------------------------------------------------------------
def _require(cond: Any, msg: str) -> None:
    if not cond:
        raise FeedbackRowInvalid(msg)


def _check_common(row: dict[str, Any], keys: frozenset[str]) -> str:
    kind = row.get("kind")
    _require(kind in KINDS, f"kind must be one of {KINDS}, got {kind!r}")
    extra = set(row) - keys
    _require(not extra, f"{kind} row carries keys outside its set: {sorted(extra)}")
    _require(row.get("schema") == FEEDBACK_SCHEMA,
             f"schema must be {FEEDBACK_SCHEMA!r}, got {row.get('schema')!r}")
    _require(_parse_ts(str(row.get("ts") or "")) is not None,
             f"ts is not a timestamp: {row.get('ts')!r}")
    _require(row.get("source") in SOURCES,
             f"source must be one of {SOURCES}, got {row.get('source')!r}")
    _require(row.get("action_taken") in ACTIONS,
             f"action_taken must be one of {ACTIONS}, got "
             f"{row.get('action_taken')!r}")
    _require(isinstance(row.get("note"), str), "note must be a string")
    _require(row.get("transport", "") in ("",) + TRANSPORTS,
             f"transport must be one of {TRANSPORTS}, or absent on a row "
             f"written before the field existed, got {row.get('transport')!r}")
    _require(isinstance(row.get("sheet_id", ""), str),
             "sheet_id must be a string — the id of the sheet build this mark "
             "came off, never the filename it was saved under")
    return str(kind)


def _check_rule(row: dict[str, Any]) -> None:
    thread = row.get("thread")
    _require(isinstance(thread, dict) and set(thread) == _THREAD_KEY_SUB,
             f"thread must be exactly {sorted(_THREAD_KEY_SUB)}")
    _require(bool(str(thread.get("conversation_id") or "")),
             "thread.conversation_id is required")
    _require(thread.get("conversation_id_digest")
             == thread_digest(thread.get("conversation_id", "")),
             "thread.conversation_id_digest must be "
             "thread_digest(thread.conversation_id)")
    _require(_SHA256_RE.fullmatch(str(thread.get("subject_sha256") or "")),
             "thread.subject_sha256 must be a full sha256 — the record carries "
             "subject HASHES, and a short or non-hex value is text that leaked")
    _require(row.get("verdict") in VERDICTS,
             f"verdict must be one of {VERDICTS}, got {row.get('verdict')!r}")
    _require(isinstance(row.get("rule"), str), "rule must be a string")
    _require(row.get("rule_key") == rule_key(row.get("rule", "")),
             "rule_key must be rule_key(rule) — one producer, not a free field")
    conf = row.get("confirmations")
    _require(isinstance(conf, int) and not isinstance(conf, bool) and conf >= 1,
             f"confirmations must be an int >= 1, got {conf!r}")
    _require(_parse_ts(str(row.get("expires_at") or "")) is not None,
             f"expires_at is not a timestamp: {row.get('expires_at')!r}")
    _require(isinstance(row.get("revoked"), bool), "revoked must be a bool")
    _require(isinstance(row.get("rule_origin", ""), str),
             "rule_origin must be a string — `<column>:<value>:<category>` "
             "for a rule the sheet proposed, empty for one the owner typed")
    for field in ("fired_count", "contradicted_count"):
        count = row.get(field, 0)
        _require(isinstance(count, int) and not isinstance(count, bool)
                 and count >= 0,
                 f"{field} must be a non-negative int, got {count!r}")


def _check_thread(row: dict[str, Any]) -> None:
    _require(bool(str(row.get("conversation_id") or "")),
             "conversation_id is required — a thread ruling is keyed on it alone")
    _require(row.get("conversation_id_digest")
             == thread_digest(row.get("conversation_id", "")),
             "conversation_id_digest must be thread_digest(conversation_id)")
    _require(_SHA256_RE.fullmatch(str(row.get("subject_sha256") or "")),
             "subject_sha256 must be a full sha256 — the record carries "
             "subject HASHES, and a short or non-hex value is text that leaked")
    _require(row.get("verdict") in VERDICTS,
             f"verdict must be one of {VERDICTS}, got {row.get('verdict')!r}")


def validate_row(row: dict[str, Any]) -> dict[str, Any]:
    """Refuse anything outside the frozen shape, in BOTH directions.

    Called on every append and on every row read back. A ledger validated only
    on the way in is a ledger that trusts whatever an editor, a partial write
    or a hand-repair left behind — and this one authorises prompt text that
    changes what the porter does to real mail.
    """
    _require(isinstance(row, dict), "a feedback row must be an object")
    kind = _check_common(
        row, RULE_ROW_KEYS if row.get("kind") == KIND_RULE else THREAD_ROW_KEYS)
    (_check_rule if kind == KIND_RULE else _check_thread)(row)
    return row




__all__ = [
    'FEEDBACK_SCHEMA', 'SHEET_STATE_ELEMENT_ID', 'SHEET_STATE_SCHEMA',
    'KIND_RULE', 'KIND_THREAD', 'KINDS', 'SOURCES', 'VERDICTS',
    'DO_NOT_TOUCH_VERDICT', 'WANTED_MORE_VERDICT', 'ACTIONS',
    'ACTION_FOR_VERB', 'TRANSPORTS', 'RULE_EXPIRY_DAYS_ENV',
    'DEFAULT_RULE_EXPIRY_DAYS', 'RULE_CAP_ENV', 'DEFAULT_RULE_CAP',
    'THREAD_CEILING_ENV', 'DEFAULT_THREAD_RENDER_CEILING', 'RULE_ROW_KEYS',
    'THREAD_ROW_KEYS', 'FeedbackRowInvalid', 'thread_digest',
    'subject_digest', 'rule_key', 'validate_row',
]
