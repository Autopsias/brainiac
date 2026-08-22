"""EXC-01 — an owner-class finding becomes ONE decidable question.

The registry (:mod:`brain.remediation`) rules that a key is ``owner``; the
routing step (:mod:`brain.remediation_routing`) sends it here. This module owns
WHAT IS ASKED: which owner keys are asked per pair and which as singletons, the
options and default each one carries, and the subject a question is keyed on.
The durable per-pair batch itself — the store, the TTL, the rotation — is
:mod:`brain.remediation_exceptions`, which reads its tables from here.

ONE ROUTER, and it reads the FINDING KEY alone
----------------------------------------------
:data:`BATCH_KEYS` is the whole decision. A key in it is a per-PAIR family and
never becomes an aggregate inbox question: it stages one proposal per pair.
Every other owner key is a SINGLETON — one inbox question, keyed on the finding
plus a stable subject id. Nothing else is consulted, so a finding cannot enter
both surfaces, which is the double-entry this item's counter-move names. A
batch key whose pairs are unavailable on a given run (an errored detector, an
alerts-only path with no fold results) BANNERS instead: loud beats a silent
half-conversion.

The cap is a PUSH rate, not the exception set
---------------------------------------------
``$BRAIN_REMEDIATION_OWNER_CAP`` (5) bounds how many questions sit in the
owner's queue at once. Every pair is staged whatever the cap says; the store
holds the full set and a freed slot refills from the remainder. Nothing is
starved by the cap — it only decides what is asked FIRST. And nothing this
module emits ever tells the owner to go and read a queue: an interactive
session asks these directly (owner-questions-via-dialogue).
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Mapping

OWNER_CAP_ENV = "BRAIN_REMEDIATION_OWNER_CAP"
DEFAULT_OWNER_CAP = 5

#: The owner-class findings asked PER PAIR through the proposal batch. Read
#: this and nothing else to decide batch vs singleton.
BATCH_KEYS = frozenset({
    "invariant:cross_tier_duplicates",
    "invariant:cross_tier_candidates",
    "invariant:cross_tier_twins",
})

#: Which metric field carries each batch key's pair population, and how the
#: pair reads in the question. Kept beside BATCH_KEYS so a fourth cross-tier
#: detector cannot be routed to the batch without also saying where its pairs
#: come from and what the owner is being told they are.
_BATCH_SOURCES: dict[str, tuple[str, str, str]] = {
    "invariant:cross_tier_duplicates": (
        "cross_tier_duplicates", "pairs",
        "are the SAME document (5-word-shingle overlap {shingle_jaccard})"),
    "invariant:cross_tier_candidates": (
        "cross_tier_candidates", "pairs",
        "share the same substance but the detector will NOT decide whether "
        "they are one document (word-set overlap {word_jaccard}, shingle "
        "overlap {shingle_jaccard} — below the decided band)"),
    "invariant:cross_tier_twins": (
        "cross_tier_twins", "cross_pairs",
        "are an id twin pair (`<date>-<slug>` / `<slug>`) at two different "
        "classifications"),
}


def is_batch_key(key: str) -> bool:
    """THE router. Batch or singleton, decided on the key and nothing else."""
    return key in BATCH_KEYS


def owner_cap() -> int:
    """How many owner questions may sit in the queue at once. A PUSH rate —
    see the module docstring: nothing is dropped because the cap is reached."""
    try:
        return max(1, int(os.environ.get(OWNER_CAP_ENV, "").strip()
                          or DEFAULT_OWNER_CAP))
    except ValueError:
        return DEFAULT_OWNER_CAP


# ---------------------------------------------------------------------------
# Owner questions — one decidable question, options + a stated default.
# ---------------------------------------------------------------------------
#: The one answer in this lane that settles NOTHING. It is the default of the
#: cross-tier shape and of the generic shape, and both question texts promise
#: the finding comes back — so it may never retire a pair or silence a key.
DEFER_ANSWER = "defer"

#: The one owner answer in this lane that MAKES A WRITE: the ACCEPT action the
#: frozen disposition table prescribes for a cross-tier pair. Named here, beside
#: the options it is one of, so the executor in
#: :mod:`brain.remediation_exceptions` matches the string the owner was shown
#: rather than a second copy of it.
CROSS_TIER_ACCEPT = (
    "raise the lower copy to the higher tier through the audited write path")
CROSS_TIER_REJECT = "record the pair as not a duplicate and stop asking"

_CROSS_TIER_SHAPE: tuple[tuple[str, ...], str] = (
    (CROSS_TIER_ACCEPT, CROSS_TIER_REJECT, DEFER_ANSWER), DEFER_ANSWER)

#: The options and default the owner is shown, per owner-class finding, from
#: the frozen disposition table (design-freeze (c)).
#:
#: **Keyed EXACTLY, on purpose.** It used to match on prefixes, and a second
#: key under an existing prefix then silently inherited options written for a
#: different finding: ``tamper:redirected-write`` was asked "admit the file
#: through `brain write` / remove it from the vault / leave it unsigned", none
#: of which means anything for a concurrent unaudited write, and no answer to
#: which was ever executed. Every ENUMERABLE owner key therefore names its own
#: row — a shared family shares one CONSTANT explicitly (see
#: ``_CROSS_TIER_SHAPE``), which is a deliberate act rather than an accident of
#: spelling (llm-review, 2026-08-21).
QUESTION_SHAPES: dict[str, tuple[tuple[str, ...], str]] = {
    "invariant:unreachable_gold": (
        ("reinstate the file from inbox/_quarantine/_resolved/ into vault/raw/",
         "retire the label from the golden set",
         DEFER_ANSWER), DEFER_ANSWER),
    "invariant:cross_tier_duplicates": _CROSS_TIER_SHAPE,
    "invariant:cross_tier_candidates": _CROSS_TIER_SHAPE,
    "invariant:cross_tier_twins": _CROSS_TIER_SHAPE,
    # FIX-01: an unsigned note whose bytes no host record covers. The default
    # is deliberately the one that changes nothing — a signature granted by
    # default is the failure this exception exists to prevent — and it SAYS the
    # finding stays open, which is why the registry marks the key
    # `keeps_blocking`. Two of the three options end the condition by removing
    # it (a signed file leaves the target set; so does a deleted one); the
    # third is the only branch where it survives the answer, and there the
    # owner has explicitly asked to keep seeing it.
    "tamper:unsigned-note": (
        ("admit the file through `brain write` (it becomes host-signed content)",
         "remove it from the vault",
         "leave it unsigned and keep the finding open"),
        "leave it unsigned and keep the finding open"),
    # A write that did not land where it was signed for. Its own row, because
    # nothing above describes it: the batch has already stopped itself, so the
    # default changes nothing and keeps the finding visible while the owner
    # hunts the concurrent writer.
    "tamper:redirected-write": (
        ("stop the lane now (`BRAIN_REMEDIATION_DISABLED=1`) until the "
         "concurrent writer is found",
         "repair the note by hand and re-verify with "
         "`brain verify-audit --check-content`",
         "leave the lane running — it stops its own batch each run — and keep "
         "the finding open"),
        "leave the lane running — it stops its own batch each run — and keep "
        "the finding open"),
    # SPD-01: a remediation cost regression. There is no cap to apply, so the
    # DEFAULT is simply acknowledging the spend as expected — it settles the
    # question rather than promising it stays open (the metric itself keeps
    # trending regardless of the answer, and a genuinely repeating regression
    # raises again on its own next week-over-week comparison).
    "trend:remediation_cost": (
        ("investigate what the model-backed branch spent on",
         "acknowledge the spend as expected"),
        "acknowledge the spend as expected"),
}

#: Genuinely OPEN vocabularies, where the members cannot be enumerated so a
#: prefix is the only honest match. ``quarantine:`` has 26+ reasons derived by
#: AST from the producers themselves (see
#: ``tests/test_remediation_enforcement.py::quarantine_reasons``). Adding a row
#: here is how a family says "my membership is open"; everything else must be
#: exact.
QUESTION_SHAPE_PREFIXES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("quarantine:",
     ("fix the cause and return the file to inbox/",
      "leave it quarantined",
      "discard it"), "leave it quarantined"),
    # FIX-03: extract_retry's own exhaustion exception. The finding TEXT (built
    # by the branch, not here) names the file and quotes its own
    # `_QUARANTINE_REMEDY`/subfloor remedy back to the owner; this row is only
    # the options — apply the remedy the finding already named, or stop.
    ("quarantine-exhausted:",
     ("apply the remedy named in the finding, then return the file to "
      "inbox/ and run `brain sync`",
      "leave it quarantined",
      "discard it"), "leave it quarantined"),
)
_GENERIC_OPTIONS = ("act on it now", DEFER_ANSWER)
_GENERIC_DEFAULT = DEFER_ANSWER


def question_shape(
    key: str,
    shapes: dict[str, tuple[tuple[str, ...], str]] | None = None,
    prefixes: tuple[tuple[str, tuple[str, ...], str], ...] | None = None,
) -> tuple[tuple[str, ...], str, str]:
    """``(options, default, source)`` for ``key``.

    ``source`` is ``exact`` / ``prefix`` / ``generic`` — returned rather than
    hidden so the wiring audit can tell a key that names its own options from
    one that merely happens to start with someone else's prefix. The tables are
    injectable for the same reason: the audit's known-positive probe feeds it a
    deliberately half-wired key."""
    shapes = QUESTION_SHAPES if shapes is None else shapes
    prefixes = QUESTION_SHAPE_PREFIXES if prefixes is None else prefixes
    if key in shapes:
        options, default = shapes[key]
        return options, default, "exact"
    for prefix, options, default in prefixes:
        if key.startswith(prefix):
            return options, default, "prefix"
    return _GENERIC_OPTIONS, _GENERIC_DEFAULT, "generic"


#: What an owner's answer DID, for the two consumers of the answers ledger.
#: Three of the four are NOT "the finding is over".
ACCEPTED, SETTLED, DEFERRED, UNRECOGNISED = (
    "accepted", "settled", "deferred", "unrecognised")


def answer_verdict(
    finding_key: str, answer: object, options: tuple[str, ...] | list[str] | None = None,
) -> str:
    """Classify one owner answer against the options it was actually asked with.

    ``options`` is the QUESTION's own list where the caller has it. It is not
    always derivable from the key: the thrashing question
    (``remediation_routing.thrashing_question``) is built by hand and its
    options appear in no shape table, so re-deriving them from the key would
    make every answer to it unreadable — and an unreadable answer is asked
    again, forever. The batch has no question record at reconcile time, only
    the pair's ``finding_key``, and for it the table IS the authority.

    * :data:`ACCEPTED` — the one answer that maps to an ACTION (the cross-tier
      raise). The consumer executes it before it retires anything.
    * :data:`SETTLED` — a real decision that needs no write; the question is
      never asked again.
    * :data:`DEFERRED` — the default. The question text promises the finding
      comes back, so it must not retire the pair or silence the key.
    * :data:`UNRECOGNISED` — the answer is not one of the options at all
      (free text, a stale option, a forged row). It FAILS CLOSED: "I could not
      read it" is never consent to stop reporting a live exposure."""
    if options is None:
        options, _default, _source = question_shape(finding_key)
    text = str(answer or "").strip()
    if text not in options:
        return UNRECOGNISED
    if text == DEFER_ANSWER:
        return DEFERRED
    return ACCEPTED if text == CROSS_TIER_ACCEPT else SETTLED


