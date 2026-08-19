"""Row validation for the COS driver's category-batch answer file.

``cos_driver.load_categories`` keeps its name, signature and module path (the
tests and the doctrine text name it there; batch-2 drain moved the function
itself here and the parent re-imports it); what lived here first is the
per-row loop that turns the JSON array into ``{conversation_id: category}``
stamps, refusing row by row exactly as before. The owner's taxonomy gate
(``resolve_never``) and ``category_gate_state`` moved here in the same drain,
re-imported by the parent so ``src/brain/cos_echecks`` — which loads
``cos_driver`` for ``category_gate_state`` — is unaffected.
"""
from __future__ import annotations

from typing import Callable


def category_row_stamps(path_name: str, raw: list,
                        short: Callable[[str], str]) -> dict[str, str]:
    """``[{conversation_id, category}]`` -> stamps, refusing the first bad row.

    Raises ``ValueError`` with the same messages the single function raised;
    ``ValueError`` is builtin, so the exception identity is unchanged.
    """
    out: dict[str, str] = {}
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            raise ValueError(f"{path_name} row {i} is a "
                             f"{type(row).__name__}, not an object")
        cid = str(row.get("conversation_id") or "").strip()
        if not cid:
            raise ValueError(f"{path_name} row {i} carries no conversation_id — "
                             "a stamp that names no thread stamps nothing")
        if "category" not in row:
            raise ValueError(
                f"{path_name} row {i} (conversation {short(cid)}) carries no "
                "`category` key at all. An absence is not the `null` the batch "
                "calls honest — it is a row the model never finished, and "
                "reading the two as the same empty string is how an INCOMPLETE "
                "answer armed the gate as a complete one")
        cat = row["category"]
        if cat is None:
            value = ""
        elif not isinstance(cat, str) or not cat.strip():
            raise ValueError(
                f"{path_name} row {i} (conversation {short(cid)}) carries "
                f"category {cat!r}. The batch asks for `null` or one of the "
                "owner's category ids; a blank string, a whitespace-only one "
                "and a non-string are none of those, and each used to collapse "
                "into the same empty string an explicit `null` produces")
        else:
            value = cat.strip()
        # CONFLICT-AWARE DUPLICATE (run 132, live capture — the category-leg
        # analog of judge_night's H3). The STREAM-01 multi-turn reassembly
        # RECOVERS the full answer, but the model re-emits a boundary object
        # across a turn split, so an identical duplicate arrives — measured on
        # run 132, one re-emission in a 258-row answer. That is a benign
        # re-emission of the SAME stamp, not "two answers to one question", so it
        # is collapsed. Only a CONFLICT — two DIFFERENT categories for one thread
        # — is refused, the genuine ambiguity the old blanket refusal meant to
        # catch. Refusing the benign case failed the whole 258-row gate on a
        # single re-emission and read `not-run` on an answer that was complete.
        if cid in out:
            if out[cid] == value:
                continue
            raise ValueError(
                f"{path_name} stamps conversation {short(cid)} with two "
                f"DIFFERENT categories ({out[cid]!r} and {value!r}) — two answers "
                "to one question, and whichever wins is decided by row order")
        out[cid] = value
    return out


# ---------------------------------------------------------------------------
# batch-2 drain: the category machinery moved verbatim out of `cos_driver`
# (resolve_never / load_categories / category_gate_state) and is re-imported
# by it — src/brain/cos_echecks loads `cos_driver` for `category_gate_state`,
# which keeps resolving off the parent module.
# ---------------------------------------------------------------------------
import json                                                    # noqa: E402
import sys                                                     # noqa: E402
from collections.abc import Iterable                           # noqa: E402
from pathlib import Path                                       # noqa: E402
from typing import Any                                         # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_driver_transport import DriverStop, short             # noqa: E402


