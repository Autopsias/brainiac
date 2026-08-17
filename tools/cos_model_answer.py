#!/usr/bin/env python3
"""Take an untrusted model leg's answer off STDOUT and write it as trusted code.

WHY THIS EXISTS (review 2026-08-13, round 5, C2). Both nightly model legs used
to hold the `Write` tool and write their own answer file, from a working
directory inside this repository. Both legs read attacker-controlled mail text.
So an injected judge could write `tools/cos_mutate.py` (executed for plan,
rehearsal and apply the SAME night), `overlay/cos/auto-archive.md` (the kill
switch, read after both legs) or `overlay/cos/ingest.md` (the taxonomy the gate
resolves `never` against) — defeating the frozen plan, the allowlist and the
lane lock in one in-cwd edit. Removing Bash closed the shell; `Write` was the
residual, and it was measured open:

    _evidence/s09/write-scope-probe.txt, arm S0 — with the shipped
    `--tools "Read,Write,Glob"`, a leg whose cwd was the EVIDENCE directory
    wrote a file into `<repo>/tools/` anyway. Working directory scopes nothing.

The fix is the lazy one: the untrusted leg does not get a writable filesystem at
all. It prints its answer, and THIS module — trusted host code, no model, no
network — parses that answer and writes the file. `--tools "Read,Glob"` removes
the tool and a blanket `Edit(//**)` deny removes every file-editing tool behind
it (measured: the deny is ABSOLUTE, it beat a narrower allow for the same
path, which is why there is no "writable evidence dir" variant of this design).

    "$CLAUDE_BIN" -p … | python3 tools/cos_model_answer.py --envelope - \
        --out <answer.json> --batches-dir <chunk>

THE LEG'S OUTPUT IS PIPED, NOT PERSISTED (review 2026-08-15, CRITICAL). It used
to be redirected into `$CHUNK/verdicts.envelope.json` first, so a file carrying
model-authored keys and values VERBATIM sat on disk before the projection ever
ran — the projection cannot precede a file this module never writes. `--envelope
-` reads the leg's stdout straight off a pipe, bounded by `MAX_ENVELOPE_BYTES`
before anything is decoded.

SAID EXACTLY (corrected 2026-08-15 — the sentence here read "NOTHING
model-authored is persisted ON THIS MODULE'S PATHS", which a one-row run
falsifies: `--out` carries a projected row's `triage_evidence` verbatim, and the
canary's own `test_a_well_formed_answer_leaves_a_clean_log_too` asserts it does).
What this module closes is the SHAPE, not the words: the answer file's KEY SET is
the batch's own, unknown keys are dropped and counted, ids are bound to the host
enumeration, and every ENVELOPE METADATA value is projected onto a closed host
vocabulary. The VALUES of the allowlisted free-text keys are the model's answer —
that is what an answer is — and they are contained by the sink table (D14), not by
this module. The FAILURE path is the one that persists nothing model-authored: it
writes `parse-failure.json` beside `--out`, carrying only host-authored counts and
a sha256 of what arrived. A path is still accepted
(`--envelope <file>`) because the tests feed fixtures that way; the nightly
pipes. THE ONE EXCEPTION IN THE PIPELINE is the nightly's own
`$CHUNK/leg.stderr` — the CLI's diagnostics, allowlisted and named at D14 sink
4c, and not written by this module at all.

AND NOTHING MODEL-AUTHORED IS EVER PRINTED — TEXT OR METADATA. Every refusal
below used to interpolate `text[:80]` (or the CLI result string) into its
sentence, and the nightly redirects this module's stderr into `$LOG` — a file
outside `$EV`, so outside the run directory's `0700`. Failure sentences now carry
`_describe()`'s host-authored digest and byte/line counts instead. The round that
shipped that stopped at the TEXT, leaving `subtype`, `stop_reason` and
`num_turns` interpolated verbatim; they are projected onto closed host
vocabularies too (`project_envelope_field`). `$LOG` is D14's sink 14 and the
canary scans it.

Exit 0 wrote the file; exit 1 did not, and says why in one sentence. Every
failure is the caller's existing survivable path — no `--categories` for the
category leg, exit 9 (READ-ONLY night) for the judgment leg — because a leg
that produced no parseable answer is the leg that produced no answer file, which
is the case both callers already handle.

TWO OUTPUT SHAPES, AUTO-DETECTED (STREAM-01, 2026-08-14). The original legs ran
`--output-format json`, which writes ONE envelope whose `result` field is only
the model's FINAL message. At real mailbox scale (~250 conversations) the
answer exceeds the 64000-token single-message output cap and spans many turns
(6 and 15 measured on run 131), so `result` is a TAIL FRAGMENT
("Continuing the array from entry 179: …") and everything before it is lost —
the category gate then reads nothing and the judgment leg dies. The legs now run
`--output-format stream-json --verbose`, which writes one JSON EVENT per line;
the full answer is reassembled by concatenating every `assistant` event's text
across ALL turns, and the terminal `result` event carries `is_error` /
`num_turns` / `permission_denials`. The legs are asked for a single JSON array,
continued across as many messages as it takes; `extract_objects` below rebuilds
the rows from the concatenated text whether the model emits one array, several
array fragments, or bare objects, skipping the `Continuing the array …` prose it
injects at a turn boundary. The legacy single-envelope path is still read
(auto-detected), so a
captured envelope from before this change still parses.

A FILE RATHER THAN INLINE SHELL, for the reason `cos_envelope_life.py` is one:
a parser sitting on the untrusted boundary that cannot be executed by a test
without slicing the shell script is a parser nothing can prove.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# D14 · the CLOSED verdict schema, and the grounding-overlap refusal
# ---------------------------------------------------------------------------
# GROUNDING REACHES THE VERDICT SINKS BEFORE ANYTHING VALIDATES. This module
# preserved every key the model emitted — `extract_objects` appends whole objects
# and `--out` serialized them as-is — and it writes `$CHUNK/verdicts.json` BEFORE
# `cos_judge.py --judge` ever runs. So a model that echoed its grounding block
# into a `notes` key, or into any key nobody enumerated, had already written MNPI
# to disk by the time the validator saw it. The projection below runs BEFORE the
# ANSWER file is written.
#
# AND NOTHING NOW PRECEDES IT (review 2026-08-15, CRITICAL). The previous round
# classified the raw stream envelopes as "unprojected sensitive evidence" and
# allowlisted them instead of closing the hole; the re-review found a SECOND
# unprojected channel beside them (parser errors interpolating model text into
# `$LOG`). Both are gone: the leg's stdout arrives on a PIPE and is never
# written, and no failure sentence carries model-authored text. There is no
# design sink 4 any more.
#
# The key sets are read off the four batch prompts' own `ANSWER with a JSON
# array` shapes (`tools/cos_judge.py`). An unknown key at ANY depth is dropped
# and counted; it is never carried.
#: TWO SCHEMAS, AND THE CATEGORY LEG IS THE OTHER ONE. It runs PRE-DRAW, before
#: `cos_ground.py` has run at all (which is the whole ordering argument for sink
#: 12), and its batch asks for exactly one key beside the id. Projecting it onto
#: the judgment table would drop `category` and disarm the gate on every night —
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

#: EVERY STRING THE PROJECTION KEEPS IS OVERLAP-TESTED, and the hand-maintained
#: `FREE_TEXT` tuple that used to name seven of them is DELETED. It is the same
#: defect shape as the `--redact` denylist this delta already inverted: the
#: review found two keys the tuple had missed — `merge_candidate`, which
#: `cos_judge.py` interpolates straight into the briefing HTML, and
#: `draft.recipients_scope`, whose value is echoed back inside a validation
#: violation that is persisted to `judgment.json`. A list of "the text fields"
#: is a list somebody has to keep correct forever; "every string" is not.
#:
#: It costs nothing on the closed-vocabulary fields: `bucket`, `tier`,
#: `dedup_check` and friends are under five tokens, so they yield no shingles
#: and the rule cannot fire on them.

#: THE OUTPUT-BEARING BOUND, and it is a DIFFERENT control from the input budget
#: (adversarial review 2026-08-14). Runs 131-133 died on the model's SINGLE-
#: MESSAGE OUTPUT cap (64000 tokens — see this file's own header), and a per-row
#: INPUT character budget cannot bound that. These are the per-field ceilings the
#: batch prompts state and this projection enforces, in characters:
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

#: AGGREGATE BOUNDS, because per-field limits bound no TOTAL. A prompt-injected
#: leg can emit valid duplicate objects until the disk, the merge and the OWA
#: bearer window are all spent. All of these are fail-closed: over the bound the
#: chunk produces no answer file (or the row is refused), which is the caller's
#: existing survivable path — its rows go unjudged and the H4 coverage floor is
#: the backstop.
MAX_PARSED_ROWS = 2000                      # 40x a 50-conversation chunk
MAX_ENVELOPE_BYTES = 16 * 1024 * 1024       # the largest real envelope: 486 KB
#: THE OUTPUT SCHEMA BUDGET, which `MAX_ENVELOPE_BYTES` is not: that one is a
#: whole-file PRE-READ limit on what the leg emitted, and requirement (d) is
#: about what the projection is willing to WRITE.
#:
#: ITS SIZE IS DERIVED, AND THE PREVIOUS ROUND'S ARITHMETIC WAS WRONG BY THREE-
#: FOLD (review 2026-08-15). That round recorded *"the worst legal row is ~11 KB
#: against a 32 KiB ceiling — so on today's schema it cannot fire"*, computed on
#: 3-byte CJK values and ignoring JSON escape expansion entirely. Two reviewers
#: measured otherwise and both were right: ~31.5 KB against 32,768 rather than
#: ~11 KB, and a legal NUL-valued row serialising to 79,183 bytes that WAS
#: REFUSED — so the bound was firing on rows the field checks admitted.
#:
#: RE-MEASURED HERE against the fixed per-field measure (`_too_long` now reads
#: serialised bytes), over every filler that escapes differently:
#:
#:     ascii 16,642 · cjk 32,668 · NUL 32,620 · accented/quote/newline/
#:     backslash/tab 32,710   — worst 32,710 bytes
#:
#: Against the old 32,768 ceiling that is 99.8 % — 0.2 % headroom, one field
#: widening from refusing legal rows every night.
#:
#: So the row bound is now sized ABOVE the worst case the field caps really
#: admit, with a stated margin (32,710 / 65,536 = 0.499), and
#: `test_the_row_bound_clears_the_derived_worst
#: _legal_row_with_margin` DERIVES that worst case from the schema (serialised,
#: escape expansion included) and fails if the margin falls below
#: `MAX_ROW_MARGIN`. A widened `FIELD_MAX` therefore breaks the TEST while
#: production still has room, which is what a regression backstop is for; the
#: same test proves the bound can still fire by lowering it under the derived
#: worst case. Requirement (d)'s per-field half now measures serialised bytes
#: too (`_too_long`), which is what makes the derivation honest.
MAX_ROW_BYTES = 64 * 1024
#: The fraction of `MAX_ROW_BYTES` the derived worst legal row may occupy. At
#: 0.75 a schema widening of a third trips the test with the production bound
#: still clear — room to react rather than a bound that fires in the morning.
MAX_ROW_MARGIN = 0.75
MAX_ANSWER_BYTES = 4 * 1024 * 1024

#: D14's overlap rule: shingle width 5 tokens — the same width `brain`'s own
#: document-identity primitive uses (ENF-03), so the engine keeps ONE notion of
#: "the same text". A field of fewer than 5 tokens yields no shingles and CANNOT
#: BE JUDGED; that limit is stated rather than papered over, and such a field is
#: too short to carry a meaningful quotation.
SHINGLE_W = 5


#: THE DOTLESS-I PAIR, which no Unicode normalization form folds (review
#: 2026-08-15). `casefold()` maps `İ` to `i` + U+0307 but leaves Turkish `ı`
#: its own letter, so `the board ınfo details now` shares NO five-token shingle
#: with its plain spelling — a visually identical quotation that walks past the
#: refusal. It is translated explicitly; the combining-mark strip below then
#: closes the `İ` half and every accent confusable with it.
_DOTLESS_I = str.maketrans({"ı": "i", "ȷ": "j"})


def _fold(text: str) -> str:
    """One folding, used on BOTH sides of every comparison.

    NFKC, NOT NFC — a five-token grounding phrase re-rendered in FULLWIDTH
    characters is visually the same text and tokenizes differently under NFC, so
    `overlap_hit` returned False on a verbatim quotation. Then casefold, then the
    dotless-i translation, then NFKD with every combining mark dropped.

    THE MARK STRIP WIDENS THE DETECTOR, deliberately and in the fail-closed
    direction: `résumé` and `resume` now share a shingle, so a row that would
    have passed can now be REFUSED. A refusal costs one verdict and is visible in
    `refused_grounding_overlap`; a missed quotation is MNPI at rest in a sink. It
    never widens the other way — nothing that was refused becomes accepted.
    """
    s = unicodedata.normalize("NFKC", str(text or "")).casefold()
    s = s.translate(_DOTLESS_I)
    return "".join(ch for ch in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(ch))


def _norm_tokens(text: str) -> list[str]:
    """Fold, then every run of non-alphanumerics to one space, strip.
    BOTH SIDES are normalized identically or the comparison means nothing."""
    # `\W` is unicode-aware for `str` patterns, so an accented or CJK token
    # survives; `_` joins the separator class because it is not alphanumeric.
    return [t for t in re.split(r"[\W_]+", _fold(text)) if t]


def shingles(text: str, w: int = SHINGLE_W) -> set[tuple[str, ...]]:
    toks = _norm_tokens(text)
    return {tuple(toks[i:i + w]) for i in range(len(toks) - w + 1)}


def block_shingles(block_text: str, own_row_text: str,
                   w: int = SHINGLE_W) -> set[tuple[str, ...]]:
    """The shingles UNIQUE to this conversation's grounding block.

    Step 4 of the rule, and it is what stops the whole thing firing on a verdict
    legitimately quoting the subject line: subtract everything the block shares
    with the conversation's OWN batch row (its `subject`, `sender` and, for
    staging, its `text`). Only what is unique to the vault block counts.

    `w` is `SHINGLE_W` (5) for the REFUSAL rule and nothing else changes it. The
    counter-direction signal (`USE_SHINGLE_W`, below) passes 2 — one function,
    one tokenizer, one subtraction, two widths.
    """
    return shingles(block_text, w) - shingles(own_row_text, w)


#: THE WIDTH THE *USE* SIGNAL READS AT, and it is 2 rather than 1 on measured
#: grounds, not taste. At width 1 the "unique to the block" set still contains
#: function words the row's own text happened not to use — a verdict reading
#: "confirm the tender position by Friday" matched a block on the word `by`, so
#: a row that ignored the vault entirely scored as having used it (probed:
#: `test_the_run_facts_count_the_rows_that_USED_their_block_not_only_delivery`).
#: A two-token run is the shortest thing that carries a phrase rather than a
#: word. It stays well under `SHINGLE_W`, because this is a floor on USE, not
#: the refusal rule's evidence of QUOTATION.
USE_SHINGLE_W = 2


def overlap_hit(field_text: str, uniq: set[tuple[str, ...]]) -> bool:
    """THRESHOLD ONE. If ANY shingle unique to the block occurs in the field, the
    row is refused — there is no percentage to calibrate and no free parameter to
    drift. A five-word verbatim run out of host-written vault prose the model was
    never asked to reproduce is not coincidence, and the legitimate quoting path
    is the mail BODY, which the subtraction above has already excluded."""
    return bool(uniq) and bool(shingles(field_text) & uniq)

#: A fenced block the model wrapped its answer in. The prompts ask for a bare
#: array; this is tolerated because the alternative to tolerating it is losing a
#: whole night's judgment to three backticks, and stripping a fence cannot
#: change what the JSON inside it says.
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def _describe(text: str) -> str:
    """What arrived, said in the HOST's own words — never in the model's.

    Every refusal in this module used to quote `text[:80]`, and the nightly
    redirects this module's stderr into `$LOG`, which sits outside `$EV` and so
    outside the run directory's `0700` (review 2026-08-15, CRITICAL: the second
    unprojected channel). An operator debugging a parse failure needs to tell
    "empty", "prose", "truncated JSON" and "yesterday's answer again" apart, and
    a length + line count + digest does all four without carrying one character
    the model wrote. The digest is stable across runs, so a recurring failure is
    recognisable as the same one.
    """
    b = text.encode("utf-8", "replace")
    return (f"{len(b)} byte(s), {text.count(chr(10)) + 1} line(s), "
            f"sha256:{hashlib.sha256(b).hexdigest()[:16]}")


# ---------------------------------------------------------------------------
# THE ENVELOPE'S METADATA IS PROJECTED TOO (review 2026-08-15, CRITICAL)
# ---------------------------------------------------------------------------
# `_describe` closed the model's own TEXT and the round stopped there, three
# lines above a refusal that interpolated `result.get('subtype')` verbatim — the
# comment there literally said "the text is described, never carried" while
# carrying the metadata beside it. A reviewer's probe put `MODEL_CANARY_SUBTYPE`
# and `MODEL_CANARY_STOP` into refusal text; this file's own probe then found a
# fifth, worse one: `num_turns` is interpolated into the SUCCESS note, which the
# nightly appends to `$LOG` on every healthy night, not only on a failure.
#
# So metadata is projected exactly as row keys are: onto a CLOSED, HOST-OWNED
# vocabulary, with anything outside it replaced by one constant token. The
# vocabularies below were read off the shipped `claude` binary (2.1.233), not
# recalled — `strings`-grepped for `subtype:"…"` and `stop_reason …` literals.
# BEING INCOMPLETE IS SAFE AND STATED: a genuinely new CLI subtype prints
# `unrecognised`, which costs diagnosability and leaks nothing. The reverse
# default — quote it and hope — is what this closes.
#: The `result` event's own verdict word.
RESULT_SUBTYPES: frozenset[str] = frozenset({
    "success", "error", "error_during_execution", "error_max_turns",
    "error_max_budget_usd", "error_max_structured_output_retries"})
#: The API's stop reasons, as the CLI passes them through.
STOP_REASONS: frozenset[str] = frozenset({
    "end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn",
    "refusal", "model_context_window_exceeded"})
#: What a projected field says when the value is not in its vocabulary. ONE
#: constant, so an operator reading `$LOG` can tell "the CLI said something we
#: do not know" from "the CLI said nothing".
UNRECOGNISED = "unrecognised"
#: A turn count is a small non-negative integer or it is not a turn count. The
#: nightly's own ceiling is `COS_MAX_TURNS` (40); this is deliberately looser so
#: a raised ceiling does not start printing `unrecognised` on healthy nights.
MAX_REPORTED_TURNS = 1000
#: EVERY envelope key the refusal sentences and the success note read, and the
#: closed vocabulary each is projected onto. The enumerating test generates its
#: canary cases from THIS, so a key read by a path added later is untested only
#: until it is named here — and `test_every_envelope_key_the_parser_reads_is_
#: enumerated` reads the source to assert nothing else is read at all.
#: `permission_denials` is on the list and has no vocabulary: it is COUNTED
#: (`len`), never rendered, which is a projection by a different means and is
#: enumerated here so that claim is tested rather than assumed.
ENVELOPE_VOCAB: dict[str, frozenset[str]] = {
    "subtype": RESULT_SUBTYPES,
    "stop_reason": STOP_REASONS,
    "num_turns": frozenset(str(n) for n in range(MAX_REPORTED_TURNS + 1)),
}
ENVELOPE_METADATA: frozenset[str] = frozenset(ENVELOPE_VOCAB) | {
    "permission_denials"}


def project_envelope_field(env: dict[str, Any], field: str) -> str:
    """One envelope metadata value, said in the HOST's own vocabulary.

    `absent` and `unrecognised` are host words, and so is every value that
    survives: a returned token is a member of `ENVELOPE_VOCAB[field]`, which no
    model authored. Booleans are excluded from the integer path deliberately —
    `True in {0, 1, …}` is true in Python, and `str(True)` would put the model's
    choice of shape into the sentence.
    """
    if field not in env:
        return "absent"
    value = env[field]
    if isinstance(value, str):
        token: str | None = value
    elif isinstance(value, int) and not isinstance(value, bool):
        token = str(value)
    else:
        token = None
    return token if token in ENVELOPE_VOCAB[field] else UNRECOGNISED


class RowCeiling(ValueError):
    """The row ceiling, as its own type so a bracket-scan retry cannot swallow it.

    `extract_answer`'s fallback scan catches `ValueError` to mean *"this `[` was
    not the array, try the next one"*. Once the ceiling is raised from INSIDE the
    element loop (below), a plain `ValueError` would be caught by that handler
    and the refusal would silently become "keep looking".
    """


def _row_ceiling(n: int) -> None:
    """ONE ceiling, applied by EVERY parser path (review 2026-08-15, HIGH).

    The bound used to live only inside `extract_objects`, and
    `answer_from_envelope` auto-detects TWO shapes: the legacy single envelope
    runs `extract_answer` first and took 2,001 rows. `test_every_parser_entry_
    path_enforces_the_row_ceiling` enumerates the `_answer_from_*` functions
    rather than naming them, so a third shape added later fails until it counts.

    FAIL CLOSED, not truncate: a truncated answer is a short batch wearing a full
    one's row count, and the caller's no-answer path already exists.
    """
    if n > MAX_PARSED_ROWS:
        raise RowCeiling(
            f"the leg emitted {n} objects, past the {MAX_PARSED_ROWS}-row "
            "ceiling for one chunk — a leg that emits without bound is "
            "spending the mutation window, not judging")


_JSON_WS = " \t\n\r"


def _decode_array(dec: json.JSONDecoder, text: str, start: int,
                  already: int = 0) -> tuple[list[Any], int]:
    """The JSON array at `text[start]`, decoded ONE ELEMENT AT A TIME.

    WHY IT IS NOT `raw_decode` (review 2026-08-15, HIGH). Both array paths used
    to hand the WHOLE array to `raw_decode` and call `_row_ceiling(len(doc))`
    afterwards, so a 16 MB envelope and every Python object in it was fully
    materialised before anything refused the row count — the pre-append bound
    the previous round recorded as shipped was not implemented. This walks the
    array's own grammar and asks the ceiling BEFORE each element is retained, so
    the 2,001st element is never decoded at all. `already` is the count the
    caller has retained outside this array (`extract_objects` scans several).

    STRICT ON SEPARATORS on purpose: `raw_decode` would have refused `[1 2]` and
    `[1,,2]`, so this refuses them too rather than quietly widening what the
    untrusted boundary accepts in exchange for an incremental bound.
    """
    n = len(text)
    i = start + 1                       # past the '['
    out: list[Any] = []
    expect_value = True                 # true after '[' and after every ','
    while True:
        while i < n and text[i] in _JSON_WS:
            i += 1
        if i >= n:
            raise ValueError("the leg's JSON array is unterminated")
        ch = text[i]
        if ch == "]":
            if expect_value and out:
                raise ValueError("the leg's JSON array ends on a comma")
            return out, i + 1
        if ch == ",":
            if expect_value:
                raise ValueError("the leg's JSON array has an empty element")
            expect_value = True
            i += 1
            continue
        if not expect_value:
            raise ValueError(
                "the leg's JSON array has two values with no comma between them")
        _row_ceiling(already + len(out) + 1)
        val, i = dec.raw_decode(text, i)
        out.append(val)
        expect_value = False


def extract_answer(result: str) -> list[Any]:
    """The JSON ARRAY the leg was asked for, or ValueError saying what came back.

    An array, specifically. Both prompts ask for "a single JSON array, one
    object per conversation_id", and both consumers (`cos_driver.load_categories`
    and `cos_judge`) REFUSE anything else — `load_categories` documents exactly
    why: a truncated or abandoned run leaves a single OBJECT behind, and reading
    its keys as conversation ids stamps threads that do not exist. Refusing the
    wrong shape HERE gives the same refusal one step earlier, with a sentence
    naming what actually arrived.
    """
    text = (result or "").strip()
    if not text:
        raise ValueError("the leg's final message was empty")
    m = _FENCE.match(text)
    if m:
        text = m.group(1).strip()
    # A LEADING ARRAY IS DECODED INCREMENTALLY (`_decode_array`), so the row
    # ceiling is applied per element rather than after the whole thing is built.
    # A trailing sentence after the array — the common shape — costs nothing,
    # because the decode stops at the closing bracket.
    dec = json.JSONDecoder()
    doc: Any = None
    if text[:1] == "[":
        try:
            doc, _end = _decode_array(dec, text, 0)
            return doc
        except RowCeiling:
            raise
        except ValueError:
            doc = None          # a broken leading array still reaches the scan
    else:
        # NOT an array at the front. Name what it IS, so a bare object reaches
        # the "not the array" refusal below rather than being mined for whatever
        # array happens to sit inside it. This decode is bounded by
        # `MAX_ENVELOPE_BYTES` and produces no rows on any path — the row
        # ceiling is about ROWS, and a non-array leading value has none.
        try:
            doc, _end = dec.raw_decode(text)
        except ValueError:
            doc = None
    if doc is None:
        # No leading array. Scan each `[` in turn rather than giving up on the
        # FIRST one: a legitimate answer can be preceded by prose that itself
        # carries a bracket ("[see note] ... [{…}]"), and the first `[` being
        # unparseable does not mean the real array is not further along. The
        # first array that parses still wins (unchanged); fail-closed if none
        # does.
        start = text.find("[")
        if start < 0:
            raise ValueError(
                "the leg answered with no JSON array at all "
                f"({_describe(text)})") from None
        last_exc: ValueError | None = None
        while start >= 0:
            try:
                # THE SLICE STAYS, and that is a MEASURED call (round 7). The
                # review asked for `raw_decode(text, start)` on the grounds that
                # the slice copies the tail per candidate bracket and so costs
                # O(n²). The copy does — but the index form is not O(n): every
                # failed candidate raises `JSONDecodeError`, whose constructor
                # computes line/column by scanning `s` from position 0 to the
                # error offset, which is ALSO O(n) per candidate. Both forms are
                # quadratic; only the constant differs, and it differs the wrong
                # way. Measured here on `"[x " * n` + a real array
                # (`_evidence/s09/known-positive-probes.md`):
                #
                #     90 KB   slice 0.059 s   index  1.098 s
                #    180 KB   slice 0.180 s   index  4.303 s
                #    360 KB   slice 0.584 s   index 17.101 s
                #
                # 4x per doubling on both legs — the shape is unchanged — and
                # the index form is ~30x slower. It was applied, measured, and
                # reverted rather than shipped on the reasoning. `_decode_array`
                # takes the slice for the same reason: a candidate bracket that
                # is not an array fails on its FIRST element, which is a
                # JSONDecodeError and so carries that same O(n) offset scan.
                doc, _end = _decode_array(dec, text[start:], 0)
                break
            except RowCeiling:
                raise
            except ValueError as exc:
                last_exc = exc
                start = text.find("[", start + 1)
        if doc is None:
            raise ValueError(
                f"the leg's answer is not parseable JSON ({last_exc}) "
                f"({_describe(text)})") from last_exc
    if not isinstance(doc, list):
        raise ValueError(
            f"the leg answered with a JSON {type(doc).__name__}, not the array "
            "of rows the batch asks for. A single object is what a truncated or "
            "abandoned run leaves behind, and reading its keys as conversation "
            "ids is how a run stamps threads that do not exist")
    _row_ceiling(len(doc))
    return doc


#: H6 (Codex MEDIUM, quadratic DoS). Every FAILED `{`/`[` candidate raises a
#: JSONDecodeError whose line/column is computed by scanning from offset 0, so a
#: large text sprayed with stray brackets makes the forward scan ~quadratic. A
#: valid answer has ~zero decode failures; this caps them at a generous ceiling
#: and fails closed past it — an answer that pins the parser is not a usable
#: answer. NOT a cap on successes: a real 250-row array must scan freely.
_MAX_SCAN_FAILURES = 50000


def _next_bracket(text: str, i: int) -> int:
    """The index of the next `{` or `[` at/after `i`, or -1 if neither remains."""
    a = text.find("{", i)
    b = text.find("[", i)
    if a < 0:
        return b
    if b < 0:
        return a
    return a if a < b else b


def extract_objects(text: str) -> list[Any]:
    """Every top-level JSON object the stream leg emitted, in order.

    The stream leg is asked for a single JSON array, but this tolerates that
    array arriving in fragments, as bare objects, with comma separators, code
    fences or prose lines between objects, because a turn that ran out of room
    resumes with whatever the model chose to type ("Continuing the array from
    entry 179:" — run 131). Forward-scan to the next `{` or `[`, decode ONE value
    there, jump past it; a whole array that parses contributes its object
    elements, and once it is broken across a turn boundary each surviving object
    is still picked up on its own `{`. Everything else — prose, commas, fences,
    blank lines — sits between brackets and is skipped. Fail closed with a
    sentence if nothing parses, which is the leg's survivable no-answer path.
    """
    dec = json.JSONDecoder()
    out: list[Any] = []
    i, n = 0, len(text)
    failures = 0
    while i < n:
        j = _next_bracket(text, i)
        if j < 0:
            break
        try:
            # AN ARRAY IS WALKED ELEMENT BY ELEMENT (`_decode_array`), never
            # handed whole to `raw_decode`: that is the same after-the-fact
            # materialisation the review found on this path (2026-08-15, HIGH),
            # and the stream leg's normal shape IS one big array.
            if text[j] == "[":
                val, end = _decode_array(dec, text, j, already=len(out))
            else:
                # Decode IN PLACE from index `j` rather than slicing `text[j:]`.
                # This scan is SUCCESS-dominated — the array lands one decodable
                # object per forward step — so the index form is O(1) per object
                # where a per-object slice copies the tail. (That is the OPPOSITE
                # trade-off from the array scan in `extract_answer`, which keeps
                # its slice: that scan is FAILURE-dominated, and there
                # JSONDecodeError's own O(n) offset computation dominates either
                # form — measured in its comment.)
                val, end = dec.raw_decode(text, j)
        except RowCeiling:
            raise
        except ValueError:
            failures += 1
            if failures > _MAX_SCAN_FAILURES:
                # H6: a brace-heavy text that never decodes is a DoS, not an
                # answer. Stop scanning and fail closed rather than churn.
                raise ValueError(
                    f"the leg's stream defeated the object scanner after "
                    f"{failures} unparseable brackets — an answer that pins "
                    "the parser is not a usable answer") from None
            i = j + 1
            continue
        i = end
        # SHAPE FILTER (R2, Codex re-review). Admit only objects that carry a
        # `conversation_id` — the key both consumers (`load_categories`,
        # `judge_night`) join on. An object without one cannot become anyone's
        # answer, so dropping it costs nothing and removes the accidental prose
        # object (a mail body's `{"timeout": 30}`) from the surface.
        #
        # STRUCTURAL RESIDUAL, stated not hidden: this cannot reject a
        # verdict-SHAPED object echoed from untrusted mail (one carrying a
        # `conversation_id`), because the multi-turn answer is itself broken by
        # the model's `Continuing the array …` prose at every turn boundary — so
        # "parse exactly one clean array" is not available without re-breaking the
        # STREAM-01 fix. And the model HOLDS the enumerated id while reading that
        # thread's untrusted body, so an injection CAN instruct it to copy the id
        # into a fabricated object — the id is not a secret at this layer. The
        # containment is downstream and layered, not "the id is unguessable":
        #   1. `judge_night` binds verdicts to the ENUMERATED set — an id the run
        #      never enumerated is dropped outright.
        #   2. A fabricated object for a REAL enumerated id is a DUPLICATE of that
        #      thread's genuine verdict; if they differ, H3 drops the cid to
        #      PENDING (fail-safe). The injection only survives if it also
        #      SUPPRESSES the genuine verdict — a strictly harder prompt injection.
        #   3. Any surviving verdict still passes `validate_verdict`'s closed
        #      vocabulary, and a staging/candidate verdict's `evidence_span` must
        #      land inside the REAL captured body — content it cannot forge blind.
        #   4. Whatever becomes a mutation still passes the frozen plan_binding,
        #      the rehearsal, and the mutation allowlist, and NOTHING sends: the
        #      worst reachable outcome is a reversible archive/categorise/draft.
        # The ACCEPTED residual is a sophisticated suppress-and-inject prompt
        # injection producing a reversible, non-send action on an enumerated
        # thread, on a nightly that is disarmed and whose next run is attended.
        # The attended live capture is what shows whether the model emits such
        # objects at all; completing containment further (verdict provenance) is
        # a downstream question, not a parser one.
        # INCREMENTALLY, BEFORE THE APPEND. The old check ran once at the end,
        # so a stream of a million objects was fully materialised in memory
        # before anything refused it (review 2026-08-15).
        if isinstance(val, dict):
            if "conversation_id" in val:
                _row_ceiling(len(out) + 1)
                out.append(val)
        elif isinstance(val, list):
            for x in val:
                if isinstance(x, dict) and "conversation_id" in x:
                    _row_ceiling(len(out) + 1)
                    out.append(x)
    if not out:
        raise ValueError(
            "the leg's stream carried no JSON object at all "
            f"({_describe(text)})")
    return out


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


def project_row(row: dict[str, Any], block_text: str | None,
                own_row_text: str, stats: dict[str, Any],
                allowed: frozenset[str] = ALLOWED_KEYS) -> dict[str, Any] | None:
    """One row, projected onto the closed schema. `None` means REFUSED.

    Refused, never truncated and never masked — the same posture
    `load_categories` takes toward a stray id. A refused row costs one verdict;
    a truncated one is a short answer wearing a full one's shape.
    """
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key not in allowed:
            _count(stats["dropped_unknown_keys"], key)
            continue
        if key == "draft":
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
            out[key] = sub
        elif key == "evidence_span":
            if value is None:
                out[key] = None
                continue
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

    # THE OVERLAP TEST, on the projected row and against THIS conversation's own
    # block only (rule 3). Cross-conversation matching would fire on boilerplate
    # and says less.
    if block_text:
        uniq = block_shingles(block_text, own_row_text)
        for label, text in _strings(out):
            if overlap_hit(text, uniq):
                stats["refused_grounding_overlap"][label] = \
                    stats["refused_grounding_overlap"].get(label, 0) + 1
                return None
    # THE ROW'S OWN SERIALIZED SIZE, last, on exactly the bytes that would be
    # written. Per-field ceilings bound no row: ~16 string fields at their caps
    # is still tens of kilobytes per row, and the row is what `--out` serializes.
    if len(json.dumps(out, ensure_ascii=False).encode("utf-8")) > MAX_ROW_BYTES:
        stats["refused_oversize_row"] += 1
        return None
    return out


def project(rows: list[Any], blocks: dict[str, Any],
            own_row_text: dict[str, str],
            allowed: frozenset[str] = ALLOWED_KEYS,
            *, enumerated: set[str]) -> tuple[list[Any], dict[str, Any]]:
    """The whole answer, projected. Counts everything it drops or refuses.

    `enumerated` is the HOST's own id set, read off the rendered batch files. A
    row whose `conversation_id` is not in it is REFUSED before projection: an id
    the host did not enumerate is not an id, it is model-authored text sitting in
    a field whose declared type happens to be `str`. `judge_night` already binds
    verdicts to the enumerated set, but that is two files downstream of
    `verdicts.json` — the leak the review measured was at the WRITE, not at the
    judgment.

    IT IS MANDATORY, AND KEYWORD-ONLY (review 2026-08-15, HIGH). The round that
    added it made it `enumerated: set[str] | None = None` with enforcement under
    `if enumerated is not None`, so the CRITICAL fix was INERT on every call that
    omitted it — both nightly legs happened to pass `--batches-dir`, which made
    it a latent fail-open rather than a live leak, and "a guard that is a no-op
    by default" is the exact shape this delta exists to remove. There is no
    accepted mode with no enumeration: an empty or missing set is a REFUSAL,
    because a chunk whose batches enumerate nothing has nothing to judge, and
    projecting its answer against an empty binding would admit every
    model-authored id instead of none.
    """
    if not enumerated:
        raise ValueError(
            "the host supplied no enumerated conversation id set — a projection "
            "with nothing to bind `conversation_id` against would keep whatever "
            "id the model wrote, which is the fail-open the binding exists to "
            "close")
    stats: dict[str, Any] = {"rows_in": len(rows), "rows_out": 0,
                             "dropped_unknown_keys": {},
                             "refused_grounding_overlap": {},
                             "refused_oversize_field": {},
                             "refused_oversize_row": 0,
                             "refused_unenumerated_id": 0,
                             "refused_shape": 0}
    out: list[Any] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("conversation_id") or "")
        if cid not in enumerated:
            stats["refused_unenumerated_id"] += 1
            continue
        entry = blocks.get(cid) or {}
        block_text = entry.get("text") if isinstance(entry, dict) else None
        projected = project_row(row, block_text, own_row_text.get(cid, ""), stats,
                                allowed)
        if projected is not None:
            out.append(projected)
    stats["rows_out"] = len(out)
    return out, stats


def own_row_texts(chunk_dir: Path) -> dict[str, str]:
    """Per conversation, the text of its OWN batch row — what step 4 subtracts.

    `subject`, `sender` and (staging/draft) `text`: the values a verdict may
    legitimately echo, and which appear on BOTH sides of the comparison.

    ITS KEY SET IS ALSO THE HOST ENUMERATION `project` binds ids against, which
    is why it globs `batch-*.md` rather than iterating `BATCH_TYPES`: the
    CATEGORY leg's chunk holds one `batch-category.md` and no judgment batch, so
    a `BATCH_TYPES` loop enumerated nothing there and the binding would have been
    silently inert on half the calls.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import cos_batch_chunk as cbc                                # noqa: PLC0415
    out: dict[str, list[str]] = {}
    for path in sorted(chunk_dir.glob("batch-*.md")):
        try:
            _h, rows = cbc.split_batch(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        for r in rows:
            if not isinstance(r, dict):
                continue
            cid = str(r.get("conversation_id") or "")
            if not cid:
                # An id-less batch row cannot enumerate anything, and admitting
                # `""` would let a row with no `conversation_id` bind.
                continue
            bucket = out.setdefault(cid, [])
            for key in ("subject", "sender", "text"):
                v = r.get(key)
                if isinstance(v, str):
                    bucket.append(v)
    return {cid: "\n".join(parts) for cid, parts in out.items()}


def _stream_events(text: str) -> list[dict[str, Any]] | None:
    """The stream-json events, or None if this is the legacy single envelope.

    `--output-format stream-json` writes one COMPLETE JSON event per line, each
    an object carrying a `type` (`system`, `assistant`, `user`, `result`). The
    legacy `--output-format json` envelope is ONE JSON value on one line (its
    `result` string's newlines are escaped), so it never has >=2 event lines and
    falls through to the single-envelope path. Strict on purpose: one non-event
    line means this is not a clean stream, and guessing is how a tail fragment
    gets read as the whole answer.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    events: list[dict[str, Any]] = []
    for ln in lines:
        try:
            obj = json.loads(ln)
        except ValueError:
            return None
        if not (isinstance(obj, dict) and "type" in obj):
            return None
        events.append(obj)
    return events


def _answer_from_stream(events: list[dict[str, Any]]) -> tuple[list[Any], str]:
    """Reassemble the full answer across ALL turns of a stream-json run.

    This is the fix's whole point: `result` alone is the FINAL message, so the
    rows are rebuilt from every `assistant` event's text, in order.
    """
    chunks: list[str] = []
    n_assistant = 0
    for e in events:
        if e.get("type") != "assistant":
            continue
        n_assistant += 1
        content = (e.get("message") or {}).get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    chunks.append(str(block.get("text") or ""))
    # The terminal `result` event is the CLI's own verdict on the run: an error
    # there is an error even when some assistant text arrived, and it carries the
    # turn count and the permission-gate refusals for the log.
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    # H1 (Codex HIGH). REQUIRE the terminal `result` event. A well-formed stream
    # always ends with one; its ABSENCE means the run was truncated or killed
    # mid-turn, and the assistant text collected so far is a PARTIAL answer, not a
    # complete one. The old `result is not None and …` accepted a resultless
    # stream as success with whatever rows had arrived — a silent partial parse.
    if result is None:
        raise ValueError(
            "the stream ended with no terminal result event — a truncated run "
            "is not a complete answer")
    if result.get("is_error"):
        # THE `result` STRING IS MODEL-AUTHORED and this sentence reaches `$LOG`
        # (review 2026-08-15, CRITICAL). The text is described; the SUBTYPE is
        # projected onto the CLI's own closed vocabulary — the previous round
        # wrote "only the CLI's own `subtype` vocabulary is quoted" and then
        # quoted whatever arrived, which a reviewer's probe walked straight
        # through with `subtype='MODEL_CANARY_SUBTYPE'`.
        raise ValueError(
            "the model leg reported an error (subtype="
            f"{project_envelope_field(result, 'subtype')}, "
            f"{_describe(str(result.get('result') or ''))})")
    # R2 (Codex re-review HIGH). A result event can report failure through its
    # SUBTYPE without setting `is_error` (e.g. `error_max_turns`), and the
    # `is_error` check above would then pass it as success. Reject any non-success
    # subtype. An ABSENT subtype is tolerated (older/leaner CLI shapes), so this
    # only fails an EXPLICIT non-success verdict — never a subtype-less result,
    # and never on the position of the result event (a trailing `system` event
    # after `result` is a normal stream shape, so events[-1] is not required).
    # `"subtype" in result` — NOT `.get() is not None`: `get` conflates an ABSENT
    # subtype with an explicit `"subtype": null`, and a null subtype is not the
    # word "success" so it must not pass (R2 round 3, Codex). A truly absent
    # subtype is still tolerated (leaner CLI shapes); a present one must be
    # "success".
    if "subtype" in result and result.get("subtype") != "success":
        raise ValueError(
            f"the model leg's result event was not a success (subtype="
            f"{project_envelope_field(result, 'subtype')}) — a failed run is "
            "not a complete answer")
    rows = extract_objects("".join(chunks))
    denials = result.get("permission_denials")
    n_denied = len(denials) if isinstance(denials, list) else 0
    # THE SUCCESS NOTE IS A SINK TOO, and the worse one: the nightly appends it
    # to `$LOG` on every HEALTHY night, where a refusal sentence only lands on a
    # broken one. `num_turns` went into it unprojected until this file's own
    # probe put the canary through it (2026-08-15).
    turns = project_envelope_field(result, "num_turns")
    return rows, (f"{len(rows)} row(s) reassembled from {n_assistant} assistant "
                  f"event(s) across {turns} turn(s); "
                  f"{n_denied} tool call(s) refused by the permission gate")


def _answer_from_single_envelope(
        text: str, envelope_path: Any) -> tuple[list[Any], str]:
    """The legacy `--output-format json` path: ONE envelope, `result` is the
    whole (short-enough) answer. Unchanged in behaviour from before STREAM-01."""
    try:
        env = json.loads(text)
    except ValueError as exc:
        # `JSONDecodeError.__str__` is the library's own "Expecting value: line
        # 1 column 1 (char 0)" — a position, never a slice of the document — so
        # it is host vocabulary and safe to print. `_describe` carries the rest.
        raise ValueError(
            f"the model leg's output at {envelope_path} could not be read "
            f"({str(exc)[:160]}; {_describe(text)}) — an unreadable answer is "
            "not an empty one") from exc
    if not isinstance(env, dict):
        raise ValueError(
            f"the model leg's output at {envelope_path} is a JSON "
            f"{type(env).__name__}, not the CLI's result envelope")
    if env.get("is_error"):
        raise ValueError(
            "the model leg reported an error (stop_reason="
            f"{project_envelope_field(env, 'stop_reason')}, "
            f"{_describe(str(env.get('result') or ''))})")
    rows = extract_answer(str(env.get("result") or ""))
    # The envelope counts what the permission gate refused. Zero is the normal
    # night; anything else is an injected leg reaching for a tool it does not
    # have, and it belongs in the log rather than in nobody's hands.
    denials = env.get("permission_denials")
    n_denied = len(denials) if isinstance(denials, list) else 0
    return rows, (f"{len(rows)} row(s) taken off the leg's stdout; "
                  f"{n_denied} tool call(s) refused by the permission gate; "
                  f"{project_envelope_field(env, 'num_turns')} turn(s)")


def read_envelope(source: str | Path) -> str:
    """The leg's output as text, BOUNDED BEFORE IT IS DECODED. `-` is stdin.

    THE PIPE IS THE PRODUCTION PATH (review 2026-08-15, CRITICAL): the nightly
    hands this module the leg's stdout directly, so the raw answer is never a
    file on disk and no sink control has to cover one. A path is still accepted
    for fixtures and for a captured envelope from before the change.

    A leg that emits until the disk complains is a denial of service on the OWA
    bearer window, not an answer, so the ceiling is applied to the STREAM as it
    arrives (`read(MAX_ENVELOPE_BYTES + 1)`) rather than after the whole thing is
    in memory. Refusing past the cap closes the pipe; the producer takes SIGPIPE
    and the caller's `PIPESTATUS` check removes any partial answer file.
    """
    if str(source) == "-":
        raw = sys.stdin.buffer.read(MAX_ENVELOPE_BYTES + 1)
        if len(raw) > MAX_ENVELOPE_BYTES:
            raise ValueError(
                f"the model leg emitted more than the {MAX_ENVELOPE_BYTES}-byte "
                "ceiling — an answer that fills the disk is not a usable answer")
        return raw.decode("utf-8", "replace")
    path = Path(source)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if size > MAX_ENVELOPE_BYTES:
        raise ValueError(
            f"the model leg's output at {path} is {size} bytes, past "
            f"the {MAX_ENVELOPE_BYTES}-byte ceiling — an answer that fills the "
            "disk is not a usable answer")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(
            f"the model leg's output at {path} could not be read "
            f"({str(exc)[:160]}) — an unreadable answer is not an empty one"
        ) from exc


def answer_from_text(text: str, label: str = "the leg's stdout"
                     ) -> tuple[list[Any], str]:
    """Auto-detect the stream-json shape (one JSON event per line) from the
    legacy single-envelope shape and reassemble accordingly. Returns the rows and
    a one-line note for the log. Raises ValueError with a sentence on every
    failure — and never one carrying model-authored text."""
    events = _stream_events(text)
    if events is None:
        return _answer_from_single_envelope(text, label)
    return _answer_from_stream(events)


def answer_from_envelope(envelope_path: str | Path) -> tuple[list[Any], str]:
    """`read_envelope` then `answer_from_text` — the whole parse in one call."""
    return answer_from_text(read_envelope(envelope_path), str(envelope_path))


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--envelope", required=True,
                   help="the leg's stdout — `-` for the PIPE (what the nightly "
                        "passes), or a path to a captured envelope")
    p.add_argument("--out", type=Path, required=True,
                   help="where the trusted host writes the parsed answer")
    p.add_argument("--grounding", type=Path, default=None,
                   help="this chunk's grounding map; its blocks are what the "
                        "overlap rule compares each free-text field against")
    p.add_argument("--batches-dir", type=Path, required=True,
                   help="this chunk's batch files — REQUIRED. They are the step-"
                        "4 subtraction AND the host enumeration every "
                        "`conversation_id` is bound against; there is no mode "
                        "without one")
    p.add_argument("--schema", choices=("judgment", "category"),
                   default="judgment",
                   help="which batch's closed key table to project onto")
    p.add_argument("--projection-out", type=Path, default=None,
                   help="where the projection's counts are written (default: "
                        "`projection.json` beside --out)")
    args = p.parse_args(argv[1:])

    def _refuse(sentence: str, text: str | None = None) -> int:
        """Fail closed, and leave behind ONLY host-authored bytes.

        The raw envelope used to be persisted so an operator had something to
        read after a parse failure; it carried model-authored keys and values
        verbatim, before any projection (review 2026-08-15, CRITICAL). This is
        what replaces it: the refusal sentence (host vocabulary, `_describe`d),
        plus the size and digest of what arrived. Enough to tell "empty" from
        "prose" from "the same failure as last night"; not one character the
        model wrote.
        """
        print(sentence, file=sys.stderr)
        try:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            (args.out.parent / "parse-failure.json").write_text(
                json.dumps({"refused": sentence,
                            "envelope": _describe(text) if text is not None
                            else "not read",
                            "schema": args.schema}, indent=2, sort_keys=True)
                + "\n", encoding="utf-8")
        except OSError:
            pass
        return 1

    try:
        text = read_envelope(args.envelope)
    except ValueError as exc:
        return _refuse(str(exc))
    try:
        rows, note = answer_from_text(text, str(args.envelope))
    except ValueError as exc:
        return _refuse(str(exc), text)

    # THE PROJECTION RUNS BEFORE THE FILE IS WRITTEN, and it runs whether or not
    # a grounding map exists: the closed schema is what stops an unenumerated key
    # reaching disk at all, and that is true of an ungrounded night too. Without
    # a map the overlap test simply has nothing to compare against, which is
    # correct — nothing was fed.
    blocks: dict[str, Any] = {}
    if args.grounding and args.grounding.exists():
        try:
            blocks = (json.loads(args.grounding.read_text(encoding="utf-8"))
                      .get("blocks") or {})
        except (OSError, ValueError):
            blocks = {}
    own = own_row_texts(args.batches_dir)
    # THE HOST ENUMERATION, and it is MANDATORY. `project` refuses an empty one
    # rather than treating "nothing to bind against" as an accepted mode — a
    # chunk whose batch files enumerate no id has nothing to judge, and the
    # previous round's `None`-means-inert default made the whole binding a no-op
    # on any call that omitted the flag. Both nightly call sites pass it, pinned
    # by `test_both_nightly_legs_hand_the_parser_its_host_enumeration`.
    try:
        rows, stats = project(rows, blocks, own,
                              CATEGORY_KEYS if args.schema == "category"
                              else ALLOWED_KEYS,
                              enumerated=set(own))
    except ValueError as exc:
        return _refuse(f"{exc} (batches at {args.batches_dir})", text)

    # THE OUTPUT BUDGET, checked on the exact bytes that would be written and
    # BEFORE they are (requirement (d)). Fail closed: no answer file is the
    # caller's existing survivable path, and a truncated one is not.
    body = json.dumps(rows, indent=2, ensure_ascii=False) + "\n"
    n_bytes = len(body.encode("utf-8"))
    if n_bytes > MAX_ANSWER_BYTES:
        return _refuse(
            f"the projected answer is {n_bytes} bytes, past the "
            f"{MAX_ANSWER_BYTES}-byte output ceiling for one chunk — an "
            "answer that fills the disk is not a usable answer", text)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    # A PRIOR ATTEMPT'S FAILURE RECORD IS NOT THIS ATTEMPT'S (the same reason the
    # nightly `rm -f`s the answer file before the leg runs): a chunk dir is
    # reused across a rerun, and a stale `parse-failure.json` beside a fresh
    # answer would send an operator to a failure that did not happen tonight.
    (args.out.parent / "parse-failure.json").unlink(missing_ok=True)
    args.out.write_text(body, encoding="utf-8")
    (args.projection_out or args.out.parent / "projection.json").write_text(
        json.dumps(stats, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(note + f"; projection kept {stats['rows_out']} of {stats['rows_in']} "
          f"row(s), dropped {sum(stats['dropped_unknown_keys'].values())} "
          f"unknown key(s), refused "
          f"{sum(stats['refused_grounding_overlap'].values())} on grounding "
          f"overlap / {sum(stats['refused_oversize_field'].values())} oversize "
          f"/ {stats['refused_oversize_row']} oversize row(s) "
          f"/ {stats['refused_unenumerated_id']} unenumerated id(s) "
          f"/ {stats['refused_shape']} on shape")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
