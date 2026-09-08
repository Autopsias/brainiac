"""The night reader and writer of `cos_judge` — `load_night`, `write_night` (batch-2 drain).

Moved verbatim out of `cos_judge` and re-imported by it: the tests monkeypatch
`cos_judge.load_night` / `cos_judge.write_night`, and the parent-side callers
(`judge_night`, `main`) resolve both through the parent's globals at CALL time,
so those patches keep governing exactly as before.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cos_signals                                              # noqa: E402
import cos_signals_stale                                        # noqa: E402
from cos_judge_rules import _age_days  # noqa: E402
import cos_voice                       # noqa: E402

# ---------------------------------------------------------------------------
# a night: batches out, verdicts in
# ---------------------------------------------------------------------------
def _ledger(vault: Path, run_id: str) -> Path:
    from brain import cos                                        # noqa: PLC0415
    return cos.run_ops_dir(vault) / f"_cos_ingestion_ledger_{run_id}.jsonl"


def load_categories(path: Path | None) -> dict[str, str]:
    """The pre-draw category batch's answer, `{conversation_id: category}`.

    ONE PARSER, IMPORTED, AND IT RAISES. `cos_driver.load_categories` holds the
    only schema; a second copy here is how the driver and the judge come to
    disagree about which rows were excluded, which is the difference between an
    armed gate and a decorative one. A malformed file raises out of here rather
    than degrading to `{}`: the nightly validates the answer BEFORE either leg
    is handed `--categories`, so a file that reaches this point and does not
    parse means the two legs were given different inputs.
    """
    if path is None:
        return {}
    import cos_driver                                             # noqa: PLC0415
    return cos_driver.load_categories(path)


def load_selection(path: Path | None) -> set[str] | None:
    """The one parser for the model-population allowlist.

    The selected enumeration is host-produced by ``cos_batch_session``.  A
    malformed or unreadable file raises: missing input cannot mean an empty,
    safe batch because that would turn a broken cap into an unbounded model
    call.
    """
    if path is None:
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    rows = value.get("rows") if isinstance(value, dict) else value
    if not isinstance(rows, list):
        raise ValueError("selection must contain a rows list")
    ids: set[str] = set()
    for row in rows:
        cid = row.get("conversation_id") if isinstance(row, dict) else None
        if not isinstance(cid, str) or not cid:
            raise ValueError("selection row has no conversation_id")
        if cid in ids:
            raise ValueError(f"selection repeats conversation_id {cid!r}")
        ids.add(cid)
    return ids


def _row_ctx(row: dict[str, Any], c: dict[str, Any],
             night: dict[str, Any]) -> dict[str, Any]:
    """The per-row context ONE thread's rules read. Split out of `load_night`
    at the 100-line function bound; `night` carries the facts read once per
    night (the taxonomy, the sender census, the spine, the draft census)."""
    prov = c.get("provenance") or {}
    text = c.get("text") or ""
    cid = row["conversation_id"]
    ctx = {
        "sender": prov.get("sender"),
        "subject": prov.get("subject"),
        "typed_fields_available": bool(prov.get("subject") or prov.get("sender")),
        "read_state": row.get("read_state"),
        # ONLY THE UNAMBIGUOUS CHIP ASSERTS A TIER (DOCTRINE v7 §4.1).
        # `P3 · Read` is written for `read`/P2, `read`/P3 AND `act`/P3, so
        # feeding it to `triage.tier_vocabulary` as "this thread IS P3"
        # would reject tonight's honest `read`/P2 verdict as contradicting
        # a chip that never claimed a tier. The driver stamps which kind of
        # chip the tier came from (`cos_driver._tier_source`); anything
        # else — including a row from a pre-v7 ledger, which carries the
        # priority-chip source verbatim — behaves exactly as before.
        "chip_tier": (row.get("tier")
                      if row.get("tier_source") != "outlook-read-chip"
                      else None),
        "priority_map": {},
        "body_opened": bool(row.get("body_opened")),
        "body_chars": int(row.get("body_chars") or 0),
        "text": text,
        "text_len": len(text),
        "taxonomy": night["taxonomy"],
        # (INGEST-01) The two inputs the `ingest` field needs and nothing
        # else does: the taxonomy DOCUMENT (for its `mode`) and the row's
        # own attachment list (a real file widens a `text` lane to `both`).
        "taxonomy_doc": night["taxonomy_doc"],
        "attachments": row.get("attachments") or [],
        "sender_rows_this_run": night["senders"].get(prov.get("sender"), 0),
        # The PRE-DRAW stamp, carried through as an input to every later
        # batch and as the verdict field itself. Absent (`None`) when no
        # category batch ran, which is the honest feature-off shape.
        "category": night["categories"].get(cid) or None,
        "drafts_inventory": [],
        "cos_draft_convids": [],
        # THE ONE DRAFT FACT THAT HAS A PRODUCER (review 2026-08-25, finding
        # 5). Kept BESIDE the two empty lists above rather than filling them:
        # those drive `first_failed_screen` and the `Held · draft` /
        # `Held · drafted` split, whose vocabulary distinguishes the OWNER'S
        # draft from COS's own, and this census cannot tell them apart.
        # Widening them off a fact that conflates the two would relabel holds
        # across the whole judge; the archive lanes only ever ask "is there an
        # unsent draft on this thread", and that is what this answers.
        "thread_carries_draft": (bool(row.get("isDraft"))
                                 or cid in night["drafted_convids"]),
        # The owner's `overlay/cos/auto-archive.md` lever (ruling 2026-09-02),
        # read ONCE per night in `load_night` and carried per row so belt 1 is
        # still a pure function of its context. It waives the draft clause of
        # the AGED-READ belt only; the fact above is still published, so the
        # hold vocabulary and every other reader see the draft exactly as
        # before.
        "archive_over_draft": night.get("archive_over_draft", False),
        # The SECOND lever off the same overlay file (ruling 2026-09-02,
        # option 1). It does not change one verdict; it tells the staging rules
        # that a `never`-category row was READ on purpose tonight, so a real
        # disposition on such a row is the owner's instruction rather than the
        # rule-1¾ breach it would otherwise be. Zero candidates still holds.
        "read_never_categories": night.get("read_never_categories", False),
        "proposals_dropped": night["proposals_dropped"],
        "ask_age_days": _age_days(row.get("received")),
    }
    ctx.update(cos_signals.signals_for_row(
        row, c, now=night["now"], open_commitments=night["commitments"]))
    # (STALE-01) The stale lane's own two host facts, from the sibling
    # producer. Same rule as its parent: computed here from this run's own
    # captured data, never read off a model answer.
    ctx.update(cos_signals_stale.stale_signals_for_row(
        row, c, now=night["now"]))
    return ctx


def load_night(vault: Path, run_id: str,
               categories: dict[str, str] | None = None,
               selection: set[str] | None = None) -> dict[str, Any]:
    """The driver's own output, plus the per-row context the rules need.

    `typed_fields_available` is the load-bearing field. A row whose sender and
    subject this run never persisted CANNOT be triaged — Phase 1.5 judges from
    typed fields and nothing else (INJ-03) — so it is reported as such rather
    than bucketed from its timestamp.
    """
    from brain import cos, cos_corpus                            # noqa: PLC0415

    rows = [json.loads(x) for x in
            _ledger(vault, run_id).read_text(encoding="utf-8").splitlines() if x.strip()]
    if selection is not None:
        rows = [row for row in rows if row.get("conversation_id") in selection]
    corpus = {r["conversation_id"]: r for r in cos_corpus.read_corpus(vault, run_id)}
    # THE WHOLE TAXONOMY DOCUMENT, not only its rules (INGEST-01). The
    # content lane `choice_for_candidate` reads is gated on the doc's own
    # `mode` — an unapproved taxonomy has no lane fact at all — so handing
    # the rules alone would silently default every candidate to `text`.
    taxonomy_doc = cos.ingest_taxonomy(vault) or {}
    taxonomy = taxonomy_doc.get("rules") or {}
    # DERIVED, NOT DECLARED (review 2026-08-13, round 2, K2). This was the
    # literal `False` below, so nothing in production could ever make it True —
    # and `check_candidate_stamps` short-circuits on it BEFORE inspecting a
    # single proposal id or digest, which means the day a real drop lane exists,
    # a producer that forgets to flip it hides duplicate ids and digest
    # mismatches behind "does not apply". `cos.run_proposal_drops` is the ONE
    # definition of the fact — the host's own pending metas and quarantined
    # claims for this run — and the verifier reads the same one, so the two legs
    # cannot disagree about the same night.
    proposals_dropped = cos.run_proposal_drops(vault, run_id) > 0
    # GAP-04 (s11, 2026-08-16). The five context facts below used to be written
    # by NOTHING but a test fixture, so every rule that reads them graded a
    # night against a permanent `False`. `cos_signals` is their HOST producer:
    # it reads this run's own ledger rows and capture-corpus text and never
    # touches a model answer. The spine is read ONCE per night, not per row.
    signal_now = cos_signals.run_date(run_id)
    signal_commitments = cos_signals.open_commitments(vault)
    # THE DRAFT CENSUS, READ ONCE (review 2026-08-25, finding 5). DOCTRINE and
    # the batch prompt both promise the model that the host re-checks the
    # drafts inventory before archiving an act thread — and `drafts_inventory`
    # below is a literal `[]` that nothing has ever filled, so that guard could
    # not fire in production. These two CAN:
    #   * `cos_mutate_ledger.threads_already_drafted` — this lane's own undo
    #     ledgers, latest row per conversation, so a `sent` or aborted draft is
    #     not a life sentence (its docstring carries the full rule); and
    #   * the driver's per-row `isDraft` stamp, the read pass's own census of
    #     tonight's enumeration, which is what sees an OWNER-written draft.
    # AND IT RAISES, exactly as `load_categories` does above. Swallowing the
    # failure would hand back an EMPTY census — which reads as "no thread
    # carries a draft" and so refuses NOTHING, the one direction a guard must
    # never fail in. `cos_mutate_plan.build_plan` already calls this function
    # unguarded on the same vault, so a read that cannot succeed here is a run
    # that could not have planned its mutations either.
    from cos_mutate_ledger import threads_already_drafted     # noqa: PLC0415
    drafted_convids = threads_already_drafted(vault)
    senders: dict[str, int] = {}
    for c in corpus.values():
        s = (c.get("provenance") or {}).get("sender")
        if s:
            senders[s] = senders.get(s, 0) + 1
    # THE OWNER'S LEVER, READ ONCE PER NIGHT (ruling 2026-09-02). Same overlay
    # file and same reader as the kill switch, so the judge leg and the plan leg
    # cannot disagree about what the owner asked for on the same night.
    from cos_mutate_gates import kill_switch                   # noqa: PLC0415
    switch = kill_switch(vault)
    archive_over_draft = bool(switch.get("archive_over_draft"))
    read_never_categories = bool(switch.get("read_never_categories"))
    night = {"taxonomy": taxonomy, "taxonomy_doc": taxonomy_doc,
             "senders": senders, "categories": categories or {},
             "proposals_dropped": proposals_dropped,
             "archive_over_draft": archive_over_draft,
             "read_never_categories": read_never_categories,
             "drafted_convids": drafted_convids, "now": signal_now,
             "commitments": signal_commitments}
    ctx_by_id = {cid: _row_ctx(row, corpus.get(cid) or {}, night)
                 for row in rows for cid in [row["conversation_id"]]}
    # THE OWNER'S VOICE PROFILE IS A NIGHT FACT, read here because this is the
    # one function that holds the vault (VOICE-01). Absent, `profile_state`
    # returns the NAMED degradation rather than an empty string, and every
    # consumer — the draft prompt, the run facts, the drafts ledger — carries
    # that word instead of quietly drafting ungrounded.
    # THE OWNER'S RULINGS ARE A NIGHT FACT TOO, read here for the same reason
    # the voice profile is: this is the one function that holds the vault
    # (FB-03). It carries the excluded counts and keys with it, so the run
    # report can NAME what the render ceiling left out of the prompt.
    return {"rows": rows, "corpus": corpus, "taxonomy": taxonomy,
            "ctx_by_id": ctx_by_id,
            "read_never_categories": read_never_categories,
            "voice_profile": cos_voice.profile_state(vault),
            "rulings": _owner_rulings(vault)}


def _owner_rulings(vault: Path) -> dict[str, Any] | None:
    """`cos_judge_grounding.owner_rulings`, imported at CALL time.

    `cos_judge_grounding` imports `cos_judge_batches`, which imports this
    module for `_short` — so a module-level import here closes a cycle that
    `cos_judge` cannot even load through. The lazy import is the seam, and it
    keeps ONE producer of the render budget rather than a second copy here.
    """
    import cos_judge_grounding                                   # noqa: PLC0415

    return cos_judge_grounding.owner_rulings(vault)





def _short(value: str) -> str:
    """A conversation id as its 16-hex SHA-256 prefix.

    Evidence written into this repository never carries a mailbox id, not even a
    fragment: set equality and per-row attribution are fully checkable from the
    digest, and this tree is a public-export source (s01/s02 precedent).
    """
    import hashlib                                               # noqa: PLC0415
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _now_iso() -> str:
    import datetime as _dt                                       # noqa: PLC0415
    return _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