def resolve_never(vault: Path, categories: dict[str, str]
                  ) -> dict[str, Any]:
    """Which of the model's stamped categories the OWNER's taxonomy calls `never`.

    THE DRIVER DOES NOT DECIDE A CATEGORY, and this is not it deciding one. The
    category arrives already judged, one per conversation, from the pre-draw
    category batch; all that happens here is a lookup in
    `<vault>/overlay/cos/ingest.md` — the owner's own config file — to ask which
    ids that file dispositions `never`. A stamp naming an id the taxonomy does
    not define is NOT excluded and is reported: an unknown id is a guess, and
    acting on a guess by skipping a body is the same defect as inventing one.
    """
    from brain import cos                                         # noqa: PLC0415

    taxonomy = cos.ingest_taxonomy(vault) or {}
    rules = taxonomy.get("rules") or {}
    never_ids = {cid for cid, r in rules.items()
                 if str((r or {}).get("disposition") or "").strip().lower() == "never"}
    excluded, undefined = set(), {}
    for conv, cat in categories.items():
        name = str(cat or "").strip()
        if not name:
            continue
        if name not in rules:
            undefined[name] = undefined.get(name, 0) + 1
            continue
        if name in never_ids:
            excluded.add(conv)
    return {"mode": taxonomy.get("mode"), "never_ids": sorted(never_ids),
            # WHAT THE OWNER ACTUALLY DEFINED, carried out so the gate's state
            # can be checked against it instead of against "non-empty string".
            "defined_ids": sorted(rules),
            "excluded": excluded, "undefined_categories": undefined,
            "categorised": len(categories)}


