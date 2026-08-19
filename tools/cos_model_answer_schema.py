"""The closed verdict-key schema every model answer is projected onto (D14).

This is the s17 extraction out of :mod:`cos_model_answer`: the two key tables,
their declared types, their per-field and per-list ceilings, and the per-key
projection loop that ``project_row`` dispatches to. Nothing here reads the
run-patchable aggregate bounds (``MAX_ROW_BYTES`` / ``MAX_PARSED_ROWS``) —
those stay in the parent and are passed in at call time, so a test that
rebinds them on the parent module is still honoured. Import direction is
one-way: this module imports nothing from its parent; the tables' consumers
reach it through the parent's re-exports.

The key sets are read off the four batch prompts' own `ANSWER with a JSON
array` shapes (`tools/cos_judge.py`). An unknown key at ANY depth is dropped
and counted; it is never carried.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

#: TWO SCHEMAS, AND THE CATEGORY LEG IS THE OTHER ONE. It runs PRE-DRAW, before
#: `cos_ground.py` has run at all (which is the whole ordering argument for sink
#: 12), and its batch asks for exactly one key beside the id. Projecting it onto
#: the judgment table would drop `category` and disarm the gate on every night –
#: measured, not reasoned: it did, in `test_only_a_validated_category_answer_
#: arms_the_gate`. So the caller names its schema and the default is the
#: judgment one; a caller that forgets is projected MORE tightly, never less.
CATEGORY_KEYS: frozenset[str] = frozenset({"conversation_id", "category"})

ALLOWED_KEYS: frozenset[str] = frozenset({
    "conversation_id",
    # triage
    "bucket", "tier", "summary", "triage_evidence", "auto_archive",
    "noise_signal",
    # staging
    "disposition", "substance_kind", "classification", "evidence_span",
    "held_reason", "dedup_check", "dedup_kind", "merge_candidate",
    # hold
    "hold_verdict", "resolution_evidence",
    # draft
    "draft",
})
DRAFT_KEYS: frozenset[str] = frozenset(
    {"text", "recipients_scope", "placeholders", "form", "voice"})
SPAN_KEYS: frozenset[str] = frozenset({"start", "end"})

#: THE DECLARED TYPE OF EVERY ALLOWED KEY. A key-closed projection is NOT a
#: closed projection, and the adversarial review proved it: a DICT in a string
#: field skipped every check — `_too_long` returns False for a non-`str`, the
#: overlap test only looks at `str` and `list`, and the dict was written to
#: `verdicts.json` verbatim. Reproduced before this table existed:
#:
#:     {"conversation_id": "c", "triage_evidence": {"x": "<vault prose>"}}
#:
#: A value must be `None` or its declared type. Anything else is a REFUSED row,
#: never a coerced one.
_STR, _BOOL, _STRLIST = "str", "bool", "strlist"
KEY_TYPES: dict[str, str] = {
    "conversation_id": _STR,
    "bucket": _STR, "tier": _STR, "summary": _STRLIST, "triage_evidence": _STR,
    "auto_archive": _BOOL, "noise_signal": _STR,
    "disposition": _STR, "substance_kind": _STR, "classification": _STR,
    "held_reason": _STR, "dedup_check": _STR, "dedup_kind": _STR,
    "merge_candidate": _STR,
    "hold_verdict": _STR, "resolution_evidence": _STR,
    # THE CATEGORY SCHEMA'S ONE FIELD, declared here rather than left to
    # `KEY_TYPES.get(key, _STR)`'s default. It was the measured hole in the
    # "enumerating" tests: `set(CATEGORY_KEYS) - set(KEY_TYPES) == {'category'}`,
    # so a retained string field on the second schema was reached by no test
    # that enumerates a type table (review 2026-08-15).
    "category": _STR,
}
DRAFT_TYPES: dict[str, str] = {
    "text": _STR, "recipients_scope": _STR, "placeholders": _STRLIST,
    "form": _STR, "voice": _STR,
}

#: THE STRUCTURED KEYS — the two the projection walks by hand rather than by
#: declared type. Everything else admitted by EITHER schema is a scalar or a
#: list of scalars and must carry a declared type; `test_every_admitted_key_
#: carries_a_declared_type` asserts exactly that closure, over the UNION of both
#: schemas, so neither table can gain a member the other's tests never see.
STRUCTURED_KEYS: frozenset[str] = frozenset({"draft", "evidence_span"})
#: Every string-bearing field name the projection can retain, on either schema,
#: nested names included. The enumerating tests generate their fields from THIS
#: rather than from one type table.
ADMITTED_FIELDS: frozenset[str] = frozenset(
    ((ALLOWED_KEYS | CATEGORY_KEYS) - STRUCTURED_KEYS)
    | {f"draft.{k}" for k in DRAFT_KEYS})

#: THE OUTPUT-BEARING BOUND, and it is a DIFFERENT control from the input budget
#: (adversarial review 2026-08-14). Runs 131-133 died on the model's SINGLE-
#: MESSAGE OUTPUT cap (64000 tokens — see the parent module's own header), and a
#: per-row INPUT character budget cannot bound that. These are the per-field
#: ceilings the batch prompts state and this projection enforces, in characters:
#:
#:   non-draft row   2 x 600 (summary) + 600 + 600 + 200  ~= 2,800 chars
#:   x 50 rows                                            ~= 140,000 chars
#:   draft row       4,000 + 10 x 200 + 200               ~=  6,200 chars
#:   x DRAFT_CAP 10                                       ~=  62,000 chars
#:   chunk total                                          ~= 202,000 chars
#:                                                        ~=  50,500 tokens
#:
#: — 79 % of the single-message cap at the shipped chunk size of 50, so a chunk
#: that obeys these bounds cannot reach the cap that killed run 131.
#:
#: WHAT IT IS NOT: this cannot stop the model EMITTING more — nothing host-side
#: can. It bounds what reaches disk, and it makes over-emission a counted number
#: (`refused_oversize_field`) instead of a silently long artifact. The stream
#: reassembly is what survives a multi-turn answer; this is what bounds it.
FIELD_MAX = {"summary": 600, "triage_evidence": 600, "held_reason": 200,
             "resolution_evidence": 600, "draft.text": 4000,
             "draft.placeholders": 200, "draft.voice": 200,
             # An id, not prose — and it reaches the briefing HTML.
             "merge_candidate": 128}
#: Every OTHER string field is a closed vocabulary or an id, so it gets a
#: generous default rather than no bound at all. An unlisted field with no cap
#: is how `merge_candidate` came to carry a paragraph.
DEFAULT_FIELD_MAX = 600

#: THE SAME CEILING IN UTF-8 BYTES, and it is not a restatement of the character
#: one (adversarial review 2026-08-15, requirement (d), the OUTPUT half). The
#: arithmetic above converts characters to TOKENS at ~4 chars/token, which is an
#: ASCII assumption: 600 CJK characters are ~1800 bytes and ~600 tokens, so a row
#: obeying every character cap could still be 3x the token budget the caps were
#: sized against — and 1,000 six-hundred-character CJK `summary` strings measured
#: ~1.8 MB through the projection, accepted. A field must satisfy BOTH: at most
#: `FIELD_MAX` characters AND at most twice that in UTF-8 bytes. The factor is 2
#: rather than 1 because Latin-1-accented prose (`ção`) runs ~1.1 bytes/char and
#: must not be refused; CJK at 3 bytes/char is bounded to ~2/3 of the character
#: cap, which is stated rather than discovered.
FIELD_BYTE_FACTOR = 2

#: CARDINALITY, PER LIST FIELD — and it is enumerated off the DECLARED types
#: below, never a hand-kept list of "the list fields". `summary` is the one the
#: review measured: the triage prompt says *"EXACTLY TWO summary lines"* and the
#: projection enforced no count at all, so a row could carry a thousand of them.
#: An unlisted `_STRLIST` key gets `DEFAULT_LIST_MAX` rather than no bound —
#: the same posture `DEFAULT_FIELD_MAX` takes for length.
LIST_MAX = {"summary": 2, "draft.placeholders": 10}
DEFAULT_LIST_MAX = 10
PLACEHOLDER_CAP = LIST_MAX["draft.placeholders"]

#: AN UNKNOWN KEY'S NAME IS NEVER PERSISTED — only a digest of it. The row
#: itself is projected clean, but the counter is copied into `judgment.json`
#: through `run_facts.grounding.projection`, and a model told to do so can emit
#: an object key that IS its grounding block:
#:
#:     {"the board minute records ACQUISITION OF NORTHWIND AT 4.2X EBITDA": 1}
#:
#: The first fix quoted a name only when it was "shaped like a key", and the
#: review broke it in one line: `zorbulax_quintiv_pellagrin_thornwick_MNPICANARY`
#: is shaped exactly like a key and landed verbatim in `projection.json`. Any
#: rule of the form "this shape is safe prose" is the same defect wearing a
#: different regex — an identifier is a perfectly good carrier. So NOTHING
#: attacker-authored is written: the bucket is a truncated sha256, which keeps
#: the ONE diagnostic the counter is for (how many DISTINCT keys a leg invented,
#: and whether the same one recurs) and carries no text at all. The HOST-written
#: namespace prefix (`draft.`, `evidence_span.`) is kept because the host wrote
#: it. `dropped_unknown_key_names` is deliberately not a field: an operator who
#: needs the name reads the 0600 envelope inside the 0700 run directory.
_KEY_DIGEST_LEN = 12


def _key_bucket(name: str) -> str:
    return hashlib.sha256(str(name).encode("utf-8")).hexdigest()[:_KEY_DIGEST_LEN]


def _count(bucket: dict[str, int], key: str, ns: str = "") -> None:
    name = ns + _key_bucket(key)
    bucket[name] = bucket.get(name, 0) + 1


def field_bytes(value: str) -> int:
    """The EXACT bytes this field costs the answer file, escapes included.

    THE BYTES THAT REACH DISK, not the bytes of the Python string (review
    2026-08-15, HIGH). The previous round measured `len(value.encode("utf-8"))`
    per field while only the aggregate row measured `json.dumps(...)`, and JSON
    escaping is where the two diverge by 6x: `json.dumps("\\x00")` is `"\\u0000"`,
    so a 600-character NUL string measured 600 bytes against a 1,200-byte
    ceiling and serialised to 3,602. Quotes, backslashes, newlines and every
    other control character expand the same way. `ensure_ascii=False` is the
    setting `--out` writes with, so this is the same serializer, not a proxy
    for it — and the enclosing quotes are counted because they are written too.
    """
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _too_long(key: str, value: Any) -> bool:
    """Over EITHER ceiling — characters, or SERIALIZED bytes at
    `FIELD_BYTE_FACTOR`x.

    Requirement (d)'s output half: the character caps were sized by an arithmetic
    that assumes ~4 chars/token, which is false for CJK, so a row obeying every
    character cap could still be several times the token budget those caps exist
    to hold.
    """
    if not isinstance(value, str):
        return False
    limit = FIELD_MAX.get(key, DEFAULT_FIELD_MAX)
    return len(value) > limit or field_bytes(value) > limit * FIELD_BYTE_FACTOR


def _too_many(key: str, value: Any) -> bool:
    """Over the CARDINALITY cap for a list field (requirement (d))."""
    return (isinstance(value, list)
            and len(value) > LIST_MAX.get(key, DEFAULT_LIST_MAX))


def _typed(declared: str, value: Any) -> bool:
    """Does `value` match its DECLARED type? `None` always does — an absent
    answer is a legitimate answer for every one of these keys."""
    if value is None:
        return True
    if declared == _STR:
        return isinstance(value, str)
    if declared == _BOOL:
        return isinstance(value, bool)
    if declared == _STRLIST:
        return isinstance(value, list) and all(isinstance(x, str) for x in value)
    return False


def _strings(row: dict[str, Any]) -> list[tuple[str, str]]:
    """(label, text) for EVERY string the projected row keeps. No exceptions.

    This is what replaced the hand-maintained `FREE_TEXT` tuple: the overlap
    rule is applied to everything, so a key added next year is covered by
    default rather than by whoever remembers to extend a list.

    `conversation_id` USED TO BE SKIPPED HERE — "host-joined, and it is in the
    batch anyway" — and the review carried
    `the board minute records NORTHWIND AT 4.2X EBITDA now` through the skip into
    `verdicts.json` with zero overlap flagged. The comment was true about where
    the id COMES from and false about what the field can CARRY. There is now no
    `continue` in this loop, and `test_every_declared_string_field_in_the_schema
    _is_overlap_tested` enumerates `ALLOWED_KEYS`/`DRAFT_KEYS` rather than
    listing them, so the claim "every retained string is tested" is proved by
    the schema instead of asserted in prose. Binding the id to the host's
    enumerated set (`project`'s `enumerated` argument) is the second half: an id
    the host never enumerated is not an id, whatever it says.
    """
    out: list[tuple[str, str]] = []
    for key, value in row.items():
        if key == "draft" and isinstance(value, dict):
            for k, v in value.items():
                if isinstance(v, str):
                    out.append((f"draft.{k}", v))
                elif isinstance(v, list):
                    out += [(f"draft.{k}", x) for x in v if isinstance(x, str)]
        elif isinstance(value, str):
            out.append((key, value))
        elif isinstance(value, list):
            out += [(key, x) for x in value if isinstance(x, str)]
    return out


def project_keys(row: dict[str, Any], allowed: frozenset[str],
                 stats: dict[str, Any]) -> dict[str, Any] | None:
    """One row's key walk, projected onto `allowed`. `None` means REFUSED.

    This is the loop half of `cos_model_answer.project_row`; the overlap test
    and the row-size bound stay there, reading the parent's own patchable
    `MAX_ROW_BYTES` at call time.
    """
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key not in allowed:
            _count(stats["dropped_unknown_keys"], key)
            continue
        if key == "draft":
            sub = project_draft_value(value, stats)
            if sub is None:
                return None
            out[key] = sub
        elif key == "evidence_span":
            if value is None:
                out[key] = None
                continue
            span = project_span_value(value, stats)
            if span is None:
                return None
            out[key] = span
        else:
            # TYPE FIRST, THEN LENGTH. A value of the wrong type is refused
            # rather than coerced — the review's dict-in-a-string-field case
            # passed every length and overlap check precisely because neither
            # of them applies to a dict.
            if not _typed(KEY_TYPES.get(key, _STR), value):
                stats["refused_shape"] += 1
                return None
            values = value if isinstance(value, list) else [value]
            if _too_many(key, value) or any(_too_long(key, x) for x in values):
                stats["refused_oversize_field"][key] = \
                    stats["refused_oversize_field"].get(key, 0) + 1
                return None
            out[key] = value
    return out


def project_draft_value(value: Any, stats: dict[str, Any]) -> dict[str, Any] | None:
    """The `draft` key's sub-object, admitted key by key. `None` refuses the row."""
    if not isinstance(value, dict):
        stats["refused_shape"] += 1
        return None
    sub: dict[str, Any] = {}
    for k, v in value.items():
        if k not in DRAFT_KEYS:
            _count(stats["dropped_unknown_keys"], k, "draft.")
            continue
        if not _typed(DRAFT_TYPES.get(k, _STR), v):
            stats["refused_shape"] += 1
            return None
        vals = v if isinstance(v, list) else [v]
        if _too_many(f"draft.{k}", v) \
                or any(_too_long(f"draft.{k}", x) for x in vals):
            stats["refused_oversize_field"][f"draft.{k}"] = \
                stats["refused_oversize_field"].get(f"draft.{k}", 0) + 1
            return None
        sub[k] = v
    return sub


def project_span_value(value: Any, stats: dict[str, Any]) -> dict[str, Any] | None:
    """The `evidence_span` key's offsets pair. `None` refuses the row."""
    if not isinstance(value, dict):
        stats["refused_shape"] += 1
        return None
    span = {}
    for k, v in value.items():
        if k not in SPAN_KEYS:
            _count(stats["dropped_unknown_keys"], k, "evidence_span.")
            continue
        if not isinstance(v, int) or isinstance(v, bool):
            stats["refused_shape"] += 1
            return None
        span[k] = v
    return span