# ---------------------------------------------------------------------------
# What the finding is ABOUT — the subject a question is keyed on.
#
# Without one, an owner-class key is a bare constant: the first answer enters
# the host-authenticated ledger and EVERY later occurrence is skipped as
# already-decided, however different the documents behind it are. The repair
# branches publish a target signature for the keys they own (REG-03); the
# keys nothing repairs — `unreachable_gold`, an unreadable quarantine drop —
# have no branch to publish one, so their subject is derived here from the
# population the detector actually found.
# ---------------------------------------------------------------------------
def digest(values: list[str]) -> str:
    """sha256 over a sorted population — the same shape as
    ``remediation_folds.target_set_hash``, so one notion of "these targets"
    serves the binding and the question key alike."""
    return hashlib.sha256(
        "\n".join(sorted(values)).encode("utf-8")).hexdigest()


def named(values: list[str], limit: int = 5) -> str:
    shown = ", ".join(f"`{v}`" for v in sorted(values)[:limit])
    more = f" (+{len(values) - limit} more)" if len(values) > limit else ""
    return shown + more


def subjects_from_results(
    results: Mapping[str, Any] | None,
) -> dict[str, dict[str, str]]:
    """``{finding key: {"hash", "detail"}}`` for the owner-class SINGLETONS.

    ``detail`` is appended to the question text, because a question that says
    "19 gold documents are unreachable" cannot be answered — "reinstate them
    from the ``_resolved`` batch" needs to know WHICH."""
    metrics = (results or {}).get("corpus_invariants")
    if not isinstance(metrics, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    row = metrics.get("unreachable_gold")
    docs = row.get("unreachable_docs") if isinstance(row, dict) else None
    if isinstance(docs, list) and docs:
        docs = [str(d) for d in docs if str(d).strip()]
        out["invariant:unreachable_gold"] = {
            "hash": digest(docs),
            "detail": ("no ranking leg reaches " + named(docs)
                       + _bucket_split(row))}
    return out


def _bucket_split(row: Mapping[str, Any]) -> str:
    """The WHY, not just the what. Measured on the reference vault (2026-08-21):
    one question covered 106 labels over 85 documents whose causes wanted four
    different answers — 19 absent from the index (the reinstate option), 22
    reachable only multi-hop, 16 reachable with a CON-01 variant, 49 a genuine
    ranking residual. Without the split the owner is choosing one remedy for
    four populations blind."""
    buckets = row.get("buckets")
    if not isinstance(buckets, dict):
        return ""
    parts = [f"{name.replace('_', ' ')} {count}"
             for name, count in sorted(buckets.items()) if count]
    return f" (by cause: {', '.join(parts)})" if parts else ""


def pairs_from_results(
    results: Mapping[str, Any] | None,
) -> dict[str, list[dict[str, Any]]]:
    """``{batch key: [pair records]}`` — the population each cross-tier
    proposal is staged from. An absent or errored metric yields no pairs,
    which makes its finding banner rather than convert silently."""
    metrics = (results or {}).get("corpus_invariants")
    if not isinstance(metrics, dict):
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for key, (metric, field, _phrase) in _BATCH_SOURCES.items():
        row = metrics.get(metric)
        pairs = row.get(field) if isinstance(row, dict) else None
        rows = [p for p in (pairs if isinstance(pairs, list) else [])
                if isinstance(p, dict) and p.get("a") and p.get("b")]
        if rows:
            out[key] = rows
    return out
