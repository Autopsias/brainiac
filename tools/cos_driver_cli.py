"""The COS driver's command-line surface: argument parser plus the
`--validate-categories` mode.

``cos_driver.main`` keeps its name and module; what lives here is the
argparse spec it parses with and the one mode whose exit status the nightly
gates on without a browser. The parent callables the mode needs
(``load_categories``, ``category_gate_state``) arrive as parameters — this
module never imports ``cos_driver``, so a monkeypatched parent attribute
keeps working.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Callable


def night_argparser(description: str, default_cap: int) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--vault", type=Path, default=None)
    p.add_argument("--tab-id", type=int, default=None,
                   help="Chrome tab id of the signed-in mail tab (never an index)")
    p.add_argument("--cap", type=int, default=default_cap)
    p.add_argument("--out", type=Path, default=None, help="evidence json path")
    p.add_argument("--replay", default=None, metavar="RUN_ID",
                   help="rebuild the accounting from the corpus and print it")
    p.add_argument("--enumerated-at", default=None,
                   help="with --replay: the run's own `enumerated_at`, which "
                        "is a CAPTURED input (it is in the PRE snapshot and on "
                        "every ledger row), never a fresh clock read")
    p.add_argument("--stage", action="store_true",
                   help="stage the page-side driver into the tab's DOM and print "
                        "the one line to evaluate in its MAIN world")
    p.add_argument("--cdp", action="store_true",
                   help="drive the tab over CDP (port 9222) instead of "
                        "AppleScript — MAIN world, one browser by port")
    p.add_argument("--ego", action="store_true",
                   help="drive the signed-in mail tab through the ego lite CLI "
                        "— MAIN world, so no extension and no DOM bridge. The "
                        "tab must already be open in the `cos` task space; this "
                        "transport never navigates. Does NOT lift the "
                        "foreground-tab requirement")
    p.add_argument("--enumerate-only", action="store_true",
                   help="run pass 1 and STOP before the draw, writing the typed "
                        "fields to --out. This is the seam the category batch "
                        "goes in: enumerate, categorise, then re-run with "
                        "--categories to draw bodies with the `never` rows out")
    p.add_argument("--categories", type=Path, default=None,
                   help="the category batch's answer "
                        "(`[{conversation_id, category}]`). Rows whose category "
                        "the OWNER's taxonomy dispositions `never` are excluded "
                        "from the draw BEFORE any body is opened (rule 1¾)")
    p.add_argument("--enumeration", type=Path, default=None,
                   help="the `--enumerate-only` output the category answer was "
                        "asked about. REQUIRED with --categories (the body "
                        "pass re-enumerates, so the stamps are bound to the "
                        "snapshot they were judged on) and with "
                        "--validate-categories")
    p.add_argument("--validate-categories", action="store_true",
                   help="check --categories against --enumeration and exit 0 "
                        "only if it is a usable answer to THIS run's batch. "
                        "Non-zero means the nightly must run the draw ungated "
                        "rather than gate it on a file it cannot trust")
    p.add_argument("--selfcheck", action="store_true")
    return p


def validate_categories_mode(
        args: argparse.Namespace, *,
        load_categories: Callable[..., dict[str, str]],
        category_gate_state: Callable[..., dict]) -> int:
    """THE ONE COMMAND WHOSE EXIT STATUS THE NIGHTLY GATES ON.

    Kept out of the TAB preflight in the parent on purpose: it needs no
    browser, and a gate that cannot run without one is a gate that fails open
    on the paths that matter. It DOES need the vault, because an answer is
    only valid against the owner's taxonomy — and an unreadable taxonomy
    exits non-zero, which is the ungated-draw path, never a gate armed over
    rules it never read.
    """
    if args.categories is None or args.enumeration is None:
        print("--validate-categories needs --categories and --enumeration",
              file=sys.stderr)
        return 2
    cat_vault = args.vault or Path(
        os.environ.get("BRAIN_VAULT", "")).expanduser()
    try:
        from brain import cos as _cos                        # noqa: PLC0415
        defined = sorted((_cos.ingest_taxonomy(cat_vault) or {}
                          ).get("rules") or {})
    except Exception as exc:                                 # noqa: BLE001
        print(f"the owner's ingest taxonomy could not be read from "
              f"{cat_vault}: {exc} — an answer cannot be checked against "
              "rules this leg never saw", file=sys.stderr)
        return 2
    if not defined:
        print(f"the owner's ingest taxonomy at {cat_vault} defines no "
              "category, so no stamp could be a valid one", file=sys.stderr)
        return 2
    try:
        rows = json.loads(args.enumeration.read_text(encoding="utf-8"))["rows"]
        ids = [r["conversation_id"] for r in rows]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"the enumeration at {args.enumeration} is unreadable: {exc}",
              file=sys.stderr)
        return 2
    try:
        cats = load_categories(args.categories, in_scope_ids=ids)
    except (OSError, ValueError) as exc:
        print(f"the category answer is not usable: {exc}", file=sys.stderr)
        return 2
    state = category_gate_state(cats, ids, defined)
    print(json.dumps(state, indent=2))
    # AN EMPTY, PARTIAL OR UNDEFINED-ID ANSWER IS NOT AN ANSWER. `[]`
    # parses, passes `[ -s ]` at two bytes and stamps nothing; a partial
    # one gates only the part it covers; an id the owner never wrote
    # excludes nothing at all. Each used to arm the gate. The REASON goes
    # to stderr so the nightly's log names what actually happened rather
    # than reciting a fixed list of causes.
    if state["state"] != "armed":
        print(state["why"], file=sys.stderr)
        return 1
    return 0
