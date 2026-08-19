"""Bracket-scanning sub-steps of `cos_model_answer`'s two untrusted-boundary parsers.

The two public parsers stay in :mod:`cos_model_answer` with unchanged
signatures (`extract_answer` for the legacy single envelope, `extract_objects`
for the stream leg); this module holds the two loops they dispatch to — the
candidate-bracket scan that finds an array buried after prose, and the
shape-filter append that keeps only objects carrying a `conversation_id`.
Import direction is one-way: the decoder, the incremental array walker, the
row-ceiling callable and the host-authored `_describe` all arrive as
parameters, so a test that rebinds `MAX_PARSED_ROWS` on the parent module is
still honoured and the digest stays defined exactly once.
"""
from __future__ import annotations

import json
from typing import Any, Callable


def scan_for_array(dec: json.JSONDecoder, text: str, start: int,
                   decode_array: Callable[..., tuple[list[Any], int]],
                   row_ceiling: type[BaseException],
                   describe: Callable[[str], str]) -> Any:
    """The first `[` that parses as an array, scanning forward from `start`.

    Raises ValueError (naming the last failure, described by `describe`)
    when no candidate bracket parses; re-raises `row_ceiling` untouched,
    exactly as the caller's own paths do.
    """
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
            doc, _end = decode_array(dec, text[start:], 0)
            return doc
        except row_ceiling:
            raise
        except ValueError as exc:
            last_exc = exc
            start = text.find("[", start + 1)
    raise ValueError(
        f"the leg's answer is not parseable JSON ({last_exc}) "
        f"({describe(text)})") from last_exc


def retain_objects(val: Any, out: list[Any],
                   enforce_ceiling: Callable[[int], None]) -> None:
    """Append the verdict-shaped objects in `val` to `out`, ceiling-checked.

    SHAPE FILTER (R2, Codex re-review). Admit only objects that carry a
    `conversation_id` — the key both consumers (`load_categories`,
    `judge_night`) join on. An object without one cannot become anyone's
    answer, so dropping it costs nothing and removes the accidental prose
    object (a mail body's `{"timeout": 30}`) from the surface.

    STRUCTURAL RESIDUAL, stated not hidden: this cannot reject a
    verdict-SHAPED object echoed from untrusted mail (one carrying a
    `conversation_id`), because the multi-turn answer is itself broken by
    the model's `Continuing the array …` prose at every turn boundary — so
    "parse exactly one clean array" is not available without re-breaking the
    STREAM-01 fix. And the model HOLDS the enumerated id while reading that
    thread's untrusted body, so an injection CAN instruct it to copy the id
    into a fabricated object — the id is not a secret at this layer. The
    containment is downstream and layered, not "the id is unguessable":
      1. `judge_night` binds verdicts to the ENUMERATED set — an id the run
         never enumerated is dropped outright.
      2. A fabricated object for a REAL enumerated id is a DUPLICATE of that
         thread's genuine verdict; if they differ, H3 drops the cid to
         PENDING (fail-safe). The injection only survives if it also
         SUPPRESSES the genuine verdict — a strictly harder prompt injection.
      3. Any surviving verdict still passes `validate_verdict`'s closed
         vocabulary, and a staging/candidate verdict's `evidence_span` must
         land inside the REAL captured body — content it cannot forge blind.
      4. Whatever becomes a mutation still passes the frozen plan_binding,
         the rehearsal, and the mutation allowlist, and NOTHING sends: the
         worst reachable outcome is a reversible archive/categorise/draft.
    The ACCEPTED residual is a sophisticated suppress-and-inject prompt
    injection producing a reversible, non-send action on an enumerated
    thread, on a nightly that is disarmed and whose next run is attended.
    The attended live capture is what shows whether the model emits such
    objects at all; completing containment further (verdict provenance) is a
    downstream question, not a parser one.
    INCREMENTALLY, BEFORE THE APPEND. The old check ran once at the end,
    so a stream of a million objects was fully materialised in memory
    before anything refused it (review 2026-08-15).
    """
    if isinstance(val, dict):
        if "conversation_id" in val:
            enforce_ceiling(len(out) + 1)
            out.append(val)
    elif isinstance(val, list):
        for x in val:
            if isinstance(x, dict) and "conversation_id" in x:
                enforce_ceiling(len(out) + 1)
                out.append(x)
