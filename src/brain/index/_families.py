"""Index duplicate suppression methods."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _FamilyMixin:
    """Index duplicate suppression methods."""

    def _collapse_duplicate_families(
        self, lex: list[int], dense: list[int], exact: "_ExactLeg",
    ) -> _FamilyCollapse:
        """Collapse HYG-01 duplicate families before scoring the legs."""
        from ..index_stages.duplicate_families import collapse_duplicate_families

        return collapse_duplicate_families(
            self,
            lex,
            dense,
            exact,
            enabled=_family_collapse_enabled(),
            min_body=_family_min_body(),
            collapse_factory=_FamilyCollapse,
        )

    def _transcript_zones(self) -> "frozenset[str]":
        zones = set(self._DEFAULT_TRANSCRIPT_ZONES)
        raw = os.environ.get("BRAIN_TRANSCRIPT_ZONES")
        if raw:
            zones |= {z.strip() for z in raw.split(",") if z.strip()}
        return frozenset(zones)

    def _dedup_params(self) -> tuple[float | None, str]:
        thr = self._DEFAULT_DEDUP_THRESHOLD
        raw = os.environ.get("BRAIN_DEDUP_THRESHOLD")
        if raw is not None:
            try:
                thr = float(raw)
            except ValueError:
                pass
        scope = os.environ.get("BRAIN_DEDUP_SCOPE", "transcript").strip().lower()
        return thr, scope

    def _suppress_near_dups(
        self,
        ordered: list[int],
        best_chunk_rowid: dict[int, int],
        zmap: dict[int, str],
        col_zone: dict[int, str],
        in_lex: set[int],
        in_full_exact: set[int] | None = None,
        *,
        suppressed: set[int] | None = None,
    ) -> list[int]:
        """Retrieval-time near-duplicate SUPPRESSION (H11/H23 — never an
        index-time deletion; suppressed notes are DEMOTED to the tail, so they
        still surface at a larger k and nothing is removed from the index).

        Root cause #3 of `docs/eval-bench/pt-diagnosis.md`: meeting transcripts
        are near-duplicative (6,752 chunk pairs >=0.80 cosine; 3,988 >=0.97) and
        monopolise the top-k (100% of the failing cross-lingual top-10),
        crowding out the single canonical curated note. This pass walks the
        fused ranking best-first and, when a TRANSCRIPT-zone candidate's
        representative (best-chunk) vector is >= the threshold cosine to an
        already-KEPT candidate's vector, defers it — keeping the cluster's
        highest-ranked representative but freeing the slots its near-clones
        would occupy. It is deliberately CONSERVATIVE (diagnosis §5 F3 risk):
          * only transcript-zone candidates are eligible for suppression
            (scope=transcript, the default) — a curated note is never
            suppressed, so a genuinely-relevant canonical hit cannot be lost;
          * the FIRST (highest-ranked) member of any near-dup cluster always
            survives, so a relevant transcript still surfaces (mono-PT uses
            transcript golds — diagnosis E3b);
          * lexical ("both"/exact) hits and full literal identities are never
            suppressed; partial title phrases retain ordinary behavior;
          * a candidate with no dense best-chunk vector (lexical-only) is never
            suppressed and is not usable as a suppressor reference.
        """
        thr, scope = self._dedup_params()
        if thr is None or not (0.0 < thr < 1.0) or len(ordered) <= 1:
            return ordered
        in_full_exact = in_full_exact or set()
        from ..vectors import cosine

        want = [
            best_chunk_rowid[rid] for rid in ordered if rid in best_chunk_rowid
        ]
        vecs_by_chunk = self.backend.get_vectors(self.conn, want) if want else {}

        def _vec(rid: int) -> list[float] | None:
            cr = best_chunk_rowid.get(rid)
            return vecs_by_chunk.get(cr) if cr is not None else None

        _transcript_zones = self._transcript_zones()

        def _is_transcript(rid: int) -> bool:
            return (zmap.get(rid, "") in _transcript_zones
                    or col_zone.get(rid, "") in _transcript_zones)

        kept: list[int] = []
        kept_vecs: list[list[float]] = []
        deferred: list[int] = []
        for rid in ordered:
            v = _vec(rid)
            eligible = (
                v is not None
                and rid not in in_lex
                and rid not in in_full_exact
                and (scope == "all" or _is_transcript(rid))
            )
            if eligible and any(cosine(v, kv) >= thr for kv in kept_vecs):
                deferred.append(rid)
                if suppressed is not None:
                    suppressed.add(rid)
                continue
            kept.append(rid)
            if v is not None:
                kept_vecs.append(v)
        return kept + deferred

