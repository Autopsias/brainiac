"""Outbound term guard (SEC-07) — refuse a protected term leaving in a tool call.

**The gap this closes.** The classification gate protects the READ side: it
decides what a caller may see. It says nothing about what the model then does
with it. ``AGENTS.md``'s retrieval discipline rule 4 names the consequence —
"the model's own web-search tool is an *ungated outbound channel*, and the
query string itself is the leak" — and until this module that rule was PROSE,
enforced by the model's compliance. A hostile document can carry an instruction
that puts a codename into a search query; nobody reviews a query string, and by
the time the results come back the term has already reached a third party.

**Why the terms are not in this repo, and never will be.** A shipped list of
one owner's counterparties is useless to every other owner and a disclosure in
itself. The terms come from the vault's own **overlay** — ``overlay/keywords/``,
the per-owner decoder ring whose rows are ``| Term | Expansion | Classification |``
— read through :func:`brain.overlay.match_keyword_tier`. The engine ships the
RULE; the vault supplies the DATA. That is the same split the overlay exists for
(``overlay/README.md``), and those rows already drive ingest classification
(``provenance.py``) and the COS grounding lane (``cos_ground.py``), so they stay
current as a side effect of work the owner already does rather than as a second
list to curate.

**An empty decoder ring is REPORTED, never a quiet pass.** A vault that maps no
terms cannot have a term caught, and a check whose all-clear means "nothing was
looked at" is worse than no check. :func:`check` always returns
``mapped_terms``, every caller surfaces it, and ``--strict`` turns an empty ring
into a refusal for a deployment that wants fail-closed.

**What this does NOT do.** It matches DECLARED terms, on word boundaries, case
insensitively. It does not catch a paraphrase, a codename nobody wrote down, or
a fact the model restates in its own words. It raises the bar for the terms an
owner has declared. It does not close the channel, and no caller should describe
it as though it did.
"""
from __future__ import annotations

import os
from typing import Any

from . import classification as cls

#: Terms at or above this tier refuse. `Internal` is deliberately NOT the
#: default: an owner's whole vault is Internal by default, so defaulting there
#: would refuse almost every query and the guard would be switched off within a
#: day. `Confidential` is the first tier that means "this one is actually
#: sensitive".
DEFAULT_MIN_TIER = "Confidential"

#: Exit code for a refused text. Distinct from 3 (VM refusal), 4 and
#: 5 (SEC-06 record failure) so a hook can tell them apart.
EXIT_REFUSED = 6

MIN_TIER_ENV_VAR = "BRAIN_EGRESS_TERM_MIN_TIER"


def min_tier() -> str:
    """The configured refusal threshold, fail-CLOSED on an unrecognised value.

    A typo must never silently widen what may leave: the only reason to set
    this var is to change the threshold deliberately, so an unreadable value
    falls to the most protective tier rather than the permissive default.
    Same rule, and same reasoning, as ``mcp_verbs._egress_ceiling_tier``.
    """
    raw = os.environ.get(MIN_TIER_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_MIN_TIER
    return raw if raw in cls.RANK else cls.TIERS[0]


def check(
    text: str,
    *,
    vault: str | os.PathLike[str] | None = None,
    overlay_dir: str | os.PathLike[str] | None = None,
    threshold: str | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    """Judge one outbound string against this vault's overlay keyword tiers.

    Returns ``allowed`` plus everything a caller needs to explain itself:
    the matched ``term`` and its ``tier``, the ``threshold`` applied, and
    ``mapped_terms`` — the size of the decoder ring that was actually
    consulted, so a zero is visible rather than indistinguishable from a pass.
    """
    from . import overlay as ov

    if threshold is None:
        bar = min_tier()
    elif threshold in cls.RANK:
        bar = threshold
    else:
        bar = cls.TIERS[0]  # fail-closed on a typo, as `min_tier` does
    mapped = ov.resolve_keyword_tiers(vault, overlay_dir)
    tier, term = (None, None)
    if mapped and text:
        tier, term = ov.match_keyword_tier(text, vault, overlay_dir)

    refused = bool(tier) and cls.RANK[tier] >= cls.RANK[bar]
    no_terms = not mapped
    return {
        "allowed": not refused and not (strict and no_terms),
        "refused_for": "term" if refused else ("no-mapped-terms" if strict and no_terms else None),
        "term": term if refused else None,
        "tier": tier if refused else None,
        "threshold": bar,
        "mapped_terms": len(mapped),
        "strict": strict,
    }


def explain(result: dict[str, Any]) -> str:
    """One line a hook can print, saying WHY — including on the allow path.

    The empty-decoder-ring case gets its own sentence on purpose: a caller
    reading only "allowed" would report a vault that protects nothing as a
    vault that found nothing.
    """
    if result.get("refused_for") == "term":
        return (f"REFUSED: '{result['term']}' is classified {result['tier']} in this "
                f"vault's overlay keywords (threshold {result['threshold']}). "
                f"The query string itself is an egress event — rephrase without the term, "
                f"or stay in the vault.")
    if result.get("refused_for") == "no-mapped-terms":
        return ("REFUSED (--strict): this vault's overlay maps NO keyword tiers, so "
                "nothing could be checked. Fill overlay/keywords/ or drop --strict.")
    if not result.get("mapped_terms"):
        return ("allowed — but this vault's overlay maps NO keyword tiers, so no term "
                "could have been caught. This is not evidence the text is safe.")
    return (f"allowed — {result['mapped_terms']} mapped term(s) checked at "
            f"threshold {result['threshold']}")
