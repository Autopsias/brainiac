"""EXC-01 — the cross-tier exception batch: one staged proposal per pair.

:mod:`brain.remediation_questions` decides that a finding is asked PER PAIR and
what the question says; this is the durable half — where the proposals live,
how long a question stays in the owner's queue, and what rotates in when one
closes. It is shaped after the COS version-link batch
(``cos/_version_links.py``): a pending file per proposal, one append-only
ledger of what happened to each pair, and one engagement line per run, so a
lane that has silently stopped staging cannot look identical to a healthy one
with nothing to do.

EXPIRED IS NOT DECIDED
----------------------
That version-link batch treats an ignored proposal as settled — the pair enters
its ledger and is never re-offered. Right for a supersession suggestion, wrong
here: a cross-tier duplicate is a live exposure of higher-tier substance
through a lower-tier copy, and it does not stop being one because nobody
answered. So a TTL expiry closes the QUESTION (the inbox slot frees and the
next-worst pair rotates in) while the PROPOSAL stays pending and re-enters the
next batch. Only an owner ANSWER retires a pair, and only as read from the
HOST-AUTHENTICATED answers ledger
(:func:`brain.remediation_state.owner_decisions`) — never from the
mount-visible ``inbox.jsonl``, where an ``answered`` line is a claim a VM can
forge.

AND THE ANSWER IS READ, NOT JUST COUNTED
----------------------------------------
The consumer reads what was DECIDED, not merely that something was. Retiring on
any answer at all made the DEFAULT (``defer``) do exactly what ACCEPT does — a
live cross-tier exposure silenced by the option that writes nothing, while the
raise the question described never ran (llm-review, s06). So ACCEPT executes
:func:`accept_pair` and only then retires; a rejection retires with no write;
``defer`` and an UNRECOGNISED answer retire nothing at all.

Where the store lives, and why
------------------------------
In :mod:`brain.remediation_exceptions_store`, host-private and vault-bound —
see that module.
"""

from __future__ import annotations

import datetime
import hashlib
import os
from pathlib import Path
from typing import Any, Mapping

from . import classification as _classification
from . import inbox as _inbox
from . import remediation_state as _rs
from . import remediation_questions as _rq
from .remediation_answers import (  # re-exported: one TTL knob, one tier rule
    accept_pair, deferred_holds, ttl_days, unanswerable_pair,
    unlabelled_finding,
    unlabelled_ids,
)
from .remediation_questions import (
    _BATCH_SOURCES, owner_cap, question_shape,
)
from .remediation_exceptions_store import (  # re-exported: the store is one place
    ANSWERED_STATE, DIRNAME, LEDGER_FILENAME, PENDING_DIRNAME, RUNS_FILENAME,
    UNLABELLED_IDS_FILENAME, write_unlabelled_ids,
    SCHEMA, STALE_SCHEMA_STATE, UNANSWERABLE_STATE, _append, _read_records,
    _record, _write_proposal, decided_pair_keys, exceptions_dir, ledger,
    pair_key, read_pending, retire as _retire, stale_pending,
)

#: The staged-but-unasked backlog, as ONE banner finding. Declared in the
#: registry (``remediation.py``) as ``banner`` so it does not render UNTRIAGED.
UNASKED_KEY = "exceptions:unasked"
RUN_EVENT = "exception-batch-run"

#: Batch questions are keyed on the PAIR, not on the finding: two detectors
#: can report the same two notes, and that is one question.
BATCH_KEY_PREFIX = "remediation-xtier:"


# ---------------------------------------------------------------------------
# Staging, expiry, asking. Run in that order, once per maintain run.
# ---------------------------------------------------------------------------
def _tier_rank(*tiers: Any) -> int:
    """The pair's exposure tier: the HIGHER of the two sides, because that is
    what the low copy leaks. Unlabelled ranks as MNPI, exactly as at the
    egress gate."""
    return max(_classification.rank(t) for t in (tiers or ("",)))


def _shown(tier: Any) -> str:
    """A tier AS RECORDED, plus what the DECISION treats it as when they differ.

    Display, never decision. A mis-cased or missing label rendered as its
    default-deny tier makes the two sides read identically — "a (MNPI) and b
    (MNPI)", a question nobody can answer — and hides the very defect that
    produced it."""
    raw = str(tier or "").strip()
    canon = _classification.normalize(raw)
    if raw == canon:
        return canon
    return f'{raw or "unlabelled"} (unrecognised — treated as {canon})'


