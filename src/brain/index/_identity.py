"""Index identity retrieval methods."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _IdentityMixin:
    """Index identity retrieval methods."""

    @staticmethod
    def _fusion_k(rrf_k: int) -> int:
        """The RRF denominator this call fuses at (see ``RRF_K_FUSE``).

        An explicit non-production ``rrf_k`` is honoured verbatim — an
        experiment, an eval arm (``eval/capture_run.py --rrf-k``) or a query-log
        replay gets exactly the constant it asked for. Only the production pin
        ``RRF_K_EXACT`` is remapped, and ``$BRAIN_RRF_K`` overrides that;
        setting it back to 60 is the exact rollback.
        """
        if rrf_k != RRF_K_EXACT:
            return rrf_k
        return _fusion_k_from_env(os.environ.get("BRAIN_RRF_K", "").strip())

    @staticmethod
    def _exact_leg_enabled(rrf_k: int) -> bool:
        """Whether the calibrated third RRF leg may participate.

        ADR-0008 pins the exact weights to RRF(k=60).  Experiments at another
        RRF value retain legacy two-leg behavior unless a later ADR calibrates
        a new exact weight set; this makes the kill switch a real rollback.
        """
        raw = os.environ.get("BRAIN_EXACT_LEG_ENABLED", "1").strip().lower()
        return rrf_k == RRF_K_EXACT and raw not in {"0", "false", "no", "off"}

    @staticmethod
    def _valid_time_sort_value(date_text: object) -> int:
        """Sortable valid-time value; malformed/absent dates sort oldest."""
        if not isinstance(date_text, str):
            return 0
        try:
            return _dt.date.fromisoformat(date_text[:10]).toordinal()
        except ValueError:
            return 0

    def _exact_tiebreak(self, record: dict[str, Any]) -> tuple[int, float, int, str]:
        """Stable full-identity collision ordering from ADR-0008."""
        resolved_zone = self._resolve_zone(record["zone"], record["path"])
        zone_weight = self._zone_weight(resolved_zone)
        live_rank = 0 if record.get("is_latest_version") != "false" else 1
        return (
            live_rank,
            -zone_weight,
            -self._valid_time_sort_value(record.get("date")),
            record.get("id", ""),
        )

    @staticmethod
    def _title_phrase_eligible(query: str, title: str) -> bool:
        return _IdentityMixin._title_phrase_tokens_eligible(phrase_tokens(query), title)

    @staticmethod
    def _title_phrase_tokens_eligible(qtokens: list[str], title: str) -> bool:
        """Contiguous title-phrase check using already-tokenized query text."""
        ttokens = phrase_tokens(title)
        if len(qtokens) < 2 or len(qtokens) >= len(ttokens):
            return False
        if len(qtokens) / len(ttokens) < 0.60:
            return False
        width = len(qtokens)
        return any(ttokens[i : i + width] == qtokens for i in range(len(ttokens) - width + 1))

    @staticmethod
    def _literal_keyword_pattern(query: str):
        """Compile the one-query literal verifier, or return ``None``.

        This is deliberately separate from FTS membership: keyword evidence is
        a literal boundary claim, not a token-OR inference.
        """
        query_norm = normalize_identity(query)
        tokens = phrase_tokens(query_norm)
        if not tokens:
            return None
        if len(tokens) == 1 and not identifier_shaped(tokens[0]):
            return None
        return (
            query_norm,
            re.compile(r"(?<!\w)" + re.escape(query_norm) + r"(?!\w)", re.UNICODE),
        )

    @staticmethod
    def _literal_keyword_match_pattern(verifier: Any, title: str, body: str) -> bool:
        """Apply an already-compiled literal verifier to one note."""
        needle, pattern = verifier
        title_norm = normalize_identity(title)
        body_norm = normalize_identity(body)
        return bool(
            (needle in title_norm and pattern.search(title_norm))
            or (needle in body_norm and pattern.search(body_norm))
        )

    def _literal_keyword_match_cached(
        self, verifier: Any, rowid: int, title: str, body: str
    ) -> bool:
        """Apply the literal verifier using a generation-local text cache.

        Matching semantics stay exactly the same: title and body use NFC,
        casefold, and whitespace normalization before the boundary verifier.
        The cache only avoids repeating that pure projection for a note that is
        returned by several searches against an unchanged index.
        """
        text = self._literal_text_cache.get(rowid)
        if text is None:
            text = (normalize_identity(title), normalize_identity(body))
            self._literal_text_cache[rowid] = text
        needle, pattern = verifier
        return bool(
            (needle in text[0] and pattern.search(text[0]))
            or (needle in text[1] and pattern.search(text[1]))
        )

    @classmethod
    def _literal_keyword_match(cls, query: str, title: str, body: str) -> bool:
        """Independently verify literal phrase evidence (never infer from FTS)."""
        pattern = cls._literal_keyword_pattern(query)
        return bool(pattern and cls._literal_keyword_match_pattern(pattern, title, body))

    def _exact_leg(self, query: str, rrf_k: int) -> _ExactLeg:
        """Build the bounded alias/title/phrase third RRF list.

        Full identity owners are indexed projections; partial title phrases are
        deliberately verified against title token sequences rather than FTS's
        token-OR candidate behavior.
        """
        empty = _ExactLeg([], {}, set(), set(), set(), set(), set(), None, [])
        query_norm = normalize_identity(query)
        if not query_norm:
            return empty

        owner_rowids, alias_rowids, title_rowids = identity_owner_rowids(self, query_norm)
        if not self._exact_leg_enabled(rrf_k):
            # The emergency switch turns off *ranking* behavior only. Retain
            # full-identity ownership so additive evidence/create-safety fields
            # can still truthfully describe an organically retrieved result;
            # ranked/full sets stay empty, so there is no injection, zone
            # exemption, dedup exemption, collision normalization, or pin.
            return _ExactLeg(
                [], {}, set(), set(), alias_rowids, title_rowids, owner_rowids,
                None, [],
            )
        owner_records = identity_records(self, owner_rowids)
        full_sorted = sorted(owner_records, key=lambda rid: self._exact_tiebreak(owner_records[rid]))
        full_ranked = full_sorted[:EXACT_FULL_CAP]

        tiers: dict[int, str] = {}
        for rid in full_ranked:
            # Alias evidence wins over a same-note title match by contract.
            tiers[rid] = "full_alias" if rid in alias_rowids else "full_title"

        partial_records = self._title_phrase_match_cache.get(query_norm)
        if partial_records is None:
            partial_records = {}
            qtokens = phrase_tokens(query_norm)
            if len(qtokens) >= 2:
                for record in title_phrase_candidates(self, qtokens):
                    rid = int(record["rowid"])
                    if (rid in owner_rowids
                            or not self._title_phrase_tokens_eligible(qtokens, record["title"])):
                        continue
                    partial_records[rid] = record
            self._title_phrase_match_cache[query_norm] = partial_records
        partial_ranked = sorted(
            partial_records, key=lambda rid: self._exact_tiebreak(partial_records[rid])
        )[:EXACT_PARTIAL_CAP]
        for rid in partial_ranked:
            tiers[rid] = "partial_title"

        return _ExactLeg(
            ranked=full_ranked + partial_ranked,
            tiers=tiers,
            full_rowids=set(full_ranked),
            partial_rowids=set(partial_ranked),
            alias_rowids=alias_rowids,
            title_rowids=title_rowids,
            owner_rowids=owner_rowids,
            unique_full_rowid=full_ranked[0] if len(owner_rowids) == 1 and full_ranked else None,
            # The injection cap bounds exact-RRF work only.  Slot
            # normalization must retain every surfaced collision owner in this
            # stable order, including a retired owner beyond that cap.
            collision_order=full_sorted if len(owner_rowids) > 1 else [],
            partial_evidence_rowids=set(partial_records),
        )

    @staticmethod
    def _exact_weight(tier: str, owner_count: int) -> float:
        if tier == "partial_title":
            return EXACT_WEIGHT_PARTIAL_TITLE
        return EXACT_WEIGHT_UNIQUE_FULL if owner_count == 1 else EXACT_WEIGHT_COLLIDING_FULL

    def _evidence_from_exact(self, exact: _ExactLeg, rid: int) -> str | None:
        """Return literal identity evidence independently of exact-leg ranking.

        Exact-leg caps and the runtime kill switch constrain candidate injection
        and rank changes. They must not make a surfaced organic result falsely
        look like a weak semantic match when its alias, full title, or eligible
        title phrase really did match the query.
        """
        if rid in exact.alias_rowids:
            return "alias_hit"
        if rid in exact.title_rowids:
            return "exact_title_match"
        if rid in exact.partial_evidence_rowids:
            return "title_phrase_match"
        return None

    @staticmethod
    def _create_safety_from_evidence(evidence: str, owner_count: int) -> str:
        if evidence in {"alias_hit", "exact_title_match"}:
            return "exists" if owner_count == 1 else "probable"
        if evidence in {"title_phrase_match", "keyword_exact", "high_vector_match"}:
            return "probable"
        return "unknown"

    def identity_egress_redacted_ids(self, query: str, max_tier: str) -> set[str]:
        """Return visible full-identity owners whose detail must be redacted.

        Exact identity ownership is intentionally calculated before egress.  If
        one full owner is withheld, exposing the other owner's exact rank,
        contribution, pin state, or combined score would disclose the hidden
        collision.  This helper keeps that fact internal and returns only the
        already-allowable owner IDs for post-gate serialisation decisions.
        """
        query_norm = normalize_identity(query)
        if not query_norm:
            return set()
        owner_rowids, _alias_rows, _title_rows = identity_owner_rowids(self, query_norm)
        if not owner_rowids:
            return set()
        records = identity_records(self, owner_rowids)
        allowed = cls_mod.ClassificationFilter(max_tier=max_tier)
        hidden_owner = any(
            not allowed.allows(record.get("classification", ""))
            for record in records.values()
        )
        if not hidden_owner:
            return set()
        return {
            str(record["id"])
            for record in records.values()
            if allowed.allows(record.get("classification", ""))
        }

    def annotate_create_safety(
        self, query: str, surfaced: list[dict[str, Any]], max_tier: str
    ) -> set[str]:
        """Apply the post-egress half of ADR-0008 create safety in place.

        This receives only already-gated hit dictionaries. It consults the
        complete pre-egress identity owner set internally, then emits only the
        conservative enum — never an owner count, visibility flag, or hidden id.
        """
        if not surfaced:
            return set()
        owner_rowids, _alias_rows, _title_rows = identity_owner_rowids(
            self, normalize_identity(query)
        )
        if not owner_rowids:
            return set()
        records = identity_records(self, owner_rowids)
        allowed = cls_mod.ClassificationFilter(max_tier=max_tier)
        owner_by_id = {record["id"]: record for record in records.values()}
        hidden_owner = any(
            not allowed.allows(record.get("classification", ""))
            for record in records.values()
        )
        owner_count = len(records)
        redacted_ids = (
            {
                str(record["id"])
                for record in records.values()
                if allowed.allows(record.get("classification", ""))
            }
            if hidden_owner else set()
        )
        for hit in surfaced:
            # Any withheld full owner makes the complete create/no-create
            # conclusion unknowable; do not leak why or how many are hidden.
            # A visible owner's score includes the collision-sensitive exact
            # contribution, so it must be withheld as well.  Ranking has
            # already completed; this only changes the egress projection.
            if hidden_owner:
                hit["create_safety"] = "unknown"
                if hit.get("id") in redacted_ids:
                    hit["score"] = None
                continue
            evidence = hit.get("evidence", "weak_semantic")
            if evidence in {"alias_hit", "exact_title_match"} and hit.get("id") in owner_by_id:
                hit["create_safety"] = "exists" if owner_count == 1 else "probable"
        return redacted_ids

    def _normalize_collision_slots(self, hits: list[Hit], collision_order: list[int]) -> list[Hit]:
        """Keep collision owners live-before-retired without globally pinning them.

        The reranker may move a collision group against unrelated candidates.
        This method preserves exactly the slots it selected for that group and
        refills only those slots in the deterministic identity order.
        """
        if len(collision_order) < 2 or not hits:
            return hits
        records = identity_records(self, set(collision_order))
        rank_by_id = {
            records[rid]["id"]: rank
            for rank, rid in enumerate(collision_order)
            if rid in records
        }
        slots = [idx for idx, hit in enumerate(hits) if hit.id in rank_by_id]
        if len(slots) < 2:
            return hits
        owners = sorted((hits[idx] for idx in slots), key=lambda hit: rank_by_id[hit.id])
        out = list(hits)
        for slot, owner in zip(slots, owners):
            out[slot] = owner
        return out

