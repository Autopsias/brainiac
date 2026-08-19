"""The two untrusted-boundary parsers of `cos_model_answer` — incremental array decode plus the stream-object scan (batch-2 drain).

Moved verbatim out of `cos_model_answer` on the same parameter-passing contract
`cos_model_answer_parse` already documents: the caller's module namespace
arrives as ``cma`` and the row-ceiling callable is BUILT from it per call, so a
test that rebinds ``MAX_PARSED_ROWS`` on the parent module is still honoured.
The parent keeps thin same-signature wrappers, so ``cma.extract_answer`` and
``cma.extract_objects`` are unchanged for every caller.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cos_model_answer_parse                                    # noqa: E402

#: A fenced block the model wrapped its answer in. The prompts ask for a bare
#: array; this is tolerated because the alternative to tolerating it is losing a
#: whole night's judgment to three backticks, and stripping a fence cannot
#: change what the JSON inside it says.
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class RowCeiling(ValueError):
    """The row ceiling, as its own type so a bracket-scan retry cannot swallow it.

    `extract_answer`'s fallback scan catches `ValueError` to mean *"this `[` was
    not the array, try the next one"*. Once the ceiling is raised from INSIDE the
    element loop (below), a plain `ValueError` would be caught by that handler
    and the refusal would silently become "keep looking".
    """


def _row_ceiling(n: int, cma) -> None:
    """ONE ceiling, applied by EVERY parser path (review 2026-08-15, HIGH).

    The bound used to live only inside `extract_objects`, and
    `answer_from_envelope` auto-detects TWO shapes: the legacy single envelope
    runs `extract_answer` first and took 2,001 rows. `test_every_parser_entry_
    path_enforces_the_row_ceiling` enumerates the `_answer_from_*` functions
    rather than naming them, so a third shape added later fails until it counts.

    FAIL CLOSED, not truncate: a truncated answer is a short batch wearing a full
    one's row count, and the caller's no-answer path already exists. The bound is
    read off the caller's namespace AT CALL TIME, which is the whole point.
    """
    if n > cma.MAX_PARSED_ROWS:
        raise RowCeiling(
            f"the leg emitted {n} objects, past the {cma.MAX_PARSED_ROWS}-row "
            "ceiling for one chunk — a leg that emits without bound is "
            "spending the mutation window, not judging")


_JSON_WS = " \t\n\r"


def _decode_array(dec: json.JSONDecoder, text: str, start: int,
                  already: int = 0,
                  row_ceiling=None) -> tuple[list[Any], int]:
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
        row_ceiling(already + len(out) + 1)
        val, i = dec.raw_decode(text, i)
        out.append(val)
        expect_value = False


def extract_answer(result: str, cma) -> list[Any]:
    """The JSON ARRAY the leg was asked for, or ValueError saying what came back.

    An array, specifically. Both prompts ask for "a single JSON array, one
    object per conversation_id", and both consumers (`cos_driver.load_categories`
    and `cos_judge`) REFUSE anything else — `load_categories` documents exactly
    why: a truncated or abandoned run leaves a single OBJECT behind, and reading
    its keys as conversation ids stamps threads that do not exist. Refusing the
    wrong shape HERE gives the same refusal one step earlier, with a sentence
    naming what actually arrived.
    """
    def _ceiling(n: int) -> None:
        _row_ceiling(n, cma)

    def _dec(dec: json.JSONDecoder, text: str, start: int,
             already: int = 0) -> tuple[list[Any], int]:
        return _decode_array(dec, text, start, already, _ceiling)

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
            doc, _end = _dec(dec, text, 0)
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
        # does. The scan loop itself lives in
        # `cos_model_answer_parse.scan_for_array`, which carries the measured
        # slice-versus-index reasoning with it and re-raises `RowCeiling`
        # untouched.
        start = text.find("[")
        if start < 0:
            raise ValueError(
                "the leg answered with no JSON array at all "
                f"({cma._describe(text)})") from None
        doc = cos_model_answer_parse.scan_for_array(
            dec, text, start, _dec, RowCeiling, cma._describe)
    if not isinstance(doc, list):
        raise ValueError(
            f"the leg answered with a JSON {type(doc).__name__}, not the array "
            "of rows the batch asks for. A single object is what a truncated or "
            "abandoned run leaves behind, and reading its keys as conversation "
            "ids is how a run stamps threads that do not exist")
    _ceiling(len(doc))
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


def extract_objects(text: str, cma) -> list[Any]:
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
    def _ceiling(n: int) -> None:
        _row_ceiling(n, cma)

    def _dec(dec: json.JSONDecoder, text_: str, start: int,
             already: int = 0) -> tuple[list[Any], int]:
        return _decode_array(dec, text_, start, already, _ceiling)

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
                val, end = _dec(dec, text, j, len(out))
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
            if failures > cma._MAX_SCAN_FAILURES:
                # H6: a brace-heavy text that never decodes is a DoS, not an
                # answer. Stop scanning and fail closed rather than churn.
                raise ValueError(
                    f"the leg's stream defeated the object scanner after "
                    f"{failures} unparseable brackets — an answer that pins "
                    "the parser is not a usable answer") from None
            i = j + 1
            continue
        i = end
        # SHAPE FILTER, ceiling-checked per append — the whole block lives in
        # `cos_model_answer_parse.retain_objects`, which moved with the R2
        # structural-residual doctrine note: only objects carrying a
        # `conversation_id` (the key both consumers join on) are admitted,
        # incrementally, BEFORE the append.
        cos_model_answer_parse.retain_objects(val, out, _ceiling)
    if not out:
        raise ValueError(
            "the leg's stream carried no JSON object at all "
            f"({cma._describe(text)})")
    return out
