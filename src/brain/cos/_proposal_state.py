"""COS proposal-state operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._claims_state import claim_quarantine_dir
from ._guards import _read_receipt_pairs
from ._io import _append_jsonl, _read_jsonl
from ._layout import _utcnow, host_dir, proposal_drop_dir, proposals_dir

def undecided_proposal_ids(vault) -> set[str]:
    """Candidate ids the owner has NOT yet ruled on — staged in the VM's
    proposal drop, or claimed into host ``pending/``. Both states mean "the
    owner's answer is still outstanding".

    A capture draft carrying one of these ids is a GATE BYPASS: the same
    content is simultaneously travelling the gated route (cos-propose ->
    broker -> owner batch -> selective commit) and the UNGATED one
    (draft-capture -> capture-inbox -> signed on the next drain). The ungated
    one always wins the race, so the owner gets asked to approve a note that is
    already authoritative in the vault, and a "reject" has nothing to reject.

    Measured 2026-07-16 (run 14/15): the COS skill forbids SUBSTITUTING
    draft-capture for cos-propose in Phase 1.6, but Phase 5 separately requires
    draft-capture for anything the owner must see. A finding that is also an
    ingestion candidate satisfies both rules and bypasses the gate — no rule
    violated. Policy cannot fix a collision between two obeyed rules; the
    engine must.
    """
    ids: set[str] = set()
    # The claim quarantine counts too (STA-01): a candidate parked there has
    # not been decided either, and it may still be released into a batch once
    # its run is validated — so a capture draft carrying its id is the same
    # race, not a legitimate alternative route.
    for d in (proposal_drop_dir(vault), proposals_dir(vault) / "pending",
              claim_quarantine_dir(vault)):
        if d.is_dir():
            ids.update(p.stem for p in d.glob("*.md"))
    return ids

def quarantine_gate_bypass(vault, draft: Path, *, now: _dt.datetime | None = None) -> Path:
    """Move a bypassing capture draft out of the drain's path, reversibly.

    NOT deleted (recoverable, auditable) and NOT left in place: leaving it
    would re-offer it to every subsequent drain, and — the real hazard — a
    later owner REJECT clears the gated copy out of ``pending/``, after which
    the leftover draft no longer matches an undecided id and the next drain
    would sign the very content the owner just rejected.
    """
    dest_dir = host_dir(vault) / "gate-bypass"
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or _utcnow()).strftime("%Y%m%dT%H%M%S")
    dest = dest_dir / f"{stamp}-{draft.name}"
    shutil.move(str(draft), dest)
    return dest

def run_proposal_drops(vault, run_id: str) -> int:
    """How many proposals THIS run dropped, as the HOST recorded them.

    ONE DEFINITION, TWO CALLERS (review 2026-08-13, round 2, K2). `load_night`
    used to hardcode `proposals_dropped: False` onto every candidate row, so
    nothing in production could ever set it True and the only "known positive"
    for the control that reads it was a hand-built dict. That is
    `vocabulary-needs-a-producer` one layer up: the flag short-circuits
    `check_candidate_stamps` BEFORE it inspects a single proposal id or digest,
    so the day a real drop lane exists, a producer that forgets the flag hides
    duplicate ids and digest mismatches behind "does not apply".

    So applicability is DERIVED, from the two host-written sidecars that carry
    a `run_id` — the pending proposal metas the host wrote when it took
    delivery, and the claims it quarantined for attribution or validity. Both
    are outside the run's control: a run writes its own ledger and its own
    markers, never these. The proposal DROP directory itself is deliberately
    not counted — `propose` records no run there, so a file in it cannot be
    attributed to a run, which is exactly why an unattributable claim
    quarantines and is counted here through that route instead.

    A count, not a boolean, because the caller that verifies wants the number
    and the caller that stamps wants `> 0`.
    """
    return run_proposal_drop_record(vault, run_id)["drops"]

def run_proposal_drop_record(vault, run_id: str) -> dict[str, Any]:
    """``{"drops": N, "malformed": M}`` — the host's record AND its damage.

    K2 FAILS CLOSED ON CORRUPTION (review 2026-08-13, round 5, H6). The count
    above answers "how many drops did the host record for this run", and
    ``check_candidate_stamps`` reads a zero as the host AGREEING that the run
    dropped nothing — the branch that makes the whole control inapplicable. A
    receipt the loaders could not parse produced exactly that zero, so damaging
    one receipt turned a contradiction into a "does not apply".

    ``malformed`` is deliberately NOT scoped to a run. An unreadable receipt has
    no readable ``run_id``, so it cannot be attributed to any run — including
    the one asking. Every run's answer is inconclusive until it is repaired,
    which is the honest reading and the fail-closed one.
    """
    want = str(run_id or "")
    pending, mal_pending = _read_receipt_pairs(proposals_dir(vault) / "pending")
    quarantined, mal_quarantined = _read_receipt_pairs(claim_quarantine_dir(vault))
    return {"drops": sum(1 for m in list(pending) + list(quarantined)
                         if str(m.get("run_id") or "") == want),
            "malformed": mal_pending + mal_quarantined}

# -- the ingestion bridge's HOST-PRIVATE store (s03 attempt 13) ---------------
#
# tools/cos_ingest_bridge.py keeps its delivery receipts here, and E16's
# settlement exemption (`cos_runverify_stamps.bridge_settled`) reads the
# settlement records below. The ENGINE owns the store — path rule, schema and
# both sides of the settlement record — and the bridge writes through this
# API, exactly as it writes drops through `propose` and quarantines through
# `_quarantine_claim`. A second copy of the path rule (the bridge holding its
# own, the verifier holding another) is how the first ends up subtly weaker.

BRIDGE_SETTLEMENT_SCHEMA = "cos_bridge_settlement/v1"
_BRIDGE_RECEIPTS_DIRNAME = "cos-bridge-receipts"
#: The three shapes the bridge settles WITHOUT a drop: a quarantined
#: candidate, a never-ingest-category candidate (zero drops by owner rule),
#: and a duplicate of a conversation another row/run already settled.
BRIDGE_SETTLEMENT_KINDS = ("quarantined", "never-category", "duplicate")

#: The CLOSED vocabulary of reasons a staged candidate got no proposal because
#: THE LEG DID NOT CARRY IT (ATT-02, s04 2026-09-05). Distinct from
#: ``BRIDGE_SETTLEMENT_KINDS`` above, which are the three shapes the bridge
#: settled deliberately: those mean the leg ran and decided, these mean it
#: never decided at all.
#:
#: WHY A CLOSED SET AND NOT PROSE. Measured over 151 ingestion ledgers as of
#: run 2026-09-05-run260 (`_evidence/porter-finishes/bridge-skip-census.txt`):
#: 596 of 1656 `act` + `ingest.relevant` rows carry neither a `proposal_id`
#: nor any statement of what became of them, spread over the 15 runs that
#: wrote no `_cos_ingest_bridge_<run>.jsonl` at all. ELEVEN of those 15 ran
#: with `COS_INGEST_BRIDGE` unset, so the nightly's bridge block was skipped
#: whole — no log line, no file, exit 0. Nothing could COUNT that, because
#: there was nothing to count; free text would have had the same defect one
#: level up. A reason from this tuple is countable by
#: ``cos_runverify_bridge.check_bridge_reach`` without parsing a sentence.
BRIDGE_SKIP_REASONS = ("leg-disabled", "backpressure-abort", "writer-busy",
                       "refused", "receipts-root-unsafe", "no-candidates")

#: The ONE ledger field a skip reason is stamped on — the producer
#: (`tools/cos_bridge_skip.py`) and the check both read this name.
BRIDGE_SKIP_KEY = "bridge_skip"

#: The bridge record written when a pass carried NOTHING. A missing
#: `_cos_ingest_bridge_<run>.jsonl` and one holding this row are DIFFERENT
#: states on disk, and keeping them different is the whole of ATT-02's second
#: rule: absence is what made those 596 rows invisible.
BRIDGE_ZERO_SCHEMA = "cos_ingest_bridge_zero/v1"


def bridge_skip_reason(row: Any) -> str | None:
    """The closed-vocabulary skip reason stamped on one ingestion-ledger row.

    ``None`` when the row carries none — AND when it carries a value outside
    the vocabulary. An off-vocabulary string is not a reason, it is prose, and
    honouring it would re-open the hole the closed set exists to shut; the row
    is then reported as unaccounted, which is what it is. The field lives in
    the run's ingestion ledger under ``run_ops_dir`` (VM-writable), so like
    every other stamp there it is a CLAIM — it says what the host-side leg
    recorded, and the loud state it produces is a FAIL either way, so there is
    nothing here for a forger to gain.
    """
    if not isinstance(row, dict):
        return None
    got = str(row.get(BRIDGE_SKIP_KEY) or "").strip()
    return got if got in BRIDGE_SKIP_REASONS else None


def bridge_conversation_key(conversation_id: Any) -> str:
    """The RUN-INDEPENDENT conversation key every bridge record ends in.

    ONE definition: the bridge's idents (``cosbridge-<run>-<key>``), its
    delivery receipts and its settlement records all end in this key, and E16
    re-derives it from the candidate row's ``conversation_id`` to look a
    claimed settlement up in the host's own record."""
    return sha256_text(str(conversation_id or ""))[:12]