def proposal_meta(
    finding_key: str, pair: Mapping[str, Any], vault: str | os.PathLike[str],
    today: datetime.date,
) -> dict[str, Any]:
    a, b = str(pair["a"]), str(pair["b"])
    # Stored AS RECORDED. Default-deny still decides which side is the low one
    # and what an ACCEPT raises to (`_classification.rank`/`normalize`, applied
    # at every use) — but normalizing before it is STORED renders a mis-cased
    # pair as "a (MNPI) and b (MNPI)", an undecidable question that sorts
    # worst-first and holds a queue slot until it expires (llm-review, s06).
    a_tier, b_tier = str(pair.get("a_tier") or ""), str(pair.get("b_tier") or "")
    low, low_tier, high, high_tier = (
        (a, a_tier, b, b_tier)
        if _classification.rank(a_tier) <= _classification.rank(b_tier)
        else (b, b_tier, a, a_tier))
    key = pair_key(a, b)
    meta = {
        "schema_version": SCHEMA,
        "vault": _rs.vault_binding(vault),
        "kind": "cross-tier",
        "finding_key": finding_key,
        "pair_key": key,
        "low_id": low, "low_tier": low_tier,
        "high_id": high, "high_tier": high_tier,
        "shingle_jaccard": pair.get("shingle_jaccard"),
        "word_jaccard": pair.get("word_jaccard"),
        "tier_rank": _tier_rank(a_tier, b_tier),
        "created": today.isoformat(),
        "last_asked": None,
        "ttl_expires": None,
    }
    ident = f"xtier-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:12]}"
    meta["id"] = ident
    meta["question_key"] = f"{BATCH_KEY_PREFIX}{ident}"
    return meta


def stage(
    vault: str | os.PathLike[str], finding_key: str,
    pairs: list[Mapping[str, Any]], today: datetime.date,
    *, known: set[str] | None = None,
) -> list[str]:
    """Stage one proposal per NEW pair. Ids of what was staged.

    A pair already pending, or already answered, is skipped — including when a
    second detector reports the same two notes, which is why ``known`` is
    threaded through a whole run rather than recomputed per key."""
    if known is None:
        known = ({p["pair_key"] for p in read_pending(vault)}
                 | decided_pair_keys(vault))
    staged: list[str] = []
    for pair in pairs:
        if unanswerable_pair(pair.get("a_tier"), pair.get("b_tier")):
            continue    # not a cross-tier question; `unlabelled_finding` has it
        meta = proposal_meta(finding_key, pair, vault, today)
        if meta["pair_key"] in known:
            continue
        _write_proposal(vault, meta)
        _record(vault, meta["pair_key"], "proposed", id=meta["id"],
                finding_key=finding_key, at=today.isoformat())
        known.add(meta["pair_key"])
        staged.append(meta["id"])
    return staged


