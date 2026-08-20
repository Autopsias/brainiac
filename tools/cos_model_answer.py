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

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cos_model_answer_parse                                    # noqa: E402
import cos_model_answer_write                                    # noqa: E402
# batch-2 drain: the overlap detector, the envelope readers and the two
# untrusted-boundary parsers moved to siblings; every name is re-imported or
# wrapped below so its `cos_model_answer` module path is unchanged. The parser
# and envelope wrappers pass THIS module (`_self()`) down, so the constants a
# test rebinds on it (`MAX_PARSED_ROWS`, `MAX_ROW_BYTES`) still steer them.
import cos_model_answer_envelope                                   # noqa: E402
import cos_model_answer_extract                                    # noqa: E402
from cos_model_answer_envelope import (  # noqa: E402,F401
    ENVELOPE_METADATA, ENVELOPE_VOCAB, MAX_REPORTED_TURNS, RESULT_SUBTYPES,
    STOP_REASONS, UNRECOGNISED, _answer_from_single_envelope,
    _answer_from_stream, _describe, _stream_events, project_envelope_field)
from cos_model_answer_extract import (  # noqa: E402,F401
    _FENCE, _JSON_WS, _MAX_SCAN_FAILURES, _decode_array, _next_bracket,
    RowCeiling)
from cos_model_answer_overlap import (  # noqa: E402,F401
    SHINGLE_W, USE_SHINGLE_W, _DOTLESS_I, _fold, _norm_tokens, block_shingles,
    overlap_hit, shingles)
# The closed key schema, its declared types and its per-field measures live in
# `cos_model_answer_schema` (the s17 extraction); every name below is re-exported
# so the enumerating tests keep reading it off THIS module — the schema's
# closure tests walk `cma.KEY_TYPES` and friends, not a second table.
from cos_model_answer_schema import (                            # noqa: E402,F401
    ADMITTED_FIELDS, ALLOWED_KEYS, CATEGORY_KEYS, DEFAULT_FIELD_MAX,
    DEFAULT_LIST_MAX, DRAFT_KEYS, DRAFT_TYPES, FIELD_BYTE_FACTOR, FIELD_MAX,
    KEY_TYPES, LIST_MAX, PLACEHOLDER_CAP, SPAN_KEYS, STRUCTURED_KEYS,
    _BOOL, _STR, _STRLIST, _count, _key_bucket, _strings, _too_long,
    _too_many, _typed, field_bytes, project_draft_value, project_keys,
    project_span_value,
)

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
# and counted; it is never carried. The tables themselves — the two key sets,
# their declared types, the structured keys, the per-field character/byte
# ceilings and the list cardinalities — live in `cos_model_answer_schema`
# (the s17 extraction) and are re-exported above; nothing about them is
# defined twice.

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
#
# The VALUE lives in `cos_model_answer_overlap` and is imported above. It was
# ALSO assigned here, shadowing that import with an equal literal — the two
# agreed by luck. `shingles()` reads the overlap module's copy while every
# reader of `cos_model_answer.SHINGLE_W` got this one, so editing either alone
# would have split the engine's one notion of "the same text" silently.


class _SelfNS:
    """Attribute view of THIS module's globals.

    A test fixture may load this file WITHOUT registering it in `sys.modules`,
    so `sys.modules[__name__]` is not reliable — but `globals()` is always this
    module's own dict, and a fixture's `mod.ATTR = x` writes into that same
    dict, so rebinding steers exactly as it always did."""

    def __getattr__(self, name):
        try:
            return globals()[name]
        except KeyError:
            raise AttributeError(name) from None


def _self():
    """This module as loaded — whichever name the harness registered it under
    (`cos_model_answer`, or a test fixture's own name). Passed to the sibling
    sub-steps so the constants live in ONE namespace a test can rebind."""
    return _SelfNS()


def _row_ceiling(n: int) -> None:
    """ONE ceiling, applied by EVERY parser path — see the extract sibling."""
    cos_model_answer_extract._row_ceiling(n, _self())


def extract_answer(result: str) -> list[Any]:
    """The JSON ARRAY the leg was asked for, or ValueError saying what came back
    (implementation: `cos_model_answer_extract`, same signature plus namespace)."""
    return cos_model_answer_extract.extract_answer(result, _self())


def extract_objects(text: str) -> list[Any]:
    """Every top-level JSON object the stream leg emitted, in order
    (implementation: `cos_model_answer_extract`, same signature plus namespace)."""
    return cos_model_answer_extract.extract_objects(text, _self())


def read_envelope(source: str | Path) -> str:
    """The leg's output as text, BOUNDED BEFORE IT IS DECODED. `-` is stdin
    (implementation: `cos_model_answer_envelope`, same signature plus namespace)."""
    return cos_model_answer_envelope.read_envelope(source, _self())


def answer_from_text(text: str, label: str = "the leg's stdout"
                     ) -> tuple[list[Any], str]:
    """Auto-detect the stream-json shape from the legacy single-envelope shape
    (implementation: `cos_model_answer_envelope`, same signature plus namespace)."""
    return cos_model_answer_envelope.answer_from_text(text, label, _self())


def answer_from_envelope(envelope_path: str | Path) -> tuple[list[Any], str]:
    """`read_envelope` then `answer_from_text` — the whole parse in one call."""
    return cos_model_answer_envelope.answer_from_envelope(envelope_path, _self())


def project_row(row: dict[str, Any], block_text: str | None,
                own_row_text: str, stats: dict[str, Any],
                allowed: frozenset[str] = ALLOWED_KEYS) -> dict[str, Any] | None:
    """One row, projected onto the closed schema. `None` means REFUSED.

    Refused, never truncated and never masked — the same posture
    `load_categories` takes toward a stray id. A refused row costs one verdict;
    a truncated one is a short answer wearing a full one's shape.

    The per-key walk (unknown-key counting, the `draft`/`evidence_span`
    sub-objects, the declared-type and per-field ceilings) lives in
    `cos_model_answer_schema.project_keys`; the aggregate row bound stays HERE,
    read off this module's own `MAX_ROW_BYTES` at call time, so a test that
    rebinds it sees the projection honour the new ceiling.
    """
    out = project_keys(row, allowed, stats)
    if out is None:
        return None

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
        return cos_model_answer_write.refuse(args, sentence, text, _describe)

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

    # The output budget, the stale-failure unlink and the answer/projection
    # writes all live in `cos_model_answer_write.write_answer`; the ceiling is
    # passed in so this module's own `MAX_ANSWER_BYTES` is read at call time.
    return cos_model_answer_write.write_answer(
        args, rows, stats, note, text, max_answer_bytes=MAX_ANSWER_BYTES,
        describe=_describe)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