def bridge_drop_ident(run_id: str, conversation_id: Any) -> str:
    """The deterministic name the bridge drops one candidate under.

    ONE DEFINITION, because two readers now need it: the bridge writes here,
    and both the mutation planner (which marks a thread `Brainiac · Ingested`
    when this run dropped it) and E4 (which verifies that mark) ask whether
    the file exists. A second copy of the naming rule is how the two answers
    drift apart."""
    return safe_slug(f"cosbridge-{run_id}-"
                     f"{bridge_conversation_key(conversation_id)}")


def bridge_dropped_row(row: dict[str, Any], run_id: str) -> bool:
    """Did the bridge take delivery of THIS ledger row, in THIS run?

    THE STAMP, NOT THE FILE. The bridge stamps `proposals_dropped: true` and
    `proposal_id: <ident>` onto the row at the moment it drops, and those stay
    on the run's ingestion ledger for good. The DROP FILE does not: the hourly
    claim sweep binds it into an owner batch and the drop directory empties
    behind it — measured 2026-08-23, run169's three drops were gone from
    `drop/proposal-drop/` within the hour while the ledger stamps remained. A
    join on file existence therefore answers "was the sweep slow tonight",
    which is not the question.

    ONE STAMP PER CANDIDATE, WHATEVER THE LANE, so this covers the text lane
    and the file lane alike — unlike the ingest manifest, which carries one
    line per ATTACHMENT and says nothing at all about a text-only night.

    STATED LIMIT: the ingestion ledger is mount-resident, so on the trust
    boundary this is the RUN'S CLAIM rather than host evidence (the same
    reading E16 gives it). It is the right authority for CHOOSING what to mark
    — the planner already trusts this ledger for every verdict, tier and date
    it reads — and the mark it drives is add-only and reversible through the
    undo ledger.
    """
    if row.get("proposals_dropped") is not True:
        return False
    try:
        want = bridge_drop_ident(run_id, row.get("conversation_id"))
    except ValueError:
        return False
    return str(row.get("proposal_id") or "") == want