def load_categories(path: Path, *,
                    in_scope_ids: Iterable[str] | None = None) -> dict[str, str]:
    """`[{"conversation_id": ..., "category": ...}]` -> `{conv: category}`.

    ONE SHAPE, AND IT IS CHECKED (review 2026-08-13, round 2). This used to
    accept a mapping root too — "one shape reaching the disk in the other's
    clothing should not cost a night" — and that branch is exactly how a FAILED
    model run got read as an answer. The output a truncated or abandoned run
    leaves is a SINGLE OBJECT rather than an array, and
    `{"conversation_id": "c1", "category": "x"}` came out of the mapping branch
    as two stamps named `conversation_id` and `category`: neither is a
    conversation, both are silently wrong, and nothing downstream could tell.
    The batch asks for an array of rows; anything else is REFUSED.

    A refusal is survivable and documented: the nightly leaves `--categories`
    off, the draw runs ungated and `category_gate.state` reads `not-run` — the
    shape every run before this one had. What is not survivable is a wrong
    stamp, because a `never` stamp on the wrong thread silently withholds a
    body from the draw.

    `in_scope_ids`, when given, is the enumeration this answer was asked about.
    A stamp naming a conversation that enumeration does not carry means the
    file is not an answer to THIS run's batch, so the file is refused rather
    than partly believed. A category of `null` is kept as the empty string and
    simply never matches a `never` id.

    AN ABSENCE IS NOT A `null` (review 2026-08-13, round 4, Codex HIGH). This
    read the value as `str(row.get("category") or "").strip()`, which collapsed
    a MISSING key, an explicit `null` and a whitespace-only string into one
    empty string — and the coverage predicate then armed the gate on all three.
    Round 3's own requirement is that every value be "either `null` or a
    currently defined taxonomy id"; a row that never names the key is neither,
    it is a row the model did not finish. So the key must be PRESENT, and its
    value must be an explicit `null` (kept as the empty string, the honest
    "nothing to exclude here") or a nonblank string (checked against the
    taxonomy afterwards, as before). Anything else is a load REFUSAL, which
    lands on the same survivable path as every other one: exit 2, no
    `--categories`, an UNGATED draw, `category_gate.state` reading `not-run`.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(
            f"{path.name} is a JSON {type(raw).__name__}, not the array of "
            "`{conversation_id, category}` rows the batch asks for. A single "
            "object is what a failed or truncated model run leaves behind, and "
            "reading its KEYS as conversation ids is how a run stamps threads "
            "that do not exist")
    out = category_row_stamps(path.name, raw, short)
    if in_scope_ids is not None:
        stray = sorted(set(out) - {str(c) for c in in_scope_ids})
        if stray:
            raise ValueError(
                f"{path.name} stamps {len(stray)} conversation(s) this run did "
                f"not enumerate (e.g. {short(stray[0])}) — it is not an answer "
                "to this run's batch")
    return out


def category_gate_state(categories: dict[str, str] | None,
                        in_scope_ids: Iterable[str],
                        defined_categories: Iterable[str] | None
                        ) -> dict[str, Any]:
    """Did the pre-draw category gate actually run? ONE definition, two callers.

    THE TWO SPELLINGS WERE THE FIRST DEFECT (review 2026-08-13, round 2). The
    driver reported `armed` when `categories is not None` and the judge when
    `categories` was truthy, so an empty answer — `[]`, two bytes, which
    `[ -s ]` happily passes — made ONE run report `armed` from one leg and
    `not-run` from the other while excluding nothing at all.

    ONE NON-EMPTY STAMP WAS THE SECOND (round 3, Codex HIGH). Deriving state
    from "any in-scope stamp with a non-empty value" armed the gate on a
    PARTIAL answer and on a taxonomy-UNDEFINED one:
    `{"c0": "no-such-category"}` over three enumerated rows reported `armed`
    while `resolve_never` ignores unknown ids, so nothing could be excluded and
    nothing was.

    So `armed` now means what the batch prompt already CLAIMS is machine-checked
    ("EXACTLY ONE category id per conversation … an id the owner never wrote is
    REFUSED"): every enumerated conversation carries a row, and every value is
    either `null` (honest, and cheap — the row stays in the draw) or an id the
    owner's taxonomy defines. Exclusion is reported SEPARATELY and is never
    part of this predicate: a complete all-`null` answer is a gate that RAN and
    held nothing out, which is a different fact from a gate that never ran.

    `defined_categories` HAS NO DEFAULT, deliberately (round 4, Claude LOW). It
    used to default to `None`, which is this function's "the taxonomy could not
    be read on this leg" sentinel — so a caller that simply FORGOT the argument
    got a permanently disarmed gate explained by a cause that never happened. A
    missing argument is now a `TypeError` at the call site, which is loud.
    """
    ids = {str(c) for c in in_scope_ids}
    supplied = dict(categories or {})
    in_scope = {c: v for c, v in supplied.items() if c in ids}
    missing = sorted(ids - set(in_scope))
    stamped = sorted(c for c, v in in_scope.items() if str(v or "").strip())
    known = None if defined_categories is None else {
        str(c) for c in defined_categories}
    undefined = sorted({str(in_scope[c]).strip() for c in stamped
                        if known is not None and str(in_scope[c]).strip() not in known})

    if categories is None:
        why = "no category answer reached this leg"
    elif not known:
        # None (not read) and EMPTY (read, defines nothing) both land here on
        # purpose: with no defined id, NO STAMP CAN BE CHECKED, so whether this
        # answer is a valid one is unverifiable — and an unverifiable answer is
        # not an armed gate. (The reason is deliberately NOT "nothing could be
        # excluded": the sibling `null` case excludes nothing either and DOES
        # arm, because a taxonomy with no `never` id is a gate that ran and
        # held nothing out. Round 4, Claude LOW — the old wording gave a reason
        # its own neighbour contradicts.)
        why = ("the owner's taxonomy "
               + ("could not be read on this leg" if known is None
                  else "defines no category")
               + ", so no stamp in this answer could be CHECKED against it and "
                 "whether the gate really ran is unverifiable")
    elif not ids:
        why = "this run enumerated no conversation, so there was nothing to stamp"
    elif missing:
        why = (f"{len(missing)} of {len(ids)} enumerated conversation(s) carry "
               f"no stamp (e.g. {short(missing[0])}) — a partial answer gates "
               "only the part it covers, and used to report `armed` anyway")
    elif undefined:
        why = (f"{len(undefined)} stamp(s) name a category the owner's taxonomy "
               f"does not define (e.g. {undefined[0]!r}); an undefined id "
               "excludes nothing, so this would be `armed` over a gate that "
               "cannot hold anything out")
    else:
        why = (f"all {len(ids)} enumerated conversation(s) carry a stamp the "
               f"taxonomy defines — {len(stamped)} named a category and "
               f"{len(ids) - len(stamped)} answered null")
    armed = bool(categories is not None and known and ids
                 and not missing and not undefined)
    return {"state": "armed" if armed else "not-run",
            # COVERAGE, not exclusion. `categorised_in_scope` is how many rows
            # named a category at all; how many of those the taxonomy
            # dispositions `never` — the actual exclusion — is the caller's
            # `excluded_before_draw`, and the two are never the same number.
            "stamps_in_scope": len(in_scope),
            "categorised_in_scope": len(stamped),
            "unstamped_in_scope": len(missing),
            "undefined_ids": undefined[:8],
            "stamps_supplied": len(supplied),
            "in_scope": len(ids),
            "why": why}

