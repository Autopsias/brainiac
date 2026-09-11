"""COS learning-ledger operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._attachment_join import (
    _note_conversation, _sweep_claim_dests, attachment_lane_context,
)
from ._attachment_gate import (
    attachment_lane_pending, attachment_lane_withheld,
    signed_attachment_conversations,
)
from ._io import _append_jsonl, _read_jsonl
from ._layout import _ts, proposals_dir

def _claims_path(vault) -> Path:
    return proposals_dir(vault) / "claims.jsonl"

def _record_verdict(vault, bound: dict[str, Any], *, outcome: str,
                    answer_mode: str, batch_size: int, ts: str) -> None:
    record_outcome(
        vault, pattern=bound.get("pattern"), ident=bound["id"], outcome=outcome,
        bundle_version=bound.get("bundle_version"), ts=ts,
        category=bound.get("category"), lane=bound.get("lane"),
        tier=bound.get("tier"), rules_version=bound.get("rules_version"),
        kind=bound.get("kind"), answer_mode=answer_mode, batch_size=batch_size,
        evidence_unit=bound.get("evidence_unit"),
        evidence_lineage=bound.get("evidence_lineage"))



def signed_ingest_notes(vault) -> dict[str, str]:
    """CONTENT HASH -> the conversation the signed note's own provenance names.

    The hash is ``sha256`` OF THE NOTE FILE'S OWN BYTES, and that is not an
    arbitrary choice: it is the same number the bridge's claims ledger already
    records for the candidate it dropped, because the drop payload and the
    signed note are the same bytes. MEASURED 2026-08-25 on the reference host:
    the claims row for ``cosbridge-2026-08-23-run179-8e4367d60843`` carries
    ``3eca9a6b…`` and ``sha256(brain/resources/cosbridge-2026-08-23-run179-
    8e4367d60843.md)`` IS ``3eca9a6b…``; 299 of the 651 bridge claims rows
    match a vault note this way.

    NOT ``cos.source_sha256``, which the first attempt at this index used and
    which is a DIFFERENT number — the sha of the captured message TEXT, not of
    the note (``cd492340…`` for the same note above). Keyed on that field the
    join matched nothing at all, so the chip it authorises would never have
    been written again: a silent fail-closed the drop-stamp rule it replaced
    could not have produced.

    ONE PASS OVER THE VAULT, ~1.2s for 3,993 notes / 134 MB on the reference
    host, so callers checking many rows must build it once and pass it down
    (`signed_ingested_conversations` and `cos_echecks_runs.chip_join` both do)
    — per row it would be four minutes.
    """
    out: dict[str, str] = {}
    root = config.vault_root(vault)
    for sub in ("brain", "raw"):
        for note in (root / sub).rglob("*.md"):
            try:
                body = note.read_bytes()
            except OSError:
                continue
            out.setdefault(hashlib.sha256(body).hexdigest(),
                           _note_conversation(
                               body[:3000].decode("utf-8", "replace")))
    return out


def claims_by_ident(vault) -> dict[str, set[str]]:
    """Bridge ident -> every content sha the claims ledger recorded for it.

    Read ONCE per pass for the same reason as the note index above: the ledger
    is 160 KB and 780 rows on the reference host, and the old shape re-read it
    per candidate row.
    """
    out: dict[str, set[str]] = {}
    for entry in _read_jsonl(_claims_path(vault)):
        ident = str(entry.get("id") or "")
        sha = str(entry.get("sha256") or "")
        if ident and sha:
            out.setdefault(ident, set()).add(sha)
    return out


def ingest_signed_row(vault, row: dict[str, Any], run_id: str, *,
                      notes: dict[str, str] | None = None,
                      claims: dict[str, set[str]] | None = None,
                      attach: set[str] | None = None,
                      lane: dict[str, Any] | None = None) -> bool:
    """Did the vault SIGN a note for THIS thread's own candidate? (FIX-03)

    TWO clauses, and BOTH must hold — this is the authority behind the
    ``Brainiac · Ingested`` chip, and until 2026-08-25 the chip keyed on the
    bridge's DROP stamp instead, so it said "ingested" about candidates that
    were merely OFFERED and might sit quarantined for weeks.

    1. EXISTENCE, BY CONTENT HASH, never by per-run ident: the claims rows for
       this run's bridge ident carry a sha, and a vault note whose own bytes
       hash to exactly that sha exists. An ident join once fabricated 55
       losses of which exactly 1 was real (measured 2026-08-24), and a note
       that a fold RENAMES keeps its bytes while losing its id.
    2. PROVENANCE: the matched note's own ``provenance.conversation_id`` is
       THIS conversation. One attachment forwarded in two threads is ONE hash
       and ONE signed note; a hash match alone would chip the second thread
       for content of which nothing of its own was signed — the exact lie this
       clause exists to remove. It refused none of the 299 live matches on the
       reference host, which is what a belt should look like.

    TWO LANES, because the vault has two write paths and the chip is one
    claim. The clauses above are the TEXT lane. A thread whose ATTACHMENT the
    vault took never gets a cosbridge claims row for those bytes — the bridge
    writes its manifest line before the file is fetched, so the claims sha is
    the text payload's — and the text lane alone therefore reads a signed
    thread as unsigned. Measured 2026-08-25: of 71 offered conversations on
    this host, 5 have no text-lane match, and 3 of those 5 hold their PDFs in
    ``raw/`` under this thread's own ``provenance.conversation_id``.

    The FILE lane is :func:`signed_attachment_conversations`, and it carries
    the same two clauses through the attachment chain: this run's own
    manifest line, the sweep's claim of that line, the released payload's
    content sha, the note the drain minted for exactly those bytes. Both lanes
    require a signed note; neither will chip on an offer.

    A THIRD CLAUSE, AND IT IS A REFUSAL (ATT-03, 2026-09-05). The two lanes
    above are alternatives — either one alone used to answer yes — so a thread
    carrying a real attachment could be read as taken on the strength of its
    TEXT while its file was still outside the vault. That is exactly what
    happened: measured over every ingestion ledger on the reference host as of
    run ``2026-09-05-run260``, 51 chipped threads carry a non-inline
    attachment and 18 of them never reached the sweep's claim at all.
    :func:`attachment_lane_pending` now REFUSES such a row outright, before
    either clause is consulted, unless the vault recorded what became of every
    one of that thread's files — joined, declined, or withdrawn. It is the
    reason a thread with an attachment cannot be marked complete on the text
    lane alone, and it binds both readers of this function: the
    ``Brainiac · Ingested`` mark and the aged-read lane's RULE-1 archive
    escape. The chip itself is UNCHANGED in what it claims; the separate
    bytes-signed claim is ``cos.attachment_joins``.
    """
    from ._proposal_state import bridge_drop_ident          # noqa: PLC0415

    cid = str(row.get("conversation_id") or "")
    if not cid:
        return False
    try:
        want_ident = bridge_drop_ident(run_id, cid)
    except ValueError:
        return False
    # THE FILE GATE COMES FIRST, and it comes first because the text clause
    # below would otherwise answer for a thread whose files nobody kept. The
    # REASON is handed back through `why` when a caller offers somewhere to put
    # it — a bare `False` told a reader nothing about which lane withheld the
    # thread (review 2026-09-05); `attachment_lane_withheld` is the same
    # sentence for a whole run at once.
    pending = attachment_lane_pending(vault, run_id, row, ctx=lane)
    if pending is not None:
        # THE SENTENCE IS NOT LOST, IT IS ASKED FOR ELSEWHERE. This used to
        # take a `why` dict and fill it in; nothing ever passed one (adversarial
        # review, 2026-09-05 — `grep "why=" src tools tests` found zero
        # callers), because `attachment_lane_withheld` re-asks
        # `attachment_lane_pending` over the same one-run context and builds the
        # map itself. A parameter no caller uses is a surface that looks wired
        # and is not, so it is gone rather than left to read as plumbing.
        return False
    index = signed_ingest_notes(vault) if notes is None else notes
    ledger = claims_by_ident(vault) if claims is None else claims
    # No claims row at all ⇒ this run's own candidate was never even claimed.
    if any(index.get(sha) == cid for sha in ledger.get(want_ident, ())):
        return True
    signed_files = (signed_attachment_conversations(vault, run_id)
                    if attach is None else attach)
    return cid in signed_files


#: The per-run ingestion ledger the judgment leg writes, as a glob and a
#: prefix. One definition, because the catch-up below has to turn a file name
#: back into the run id that names it.
INGEST_LEDGER_GLOB = "_cos_ingestion_ledger_*.jsonl"
INGEST_LEDGER_PREFIX = "_cos_ingestion_ledger_"
#: How far back the ingestion-mark catch-up looks, in days. The same knob the
#: mutation plan's recency window uses, read the same way, so the planner that
#: CHOOSES a mark and the E-check that JUDGES one cannot disagree about which
#: nights are still in reach.
# CATCH_UP_DAYS_ENV / DEFAULT_CATCH_UP_DAYS now live in `_constants` (one
# definition for both ingestion lanes); `_shared` re-exports them here.


def catching_up_ingest_runs(vault, run_id: str, *,
                            since_days: int | None = None
                            ) -> dict[str, list[dict[str, Any]]]:
    """The runs whose offered candidates the vault may have signed BY NOW.

    THE NIGHT THAT OFFERS A CANDIDATE CAN NEVER BE THE NIGHT THAT SIGNS IT
    (review 2026-08-25). ``tools/cos_nightly.sh`` runs the ingest bridge —
    which DROPS candidates as proposals — and builds its mutation plan a few
    legs later in the same run. A drop is answered by the owner and signed by
    a later maintenance drain, hours or days afterwards. So anything keyed on
    tonight's own run id alone answers zero on every night, forever, which is
    what the first cut of FIX-03 shipped:
    ``tests/test_cos_night_phase_order.py`` pins that nothing between those
    two legs signs anything.

    Returns run id -> THAT RUN'S OWN ledger rows, because the per-run bridge
    ident is what :func:`ingest_signed_row` keys on: a row may only be judged
    under the run that offered it.

    STATED CEILING: the window bounds the catch-up at ``since_days`` (14 by
    default, ``$BRAIN_COS_SINCE_DAYS``), applied to the LEDGER FILE NAME — so
    this reads a fortnight of ledgers rather than every run the host has ever
    made. IT BOUNDS THE WORK, NOT THE ANSWER (DD-01, 2026-09-10): a thread
    whose offering run has left the window still reads as signed, because
    :func:`signed_ingested_catching_up` falls through to the cross-run note
    index. This docstring claimed the opposite until the bridge stopped
    re-offering an unchanged thread every night, which is what had been
    keeping every signed thread's evidence inside the window. An `--all`
    run lifts the plan's recency window but NOT this one; widening it is a
    knob, not a redesign.
    """
    if since_days is None:
        try:
            since_days = int(os.environ.get(CATCH_UP_DAYS_ENV)
                             or DEFAULT_CATCH_UP_DAYS)
        except ValueError:
            since_days = DEFAULT_CATCH_UP_DAYS
    cutoff = ""
    if since_days > 0:
        try:
            day = _dt.date.fromisoformat(str(run_id)[:10])
        except ValueError:
            day = None
        if day is not None:
            cutoff = (day - _dt.timedelta(days=int(since_days))).isoformat()
    out: dict[str, list[dict[str, Any]]] = {}
    from ._runs import run_ops_dir                            # noqa: PLC0415

    for path in sorted(run_ops_dir(vault).glob(INGEST_LEDGER_GLOB)):
        rid = path.name[len(INGEST_LEDGER_PREFIX):-len(".jsonl")]
        if rid != run_id and rid[:10] < cutoff:
            continue
        rows = _read_jsonl(path)
        if rows:
            out[rid] = rows
    return out


def signed_ingested_catching_up(vault, run_id: str,
                                rows: list[dict[str, Any]], *,
                                since_days: int | None = None) -> set[str]:
    """Conversations the vault has SIGNED a candidate for, tonight's included.

    THE ONE DEFINITION BEHIND THE ``Brainiac · Ingested`` CHIP, and it has two
    callers that must never drift: the planner that chooses the mark
    (``cos_mutate_plan_marks.ingested_conversations``) and E4, which judges
    whether a dispatched mark was justified. Keyed on tonight's run alone the
    planner plans nothing; keyed on tonight's run alone E4 calls every
    caught-up mark unsigned. Both ask this.

    ``rows`` bounds the answer to threads the caller is actually considering —
    the planner passes tonight's ledger, so a thread that has left the mailbox
    is never marked. That bound is applied BEFORE the join, not only after it,
    and on the live host that is the difference between a plan step that costs
    seconds and one that costs half a minute: MEASURED 2026-08-25 against the
    reference vault, run188's 218 rows and a 14-day window reach 63 runs, and
    asking each of them about its whole ledger walked 63 attachment lanes at
    ~0.57s each. Scoped to the conversations tonight enumerated, only the runs
    that actually offered one of them are asked at all. The answer is
    identical by construction — it was intersected with ``wanted`` anyway.
    """
    wanted = {str(r.get("conversation_id") or "") for r in rows
              if r.get("conversation_id")}
    by_run: dict[str, list[dict[str, Any]]] = {}
    for rid, ledger in catching_up_ingest_runs(
            vault, run_id, since_days=since_days).items():
        mine = [r for r in ledger
                if str(r.get("conversation_id") or "") in wanted]
        if mine:
            by_run[rid] = mine
    by_run[run_id] = [r for r in rows if r.get("conversation_id")]
    signed = signed_ingested_conversations_by_run(vault, by_run) & wanted
    # THE NIGHTLY RE-DROP WAS DOING A SECOND JOB, AND DD-01 TOOK IT AWAY
    # (review 2026-09-10). Every clause above is keyed on a PER-RUN bridge
    # ident, so a thread only answers yes while some run INSIDE the window
    # still offered it. The bridge used to re-offer an unchanged thread every
    # night, which re-stated its signed evidence inside the window forever;
    # now it settles the thread `already-ingested` and offers nothing, so a
    # fortnight later the window is empty. Probed: sign `2026-08-18-run1`,
    # settle on `2026-08-19-run2` -> {thread-1}, ask again on
    # `2026-09-10-run9` -> set(). That empty set feeds the
    # `Brainiac · Ingested` chip AND the aged-read RULE-1 archive escape, so
    # the thread could never be archived — the inbox-to-zero goal DD-01
    # exists for, closed by DD-01 itself.
    #
    # `signed_bridge_notes` is the cross-run half of the SAME join, and it is
    # the same STRENGTH: sha256 of the note file's OWN BYTES against a sha the
    # host-private claims ledger recorded, and only then the note's own
    # `provenance.conversation_id`. Frontmatter alone is still a claim
    # (STA-01) — a forged note carries no claims row and answers no at any
    # age. ONE FULL VAULT WALK (~1.2s / 3,993 notes), so it is built once here
    # and only when something is still unaccounted for.
    missing = wanted - signed
    if missing:
        from ._bridge_notes import signed_bridge_notes        # noqa: PLC0415
        from ._proposal_state import bridge_conversation_key  # noqa: PLC0415
        index = signed_bridge_notes(vault)
        signed |= {cid for cid in missing
                   if index.get(bridge_conversation_key(cid))}
    # A UNION IS ONLY SOUND FOR A FACT THAT CANNOT BECOME FALSE (ATT-03,
    # 2026-09-05). "This thread's files are accounted for" is not such a fact:
    # a new attachment on an existing thread makes an older run's YES wrong,
    # and a union has no way to retract one. Measured on 2026-09-05-run260, 16
    # of 120 threads sat in this set while THAT run's own row said their files
    # never reached a note — and this set feeds the chip AND the aged-read
    # RULE-1 archive escape, so those threads were archivable with their bytes
    # outside the vault. Tonight's row is the only witness of tonight's file
    # set, so tonight's row holds the veto.
    return signed - set(attachment_lane_withheld(vault, run_id, rows))


def signed_ingested_conversations_by_run(
        vault, rows_by_run: dict[str, list[dict[str, Any]]]) -> set[str]:
    """The conversation ids SIGNED across several runs, in ONE vault scan.

    SIGNING NEVER HAPPENS INSIDE THE NIGHT THAT OFFERED THE CANDIDATE (review
    2026-08-25). The COS night drops proposals and then plans its mutations
    minutes later; the drop is answered, drained and signed by a LATER
    maintenance pass, so a mark keyed on tonight's own run id can only ever
    plan zero. The planner therefore asks about the runs whose candidates the
    vault has had time to take, and this is the shape that makes that cheap:
    ``signed_ingest_notes`` is a full-vault walk (~1.2s / 3,993 notes on the
    reference host) and would otherwise be paid once per run asked about.

    ``rows_by_run`` maps a run id to THAT RUN'S OWN ledger rows — the per-run
    bridge ident is what both clauses of :func:`ingest_signed_row` key on, so
    a row may only be judged under the run that offered it.
    """
    notes = signed_ingest_notes(vault)
    claims = claims_by_ident(vault)
    out: set[str] = set()
    for run_id, rows in rows_by_run.items():
        # ONE walk of this run's manifest lines, reused by both halves: the
        # file lane's joined set and the pending check read the same pass.
        lane = attachment_lane_context(vault, run_id)
        attach = {j["conversation_id"] for j in lane["joins"]}
        out |= {str(r.get("conversation_id") or "") for r in rows
                if r.get("conversation_id")
                and ingest_signed_row(vault, r, run_id, notes=notes,
                                      claims=claims, attach=attach, lane=lane)}
    return out


def signed_ingested_conversations(vault, run_id: str,
                                  rows: list[dict[str, Any]]) -> set[str]:
    """The conversation ids whose OWN candidate this run reached signed state.

    The planner-facing batch form of :func:`ingest_signed_row`: one vault scan,
    one claims read, every row checked. This REPLACES the drop-stamp join as
    the ``Brainiac · Ingested`` chip's authority, and the planner and E4 both
    call the engine's one definition — the same single-rule discipline
    ``bridge_dropped_row`` already followed.
    """
    return signed_ingested_conversations_by_run(vault, {run_id: rows})


def _outcomes_path(vault=None) -> Path:
    return proposals_dir(vault) / "outcomes.jsonl"

def record_outcome(vault, *, pattern: str, ident: str, outcome: str,
                   bundle_version: str, ts: str | None = None,
                   category: str | None = None, lane: str | None = None,
                   tier: str | None = None, rules_version: str | None = None,
                   kind: str | None = None, answer_mode: str | None = None,
                   batch_size: int | None = None,
                   evidence_unit: str | None = None,
                   evidence_lineage: str | None = None) -> dict[str, Any]:
    """Append ONE owner-decision or claim-time-defect record. Never mutated,
    never deleted (the acceptance evidence this gate reads is itself
    audit-shaped, even though it lives outside the signed note chain).

    IDEMPOTENT PER (proposal id, outcome) — the expected double-count failure
    mode is one candidate appearing in TWO batches after a TTL requeue; a
    second ``accepted`` for the same id is a re-record of one owner decision,
    not a second one, so it is dropped rather than appended.

    LRN-01 adds the CATEGORY dimension: ``category``/``lane``/``tier``/
    ``rules_version`` are the graduation evidence key (HOST-bound at claim
    time, never read back off the VM-authored candidate), and ``kind``/
    ``answer_mode``/``batch_size`` are what the bulk-accept guard and the
    reports need. ``evidence_unit`` (HARDENED:codex-4) is the stable identity
    of the underlying material: the FIRST record for a unit counts, every
    later one is stored with ``counted: false`` so repeated forwards, thread
    re-extractions and whitespace variants cannot inflate the Wilson sample.
    ``evidence_lineage`` (B5) dedups the OTHER way round — one HOST-VERIFIED
    conversation is one unit even when its messages differ. Either match is
    enough to stop a second verdict counting; neither can be produced by a
    VM claim, so nothing a producer writes can raise the sample.
    """
    existing = _read_jsonl(_outcomes_path(vault))
    for e in existing:
        if e.get("id") == ident and e.get("outcome") == outcome:
            return e
    counted = True
    if evidence_unit or evidence_lineage:
        for e in existing:
            if (e.get("counted") is False
                    or e.get("outcome") not in ("accepted", "rejected")
                    or outcome not in ("accepted", "rejected")):
                continue
            if ((evidence_unit and e.get("evidence_unit") == evidence_unit)
                    or (evidence_lineage
                        and e.get("evidence_lineage") == evidence_lineage)):
                counted = False
                break
    rec: dict[str, Any] = {
        "pattern": pattern or CATEGORY_UNCLASSIFIED, "id": ident, "outcome": outcome,
        "bundle_version": bundle_version or "unknown", "ts": ts or _ts(),
        "category": category or CATEGORY_UNCLASSIFIED,
        "lane": lane or LANE_TEXT,
        "tier": tier or "unknown",
        "rules_version": rules_version or "unknown",
        "counted": counted,
    }
    for k, v in (("kind", kind), ("answer_mode", answer_mode),
                 ("batch_size", batch_size), ("evidence_unit", evidence_unit),
                 ("evidence_lineage", evidence_lineage)):
        if v is not None:
            rec[k] = v
    _append_jsonl(_outcomes_path(vault), rec, vault=vault)
    return rec

def _defects_path(vault=None) -> Path:
    return proposals_dir(vault) / "defects.jsonl"

def log_defect(vault, kind: str, detail: str, ts: str | None = None) -> dict[str, Any]:
    """Append ONE ingestion-taxonomy defect. Doctrine alone is not a gate: an
    unparseable taxonomy or a refused `never` candidate has to leave a
    machine-readable trace, not just a comment in a spec."""
    rec = {"kind": kind, "detail": scrub(str(detail)), "ts": ts or _ts()}
    _append_jsonl(_defects_path(vault), rec, vault=vault)
    return rec

def defects(vault) -> list[dict[str, Any]]:
    return _read_jsonl(_defects_path(vault))

def _demotions_path(vault=None) -> Path:
    return proposals_dir(vault) / "demotions.jsonl"

def demote_category(vault, category: str | None, *, reason: str,
                    ts: str | None = None) -> dict[str, Any]:
    """Un-graduate ``category`` NOW and reset its evidence.

    Append-only: the reset is a DEMOTION MARKER, not a deletion of history —
    ``category_stats`` ignores every outcome at or before the newest marker.
    Fired by a claim-time security defect and by EVERY undo path."""
    rec = {"category": str(category or CATEGORY_UNCLASSIFIED),
           "reason": reason, "ts": ts or _ts()}
    _append_jsonl(_demotions_path(vault), rec, vault=vault)
    return rec

def _last_demotion(vault, category: str) -> str | None:
    stamps = [str(e.get("ts", "")) for e in _read_jsonl(_demotions_path(vault))
              if e.get("category") == category and e.get("ts")]
    return max(stamps) if stamps else None

__all__ = ['_claims_path', '_record_verdict', '_note_conversation', 'signed_ingest_notes', '_sweep_claim_dests', 'signed_attachment_conversations', 'attachment_lane_context', 'attachment_lane_pending', 'attachment_lane_withheld', 'CATCH_UP_DAYS_ENV', 'DEFAULT_CATCH_UP_DAYS', 'claims_by_ident', 'ingest_signed_row', 'signed_ingested_conversations', 'signed_ingested_conversations_by_run', 'catching_up_ingest_runs', 'signed_ingested_catching_up', '_outcomes_path', 'record_outcome', '_defects_path', 'log_defect', 'defects', '_demotions_path', 'demote_category', '_last_demotion']