def bridge_receipts_root(vault) -> Path:
    """The ingestion bridge's host-private dir for THIS vault, proven off-mount.

    Same construction as the approved queue: ``host_private_base() /
    <dirname> / vault_slug8``. The per-vault identity is the hash of the
    RESOLVED VAULT PATH (``config.vault_slug8``) — never the mount-resident
    ``.brain/vault-id`` a VM can rewrite. Raises ``config.HostPathUnsafe``
    when ``$BRAIN_INDEX_DIR`` (or a symlink) resolves back onto the mount —
    callers REFUSE rather than fall back to a VM-reachable location."""
    return config.proven_off_mount(
        config.host_private_base() / _BRIDGE_RECEIPTS_DIRNAME
        / config.vault_slug8(vault),
        vault, what="bridge delivery receipts")


def _bridge_settlements_path(vault, run_id: str) -> Path:
    return bridge_receipts_root(vault) / f"settlements-{safe_slug(str(run_id))}.jsonl"


def record_bridge_settlement(vault, *, run_id: str, conversation_key: str,
                             kind: str, detail: str = "",
                             now: _dt.datetime | None = None) -> None:
    """HOST record that the bridge settled one conversation WITHOUT a drop.

    Written by the bridge at the moment it settles the candidate — BEFORE the
    mount-side ledger stamp, so a crash between the two leaves a record with
    no claim (inert: the exemption also requires the row to claim it) rather
    than a claim with no record (a night failing E16 until the re-run
    re-settles). E16 exempts a stamp-less candidate row from the stamp
    requirement ONLY when this record backs the row's claim: the row's own
    ``bridge_quarantined``/``bridge_refused``/``bridge_duplicate_of`` fields
    live in the run's ingestion ledger under ``cos.run_ops_dir`` — VM-writable
    by the engine's own account — so on their own they are a CLAIM, never
    evidence (the same rule as the delivery receipts beside this file).

    The ``ts`` is the PASS IDENTITY (attempt 16): ``bridge_run`` threads ONE
    ``now`` through everything it records, and ``bridge_settlements`` bounds
    the exemption pool to the records of the latest pass. Microsecond
    resolution on purpose — at whole seconds two quick passes could share an
    identity, and the superseded pass's credits would ride the current one."""
    if kind not in BRIDGE_SETTLEMENT_KINDS:
        raise ValueError(f"unknown bridge settlement kind {kind!r}")
    d = bridge_receipts_root(vault)
    d.mkdir(parents=True, exist_ok=True)
    config.secure_file_permissions(d, 0o700)
    _append_jsonl(_bridge_settlements_path(vault, run_id), {
        "schema": BRIDGE_SETTLEMENT_SCHEMA, "run": str(run_id),
        "conversation_key": str(conversation_key), "kind": kind,
        "detail": scrub(str(detail or ""))[:300],
        "ts": (now or _utcnow()).strftime("%Y-%m-%dT%H:%M:%S.%fZ")},
        vault=vault)


