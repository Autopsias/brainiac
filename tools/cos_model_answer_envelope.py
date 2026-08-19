"""The CLI result-envelope readers of `cos_model_answer` — closed metadata vocabularies, the two-shape answer reassembly (batch-2 drain).

Moved verbatim out of `cos_model_answer`. The parent re-exports every name
(INCLUDING `_answer_from_stream` / `_answer_from_single_envelope` as the
functions themselves, so `inspect.getsource` and the `_answer_from_*`
enumeration the closure tests run keep working), and the parsers plus the
envelope byte ceiling arrive as the caller's module namespace ``cma``, read at
CALL time exactly as before.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

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


def _describe(text: str) -> str:
    """What arrived, said in the HOST's own words — never in the model's.

    Every refusal in this module used to quote `text[:80]`, and the nightly
    redirects this module's stderr into `$LOG`, which sits outside `$EV` and so
    outside the run directory's 0700 (review 2026-08-15, CRITICAL: the second
    unprojected channel). An operator debugging a parse failure needs to tell
    "empty", "prose", "truncated JSON" and "yesterday's answer again" apart, and
    a length + line count + digest does all four without carrying one character
    the model wrote. The digest is stable across runs, so a recurring failure is
    recognisable as the same one.
    """
    b = text.encode("utf-8", "replace")
    return (f"{len(b)} byte(s), {text.count(chr(10)) + 1} line(s), "
            f"sha256:{hashlib.sha256(b).hexdigest()[:16]}")


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


def _answer_from_stream(events: list[dict[str, Any]],
                        cma=None) -> tuple[list[Any], str]:
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
    rows = cma.extract_objects("".join(chunks))
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
        text: str, envelope_path: Any, cma=None) -> tuple[list[Any], str]:
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
    rows = cma.extract_answer(str(env.get("result") or ""))
    # The envelope counts what the permission gate refused. Zero is the normal
    # night; anything else is an injected leg reaching for a tool it does not
    # have, and it belongs in the log rather than in nobody's hands.
    denials = env.get("permission_denials")
    n_denied = len(denials) if isinstance(denials, list) else 0
    return rows, (f"{len(rows)} row(s) taken off the leg's stdout; "
                  f"{n_denied} tool call(s) refused by the permission gate; "
                  f"{project_envelope_field(env, 'num_turns')} turn(s)")


def read_envelope(source: str | Path, cma=None) -> str:
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
        raw = sys.stdin.buffer.read(cma.MAX_ENVELOPE_BYTES + 1)
        if len(raw) > cma.MAX_ENVELOPE_BYTES:
            raise ValueError(
                f"the model leg emitted more than the {cma.MAX_ENVELOPE_BYTES}-byte "
                "ceiling — an answer that fills the disk is not a usable answer")
        return raw.decode("utf-8", "replace")
    path = Path(source)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if size > cma.MAX_ENVELOPE_BYTES:
        raise ValueError(
            f"the model leg's output at {path} is {size} bytes, past "
            f"the {cma.MAX_ENVELOPE_BYTES}-byte ceiling — an answer that fills "
            "the disk is not a usable answer")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(
            f"the model leg's output at {path} could not be read "
            f"({str(exc)[:160]}) — an unreadable answer is not an empty one"
        ) from exc


def answer_from_text(text: str, label: str = "the leg's stdout",
                     cma=None) -> tuple[list[Any], str]:
    """Auto-detect the stream-json shape (one JSON event per line) from the
    legacy single-envelope shape and reassemble accordingly. Returns the rows and
    a one-line note for the log. Raises ValueError with a sentence on every
    failure — and never one carrying model-authored text."""
    events = _stream_events(text)
    if events is None:
        return _answer_from_single_envelope(text, label, cma)
    return _answer_from_stream(events, cma)


def answer_from_envelope(envelope_path: str | Path,
                         cma=None) -> tuple[list[Any], str]:
    """`read_envelope` then `answer_from_text` — the whole parse in one call."""
    return answer_from_text(read_envelope(envelope_path, cma),
                            str(envelope_path), cma)