def reconcile(
    core: Any, today: datetime.date,
    decisions: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[str]:
    """EXECUTE the owner's answers, then retire the pairs they settled.

    Reads the DECISIONS, not the keys. Retiring on any answer at all made the
    default — ``defer`` — do exactly what ACCEPT does, so a live cross-tier
    exposure was silenced by the option that writes nothing, and the raise the
    question described never ran (llm-review, s06). Four outcomes, one per
    :func:`brain.remediation_questions.answer_verdict` value: ACCEPT raises the
    lower copy and only then retires the pair; SETTLED retires it; DEFER and an
    UNRECOGNISED answer retire NOTHING — the proposal stays pending, stays
    reported, and is asked again after its cooldown."""
    vault = Path(core.vault)
    decisions = _rs.owner_decisions(vault) if decisions is None else decisions
    if not decisions:
        return []
    done: list[str] = []
    for meta in read_pending(vault):
        row = decisions.get(str(meta.get("question_key") or ""))
        if row is None:
            continue
        verdict = _rq.answer_verdict(
            str(meta.get("finding_key") or ""), row.get("answer"))
        if verdict in (_rq.DEFERRED, _rq.UNRECOGNISED):
            continue
        if verdict == _rq.ACCEPTED:
            refused = accept_pair(core, meta)
            if refused:
                # The exposure is NOT closed, so the pair is not retired: it
                # stays pending, stays in the unasked backlog finding, and the
                # refusal is on the ledger for the owner to read — ONCE per
                # distinct refusal, because this runs hourly and a note that
                # has left the index would otherwise append a row every hour.
                if _last_refusal(vault, str(meta["pair_key"])) != refused:
                    _record(vault, meta["pair_key"], "accept-failed",
                            id=meta["id"], at=today.isoformat(), reason=refused)
                continue
        if _retire(vault, meta, ANSWERED_STATE, verdict=verdict,
                   at=today.isoformat()):
            done.append(str(meta["id"]))
    return done


def retire_unanswerable(
    core: Any, today: datetime.date,
) -> list[dict[str, Any]]:
    """Retire every pending proposal the tier rule refuses, as PAIR records.

    A proposal whose members do not form an answerable cross-tier question —
    staged before the rule existed, or by a producer that had normalized the
    absence away — is skipped by :func:`ask_pending` and filtered out of
    :func:`unasked_finding`. Before this it therefore sat in ``pending/``
    forever: counted in ``runs.jsonl``, on the exceptions page, asked by
    nothing, surfaced by nothing. It is not an OPEN question, so it gets a
    terminal state like every other settled proposal — the pending file goes,
    the refusal goes on the ledger, and the caller reports its two members
    through ``exceptions:unlabelled``, the finding that already exists for
    exactly this defect.

    MEDIUM finding (llm-review, s06 review): retiring the PROPOSAL is not
    enough on its own — if this pair had already been ASKED, the enqueued
    owner question stayed open forever (an unanswerable pair is never
    answered), permanently consuming one of the ~5 queue slots while an
    ACCEPT on it would have been a silent no-op. So every question this call
    retires is closed through the SAME path :func:`expire_due` already uses
    (``inbox.expire_questions``) — retiring is not a decision any more than
    expiry is, but it must free the slot exactly like expiry does.

    It CONVERGES and it is not a decision: ``decided_pair_keys`` reads only
    ``answered``, so once the note carries a canonical classification the
    metric re-stages the pair as a real question."""
    vault = Path(core.vault)
    out: list[dict[str, Any]] = []
    closing: set[str] = set()
    for meta in read_pending(vault):
        why = unanswerable_pair(meta.get("low_tier"), meta.get("high_tier"))
        if not why or not _retire(vault, meta, UNANSWERABLE_STATE,
                                  at=today.isoformat(), reason=why):
            continue
        closing.add(str(meta["question_key"]))
        out.append({"a": meta.get("low_id"), "a_tier": meta.get("low_tier"),
                    "b": meta.get("high_id"), "b_tier": meta.get("high_tier")})
    if closing:
        entries, closed = _inbox.expire_questions(
            core._read_inbox(), closing, expired=today.isoformat())
        if closed:
            core._write_inbox(entries)
    return out


def retire_stale_schema(
    vault: str | os.PathLike[str], today: datetime.date,
) -> list[dict[str, Any]]:
    """Garbage-collect a proposal an OLDER schema wrote — HIGH finding
    (llm-review, s06 review).

    ``read_pending`` already refuses to hand one of these to ``ask_pending``/
    ``accept_pair``: an earlier build of this feature normalized a pair's
    tier fields before storing them, so a missing classification was written
    as the literal string ``"MNPI"`` — indistinguishable, once on disk, from
    a note genuinely classified MNPI, and the stored value can never be
    trusted again. Left alone such a file is simply invisible forever
    (excluded by :func:`read_pending`'s exact-schema match, never cleaned
    up); this ends it on the ledger instead, the same terminal treatment
    every other settled proposal gets. It was never asked (only
    `read_pending`'s set is ever asked), so there is no owner question to
    close — unlike :func:`retire_unanswerable`."""
    out: list[dict[str, Any]] = []
    for meta in stale_pending(vault):
        if _retire(vault, meta, STALE_SCHEMA_STATE, at=today.isoformat(),
                   reason=f"schema {meta.get('schema_version')!r} predates "
                          f"{SCHEMA!r} — its stored tier is not trusted"):
            out.append(meta)
    return out


def _last_refusal(vault: str | os.PathLike[str], pair: str) -> str:
    """The reason on this pair's most recent ledger row, if it was a refusal."""
    rows = [r for r in ledger(vault) if r.get("pair_key") == pair]
    if rows and rows[-1].get("state") == "accept-failed":
        return str(rows[-1].get("reason") or "")
    return ""


def expire_due(core: Any, today: datetime.date) -> list[str]:
    """Close the questions whose TTL has run out — and keep their proposals.

    Expiry frees the inbox slot so the next-worst pair can rotate in. It is
    NOT a decision: the proposal stays pending, stays on the exceptions page,
    and is asked again when a slot frees. Only an answer retires a pair."""
    vault = Path(core.vault)
    due = [m for m in read_pending(vault)
           if m.get("ttl_expires") and str(m["ttl_expires"]) <= today.isoformat()]
    if not due:
        return []
    entries, closed = _inbox.expire_questions(
        core._read_inbox(), {str(m["question_key"]) for m in due},
        expired=today.isoformat())
    if closed:
        core._write_inbox(entries)
    for meta in due:
        _write_proposal(vault, {**meta, "ttl_expires": None})
        _record(vault, meta["pair_key"], "expired", id=meta["id"],
                at=today.isoformat())
    return [str(m["id"]) for m in due]


def _worst_first(meta: Mapping[str, Any]) -> tuple[int, str, str]:
    """Highest classification first, then least recently asked (never-asked
    first), then oldest. The rotation order the grill ruling names."""
    return (-int(meta.get("tier_rank") or 0), str(meta.get("last_asked") or ""),
            str(meta.get("created") or ""))


def ask_pending(
    core: Any, today: datetime.date, *, hold: set[str] | None = None,
) -> list[str]:
    """Fill the free owner-queue slots, worst-first. Ids of what was asked.

    ``hold`` is :func:`deferred_holds` — the questions the owner said "not now"
    to, which keep their proposal but skip the queue until the cooldown ends."""
    vault = Path(core.vault)
    pending = read_pending(vault)
    if not pending:
        return []
    open_keys = {str(e.get("key")) for e in _inbox.open_questions(core._read_inbox())}
    slots = owner_cap() - len(open_keys)
    asked: list[str] = []
    for meta in sorted(pending, key=_worst_first):
        if slots <= 0:
            break
        if str(meta["question_key"]) in open_keys | (hold or set()):
            continue
        if unanswerable_pair(meta.get("low_tier"), meta.get("high_tier")):
            continue    # unreachable while `retire_unanswerable` runs first;
                        # kept because asking one is the harm this lane exists
                        # to stop, and a skipped question costs nothing
        if not core.enqueue_question(batch_question(meta),
                                     source=f"remediation:{meta['finding_key']}",
                                     today=today):
            continue
        _write_proposal(vault, {
            **meta, "last_asked": today.isoformat(),
            "ttl_expires": (today + datetime.timedelta(
                days=ttl_days())).isoformat()})
        _record(vault, meta["pair_key"], "asked", id=meta["id"],
                at=today.isoformat())
        slots -= 1
        asked.append(str(meta["id"]))
    return asked


def batch_question(meta: Mapping[str, Any]) -> dict[str, Any]:
    """ONE decidable question about ONE pair: what was found, the options, the
    default, and what expiry does. Nothing here tells the owner to go and read
    a queue — an interactive session asks it directly."""
    finding_key = str(meta.get("finding_key") or "")
    options, default, _source = question_shape(finding_key)
    _metric, _field, phrase = _BATCH_SOURCES.get(
        finding_key, ("", "", "are a cross-tier pair"))
    verdict = phrase.format(
        shingle_jaccard=meta.get("shingle_jaccard"),
        word_jaccard=meta.get("word_jaccard"))
    low_tier, high_tier = _shown(meta["low_tier"]), _shown(meta["high_tier"])
    low_deny = _classification.normalize(meta["low_tier"])
    high_deny = _classification.normalize(meta["high_tier"])
    return {
        "key": str(meta["question_key"]),
        "question": (
            f"`{meta['low_id']}` ({low_tier}) and `{meta['high_id']}` "
            f"({high_tier}) {verdict}, so a reader capped at {low_deny} reaches "
            f"{high_deny} substance through the lower copy. What should happen?"),
        "options": list(options),
        "default": default,
        "context": (
            f"Cross-tier exception `{meta['id']}` from `{finding_key}`. "
            "ACCEPT raises the lower copy to "
            f"{high_deny} through the audited write path that already only ever "
            "raises a classification (`remediation_folds.audited_write`, "
            "raise_classification); it never lowers one, and the change is "
            f"reversible through the same path. The default ({default}) writes "
            f"nothing. Unanswered, this question closes after {ttl_days()} "
            "day(s) to free the queue — but expiry is NOT a decision: the pair "
            "stays on the exceptions list and is asked again when a slot "
            "frees. Only an answer retires it."),
        "source": f"remediation:{finding_key}",
    }


def apply_batches(
    core: Any, findings: list[tuple[str, str]],
    pairs: Mapping[str, list[Mapping[str, Any]]], today: datetime.date,
) -> list[tuple[str, str]]:
    """Convert every batch-class finding, then ask what fits the cap.

    Returns the findings that must still BANNER — a key whose detector handed
    over no pairs, or a store this host could not write. A conversion that
    cannot happen is loud, never a silently swallowed exposure."""
    vault = Path(core.vault)
    # A pair the tier rule refuses is NOT a cross-tier question, so it may
    # not decide that a key is convertible either: a finding whose whole
    # population is unanswerable BANNERS, and its unlabelled members get
    # their own finding — never a silent drop.
    real = {k: [p for p in rows
                if not unanswerable_pair(p.get("a_tier"), p.get("b_tier"))]
            for k, rows in pairs.items()}
    convertible = [(k, t) for k, t in findings if real.get(k)]
    deferred = [(k, t) for k, t in findings if not real.get(k)]
    unlabelled = unlabelled_finding(pairs, write_unlabelled_ids(vault, pairs))
    try:
        # Reconcile and expire run whether or not there is anything to stage.
        # A metric back at its floor produces no finding, and the answers to
        # the questions it left open still have to land — otherwise a pair the
        # owner ruled on sits pending forever, on the exceptions page, in a
        # vault with nothing wrong with it.
        decisions = _rs.owner_decisions(vault)
        reconcile(core, today, decisions)
        # …and retire what can never BE answered, before the counts below are
        # taken off `pending`. Its members join this run's own population in
        # the unlabelled finding, so a proposal that leaves the store is never
        # quieter than one that stays.
        refused = retire_unanswerable(core, today)
        # …and garbage-collect anything an older, no-longer-trusted schema
        # left behind — never counted in `unlabelled`, since a stale record's
        # STORED tier is exactly what this refuses to believe.
        stale = retire_stale_schema(vault, today)
        merged = {**pairs, UNANSWERABLE_STATE: refused}
        unlabelled = unlabelled_finding(
            merged, write_unlabelled_ids(vault, merged))
        known = {p["pair_key"] for p in read_pending(vault)} | decided_pair_keys(vault)
        staged: list[str] = []
        for key, _text in convertible:
            staged.extend(stage(vault, key, list(real[key]), today, known=known))
        expired = expire_due(core, today)
        asked = ask_pending(core, today, hold=deferred_holds(decisions, today))
        pending = read_pending(vault)
        if convertible or pending or expired or refused or stale:
            _append(exceptions_dir(vault) / RUNS_FILENAME, vault, {
                "event": RUN_EVENT, "at": today.isoformat(),
                "keys": sorted(k for k, _t in convertible),
                "staged": len(staged), "asked": len(asked),
                "expired": len(expired), "pending": len(pending),
                "unanswerable": len(refused), "stale_schema": len(stale)})
        return deferred + unlabelled + unasked_finding(core, pending)
    except Exception:  # noqa: BLE001 — an unusable store must banner, not vanish
        return deferred + unlabelled + convertible


def unasked_finding(
    core: Any, pending: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    """The staged exceptions that are NOT in the owner's queue, as ONE finding.

    The cap is a push rate and the store holds the full set — but nothing
    RENDERS that store until s07 builds the exceptions page, so a pair staged
    while the queue was full reached no surface at all: not the queue, not the
    banner, not the ``brain alerts`` feed (llm-review, s06). This is the
    interim surface, and it is deliberately ONE aggregate line: a banner per
    pair would turn the push rate back into noise. Counts and the worst tier
    only — the ids stay in the host-private store rather than in the
    mount-visible findings feed."""
    open_keys = {str(e.get("key")) for e in _inbox.open_questions(core._read_inbox())}
    waiting = [m for m in pending
               if str(m.get("question_key")) not in open_keys
               and not unanswerable_pair(m.get("low_tier"), m.get("high_tier"))]
    if not waiting:
        return []
    tier = _classification.TIERS[max(int(m.get("tier_rank") or 0) for m in waiting)]
    return [(UNASKED_KEY, (
        f"{len(waiting)} cross-tier exception(s) staged and not in the owner "
        f"queue (cap {owner_cap()}); the worst is a {tier} exposure. Nothing "
        "is lost — each is asked as a slot frees, so answering an open "
        "question rotates the next one in."))]