def bridge_settlements(vault, run_id: str) -> dict[str, dict[str, int]]:
    """conversation key -> {settlement kind -> HOST-recorded event count,
    bounded to the LATEST bridge pass that recorded that (key, kind)}.

    FAIL CLOSED, in the restrictive direction: an unsafe receipts root, an
    unreadable file or a malformed/foreign line is NOT a record, and a
    settlement claim with no record behind it stays fully scored — a loud
    false FAIL on damaged host state, never a quiet exemption forged from the
    mount. (Contrast H6 in ``run_proposal_drop_record``, where damage had to
    become INCONCLUSIVE because reading it as a zero was the PERMISSIVE
    branch; here the no-record reading is the strict one.)

    COUNTS, not a set (s03 attempt 14 — the round-6 "reusable bearer token"
    finding): a set let ONE recorded settlement exempt EVERY ledger row that
    named its conversation, so the untrusted leg could hide any number of
    forged stamp-less rows behind one genuine quarantine. The bridge records
    one settlement event per row it settles, so the recorded count is the
    cardinality bound: E16 spends one recorded event per exempted row and
    scores everything past it.

    BOUNDED TO THE LATEST PASS, WHOLE FILE (s03 attempt 16, superseding the
    attempt-15 per-(conversation, kind) bound): the file is append-only and
    a bridge re-run re-records every settlement it makes, so a raw sum grew
    by one full pass per re-run. Attempt 15 bounded each (conversation,
    kind) to ITS latest pass — but a pass that settled a conversation
    DIFFERENTLY (the re-run dropped what the first pass quarantined, or a
    taxonomy change re-routed it) records nothing under the old kind, so the
    superseded pass stayed "latest" for that kind and its stale credit
    exempted a forged stamp-less row. The pool is now the record set of the
    single LATEST pass in the file (every settlement of one pass carries
    that pass's one microsecond timestamp — ``bridge_run`` threads ONE
    ``now`` through everything it records): a superseded pass contributes
    NOTHING, and a genuinely settled row stays exempt because the pass that
    settled it recorded it. An event with no ``ts`` carries no pass identity
    and is not a record — the strict direction again."""
    events: list[tuple[str, str, str]] = []   # (ts, conversation_key, kind)
    try:
        path = _bridge_settlements_path(vault, run_id)
    except config.HostPathUnsafe:
        return {}
    for e in _read_jsonl(path):
        if (isinstance(e, dict)
                and e.get("schema") == BRIDGE_SETTLEMENT_SCHEMA
                and str(e.get("run") or "") == str(run_id)
                and e.get("kind") in BRIDGE_SETTLEMENT_KINDS
                and str(e.get("conversation_key") or "")
                and str(e.get("ts") or "")):
            events.append((str(e["ts"]), str(e["conversation_key"]),
                           str(e["kind"])))
    if not events:
        return {}
    latest = max(ts for ts, _, _ in events)
    out: dict[str, dict[str, int]] = {}
    for ts, key, kind in events:
        if ts == latest:
            kinds = out.setdefault(key, {})
            kinds[kind] = kinds.get(kind, 0) + 1
    return out


__all__ = ['undecided_proposal_ids', 'quarantine_gate_bypass',
           'run_proposal_drops', 'run_proposal_drop_record',
           'BRIDGE_SETTLEMENT_SCHEMA', 'BRIDGE_SETTLEMENT_KINDS',
           'bridge_conversation_key', 'bridge_drop_ident', 'bridge_dropped_row',
           'bridge_receipts_root',
           'record_bridge_settlement', 'bridge_settlements']
