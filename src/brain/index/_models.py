"""Index value records."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

@dataclass
class Hit:
    id: str
    title: str
    classification: str
    zone: str
    path: str
    score: float
    source: str  # "lexical" | "semantic" | "both" | "exact"
    snippet: str = ""
    is_latest_version: str = ""  # TMP-02: "true"|"false"|"" — post-egress field,
                                  # never consulted by the classification gate.
    date: str = ""  # valid-time date (effective_date → document_date → created)
                    # — lets an agent see at a glance HOW CURRENT each hit is.
    type: str = ""  # note type (decision|source|note|…) — authority signal:
                    # a `decision` hit IS the decision layer; a `source` hit is
                    # material under consideration (2026-07-11: an agent
                    # promoted a draft memo's scenario into a "decision"
                    # because the ranked list didn't show which was which).
    evidence: str = "weak_semantic"  # ADR-0008 strongest visible match reason.
    create_safety: str = "unknown"   # conservative create/no-create signal.
    duplicates: list[str] = field(default_factory=list)
    # HYG-01: ids this hit ABSORBED at ranking time — byte-identical, already
    # owner-superseded copies of the same bytes that would otherwise have taken
    # their own result slots. Provenance, never a second slot. Egress-safe by
    # construction: a family is only formed from members carrying the SAME
    # `classification`, so any tier that surfaces the canonical also surfaces
    # every id listed here.

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "classification": self.classification,
            "zone": self.zone,
            "path": self.path,
            "score": round(self.score, 6),
            "source": self.source,
            "snippet": self.snippet,
            "is_latest_version": self.is_latest_version,
            "date": self.date,
            "type": self.type,
            "evidence": self.evidence,
            "create_safety": self.create_safety,
            **({"duplicates": list(self.duplicates)} if self.duplicates else {}),
        }

@dataclass
class _ExactLeg:
    """Pre-egress exact-leg state for one normalized query (ADR-0008)."""

    ranked: list[int]
    tiers: dict[int, str]
    full_rowids: set[int]
    partial_rowids: set[int]
    alias_rowids: set[int]
    title_rowids: set[int]
    owner_rowids: set[int]
    unique_full_rowid: int | None
    collision_order: list[int]
    # Evidence is not capped with the ranked partial list: a title that passes
    # the phrase verifier remains accurately attributable even when the
    # bounded exact leg did not inject it.
    partial_evidence_rowids: set[int] = field(default_factory=set)

@dataclass
class _FamilyCollapse:
    """Which candidate rowids fold into which canonical member (HYG-01)."""

    #: absorbed rowid -> canonical rowid (canonicals are NOT keys)
    canonical_of: dict[int, int] = field(default_factory=dict)
    #: canonical rowid -> the note ids it absorbed, for hit provenance
    absorbed_ids: dict[int, list[str]] = field(default_factory=dict)
    #: families seen but NOT collapsed (a guard declined them) — surfaced in
    #: --explain so a narrow rule's effect is auditable instead of silent.
    declined: int = 0

    @property
    def collapsed(self) -> int:
        return len(self.absorbed_ids)

    def fold(self, order: list[int]) -> list[int]:
        """Rewrite one leg's ranked list so a family occupies ONE position —
        the best rank any member held. Order is otherwise untouched."""
        out: list[int] = []
        seen: set[int] = set()
        for rid in order:
            canon = self.canonical_of.get(rid, rid)
            if canon in seen:
                continue
            seen.add(canon)
            out.append(canon)
        return out

    def fold_dense(
        self,
        order: list[int],
        best_chunk_text: dict[int, str],
        best_chunk_rowid: dict[int, int],
        best_dense_score: dict[int, float],
    ) -> tuple[list[int], dict[int, str], dict[int, int], dict[int, float]]:
        """``fold`` plus the dense leg's per-note representative chunk.

        The canonical inherits the representative of whichever member ranked
        BEST — which is the whole point, since that member may be the only one
        the dense leg surfaced at all. Safe to transplant: family membership is
        byte-identity, so the absorbed member's best chunk is text the canonical
        contains verbatim.
        """
        text, chunk, score = (
            dict(best_chunk_text), dict(best_chunk_rowid), dict(best_dense_score))
        out: list[int] = []
        seen: set[int] = set()
        for rid in order:
            canon = self.canonical_of.get(rid, rid)
            if canon in seen:
                continue
            seen.add(canon)
            out.append(canon)
            if canon == rid:
                continue
            for src, dst in ((best_chunk_text, text), (best_chunk_rowid, chunk),
                             (best_dense_score, score)):
                if rid in src:
                    dst[canon] = src[rid]
        return out, text, chunk, score

@dataclass
class _SearchTrace:
    """Opt-in per-query attribution for ADR-0008 observability.

    The normal retrieval path never constructs this object.  It deliberately
    keeps the native RRF and reranker scales separate: ``pre_rerank_score`` is
    the final comparable RRF/zone/staleness score, while ``rerank_score`` only
    records the cross-encoder's ordering signal.
    """

    rrf_k: int
    exact_leg_enabled: bool
    rerank_requested: bool
    candidate_limit: int
    result_limit: int
    lexical_order: list[int] = field(default_factory=list)
    dense_order: list[int] = field(default_factory=list)
    exact_order: list[int] = field(default_factory=list)
    pre_rerank_order: list[int] = field(default_factory=list)
    final_pre_egress_order: list[int] = field(default_factory=list)
    rerank_applied: bool = False
    # RK-02 adaptive gate: whether the gate was live, whether it SKIPPED the
    # cross-encoder for this query, and the rule that decided. Always
    # populated in trace mode, so a later audit reads the decision instead of
    # inferring it from `rerank_requested` vs `rerank_applied` (which cannot
    # tell a gate skip apart from a missing model or a timeout).
    rerank_gate: dict[str, Any] = field(
        default_factory=lambda: {"enabled": True, "skipped": False, "reason": None}
    )
    # HYG-01: how many duplicate families this query collapsed, and how many it
    # SAW but declined (a guard refused them). Counts only — the absorbed ids
    # ride on the surviving hit, which the egress gate has already ruled on.
    family_collapse: dict[str, Any] = field(
        default_factory=lambda: {"enabled": True, "collapsed": 0, "declined": 0}
    )
    _records: dict[int, dict[str, Any]] = field(default_factory=dict)
    _id_by_rowid: dict[int, str] = field(default_factory=dict)

    def record(self, rowid: int) -> dict[str, Any]:
        """Return this candidate's trace record, allocating only in trace mode."""
        return self._records.setdefault(
            rowid,
            {
                "lexical": None,
                "dense": None,
                "exact": None,
                "raw_rrf_score": 0.0,
                "zone": {"scope": "semantic_only", "factor": 1.0, "applied": False},
                "staleness": {"factor": 1.0},
                "near_duplicate": {"exempt": False, "suppressed": False},
                "pin": {"eligible": False, "applied": False},
                "pre_rerank_score": 0.0,
                "pre_rerank_rank": None,
                "rerank_score": None,
                "rerank_rank": None,
                "_pre_egress_final_rank": None,
            },
        )

    @staticmethod
    def _number(value: Any) -> float:
        """Normalise numpy-like values before JSON-facing trace construction."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _round(cls, value: Any) -> float:
        # Explain is arithmetic, not the compact display score exposed by
        # ``Hit.to_dict``.  Keep enough precision for a caller to reproduce a
        # three-leg sum without a one-micro-point rounding discrepancy.
        return round(cls._number(value), 12)

    def explain_for(
        self, rowid: int, final_rank: int | None, *, redact_identity: bool = False,
    ) -> dict[str, Any] | None:
        """Return a detached, JSON-ready record for one egress-approved hit.

        ``final_rank`` is supplied by the caller *after* its egress decision;
        this prevents a pre-gate rank from being mistaken for an output rank.
        """
        record = self._records.get(rowid)
        if record is None:
            return None

        def leg(value: dict[str, Any] | None) -> dict[str, Any] | None:
            if value is None:
                return None
            return {
                key: (self._round(item) if item is not None and key in {"contribution", "similarity", "weight"}
                      else item)
                for key, item in value.items()
            }

        # The serialized totals are recomputed from the serialized PARTS, not
        # rounded independently from the internal accumulator. `round(a + b)`
        # and `round(a) + round(b)` differ in the last place, so rounding each
        # separately leaves an attribution record whose legs do not add up to
        # its own total. That discrepancy stayed under 1e-12 only while the
        # fusion constant was 60 and every contribution was ~0.016; at
        # RRF_K_FUSE it is visible. The values still track the real arithmetic
        # to within the serialization precision — this fixes which of two
        # equally-rounded answers is printed, nothing else.
        legs = [leg(record["lexical"]), leg(record["dense"]), leg(record["exact"])]
        raw = self._round(sum(v["contribution"] for v in legs if v))
        zone_factor = self._round(record["zone"]["factor"])
        stale_factor = self._round(record["staleness"]["factor"])
        result = {
            "lexical": legs[0],
            "dense": legs[1],
            "exact": legs[2],
            "raw_rrf_score": raw,
            "zone": {
                "scope": record["zone"]["scope"],
                "factor": zone_factor,
                "applied": bool(record["zone"]["applied"]),
            },
            "staleness": {"factor": stale_factor},
            "near_duplicate": {
                "exempt": bool(record["near_duplicate"]["exempt"]),
                "suppressed": bool(record["near_duplicate"]["suppressed"]),
            },
            "pre_rerank_score": self._round(raw * zone_factor * stale_factor),
            "pre_rerank_rank": record["pre_rerank_rank"],
            "pin": {
                "eligible": bool(record["pin"]["eligible"]),
                "applied": bool(record["pin"]["applied"]),
            },
            # Reranker relevance is deliberately emitted as a separate scale.
            # It is never arithmetically combined with the RRF score above.
            "rerank_score": (
                None if record["rerank_score"] is None else self._round(record["rerank_score"])
            ),
            "rerank_rank": record["rerank_rank"],
            "final_rank": final_rank,
        }
        if redact_identity:
            # A full owner withheld by the classification gate changes the
            # exact-leg weight, pin eligibility, and therefore the aggregate
            # RRF arithmetic.  Emitting any of those values for the visible
            # owner would prove a private collision.  Keep the local organic
            # evidence/factors, but remove the identity-derived explanation
            # rather than fabricate a visible-only exact calculation.
            result.update({
                "exact": None,
                "raw_rrf_score": None,
                "pre_rerank_score": None,
                "pre_rerank_rank": None,
                "near_duplicate": {"exempt": None, "suppressed": None},
                "pin": {"eligible": None, "applied": None},
                "rerank_rank": None,
            })
        return result

    def explain_for_id(
        self, note_id: str, final_rank: int | None, *, redact_identity: bool = False,
    ) -> dict[str, Any] | None:
        """Look up a trace record by an already-egress-approved public ID."""
        for rowid, known_id in self._id_by_rowid.items():
            if known_id == note_id:
                return self.explain_for(rowid, final_rank, redact_identity=redact_identity)
        return None

    def compact_digest(self, surfaced_ids: set[str], *, per_leg_limit: int = 20) -> dict[str, Any]:
        """Return the bounded, egress-safe capture projection.

        IDs are filtered to already-surfaced results before any rank is
        materialised, then ranks are renumbered in that gated projection.  This
        avoids rank gaps becoming a side-channel for withheld candidates while
        retaining a useful stage-presence digest for host-only capture in S04.
        """
        per_leg_limit = max(1, int(per_leg_limit))

        def project(order: list[int]) -> tuple[list[dict[str, Any]], bool]:
            visible = [
                rowid for rowid in order
                if self._id_by_rowid.get(rowid) in surfaced_ids
            ]
            return (
                [
                    {"id": self._id_by_rowid[rowid], "rank": rank}
                    for rank, rowid in enumerate(visible[:per_leg_limit], start=1)
                ],
                len(visible) > per_leg_limit,
            )

        lexical, lexical_truncated = project(self.lexical_order)
        dense, dense_truncated = project(self.dense_order)
        exact, exact_truncated = project(self.exact_order)
        pre_rerank, pre_rerank_truncated = project(self.pre_rerank_order)
        final, final_truncated = project(self.final_pre_egress_order)
        return {
            "version": 1,
            "per_leg_limit": per_leg_limit,
            "truncated": any((
                lexical_truncated, dense_truncated, exact_truncated,
                pre_rerank_truncated, final_truncated,
            )),
            "legs": {"lexical": lexical, "dense": dense, "exact": exact},
            "pre_rerank": pre_rerank,
            "final": final,
        }

@dataclass
class _NotePlan:
    """A note's planned index rows (chunking/prefix/dedup done) BEFORE embedding.

    Decouples planning from writing so ``rebuild`` can bulk-embed every note's
    chunk inputs in one batched call (the S11 indexing speed fix) instead of one
    tiny embed per note."""

    note_rowid: int
    row: dict[str, Any]
    chunks: list[Any]
    inputs: list[str]

@dataclass
class _ResumeState:
    """Everything ``_rebuild_impl`` needs to continue a validated staging DB
    (RB-02). Only ever built by ``_try_resume`` after EVERY persisted
    invariant (schema/backend/model/dim/format + vault fingerprint) has been
    confirmed to match -- see docs/adr/0007."""

    committed_batches: int
    committed_notes: int
    start_chunk_rowid: int
    vault_fingerprint: str
    notes_per_batch: int
    finished: bool = False

