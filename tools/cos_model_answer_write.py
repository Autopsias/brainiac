"""The trusted write-out stage of `cos_model_answer.main`'s answer pipeline.

`main` owns argument parsing, the envelope read and the projection call; this
module owns everything that happens to DISK and STDERR afterwards — the
fail-closed refusal artifact (`parse-failure.json`, host-authored bytes only)
and the final write of the projected answer plus its projection counts. Import
direction is one-way: the output ceiling arrives as a parameter so a test that
rebinds `MAX_ANSWER_BYTES` on the parent module is still honoured.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable


def refuse(args: argparse.Namespace, sentence: str,
           text: str | None, describe: Callable[[str], str]) -> int:
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
                        "envelope": describe(text) if text is not None
                        else "not read",
                        "schema": args.schema}, indent=2, sort_keys=True)
            + "\n", encoding="utf-8")
    except OSError:
        pass
    return 1


def write_answer(args: argparse.Namespace, rows: list[Any],
                 stats: dict[str, Any], note: str, text: str | None, *,
                 max_answer_bytes: int,
                 describe: Callable[[str], str]) -> int:
    """Serialize, bound, and write the projected answer. Returns the exit code.

    THE OUTPUT BUDGET is checked on the exact bytes that would be written and
    BEFORE they are (requirement (d)). Fail closed: no answer file is the
    caller's existing survivable path, and a truncated one is not.
    """
    body = json.dumps(rows, indent=2, ensure_ascii=False) + "\n"
    n_bytes = len(body.encode("utf-8"))
    if n_bytes > max_answer_bytes:
        return refuse(
            args,
            f"the projected answer is {n_bytes} bytes, past the "
            f"{max_answer_bytes}-byte output ceiling for one chunk — an "
            "answer that fills the disk is not a usable answer", text, describe)

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
