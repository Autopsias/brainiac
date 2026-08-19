"""Stable brain.index import facade."""
from __future__ import annotations

from ._settings import (
    EXACT_FULL_CAP, EXACT_PARTIAL_CAP, EXACT_WEIGHT_COLLIDING_FULL,
    EXACT_WEIGHT_PARTIAL_TITLE, EXACT_WEIGHT_UNIQUE_FULL, FAMILY_MIN_BODY,
    GREP_REGEX_TIMEOUT_S, INDEX_FORMAT_VERSION, MAX_GREP_PATTERN_LEN,
    RRF_K_EXACT, RRF_K_FUSE, SCHEMA_VERSION, GrepPatternError,
    _GREP_HAS_TIMEOUT, _TEMPORAL_INTENT_RE, _boilerplate_patterns,
    _env_float, _family_collapse_enabled, _family_min_body, _fusion_k_from_env,
    _grep_bounded_search_impl, _grep_engine, _matches_boilerplate_pattern,
    _recency_factor, _today, rerank_gate_enabled,
)
from ._models import Hit, _ExactLeg, _FamilyCollapse, _NotePlan, _ResumeState, _SearchTrace
from ..dbretry import with_write_retry
from ..embed import get_embedder
from ._connection import _ConnectionMixin
from ._schema import _SchemaMixin
from ._planning import _PlanningMixin
from ._lifecycle import _LifecycleMixin
from ._records import _RecordMixin
from ._zones import _ZoneMixin
from ._identity import _IdentityMixin
from ._families import _FamilyMixin
from ._search import _SearchMixin
from ._graph import _GraphMixin
from ._tools import _ToolMixin


def _grep_bounded_search(compiled, text: str):
    """Preserve the public grep timeout seam."""
    return _grep_bounded_search_impl(compiled, text, has_timeout=_GREP_HAS_TIMEOUT)


class BrainIndex(
    _ConnectionMixin, _SchemaMixin, _PlanningMixin, _LifecycleMixin,
    _RecordMixin, _ZoneMixin, _IdentityMixin, _FamilyMixin, _SearchMixin,
    _GraphMixin, _ToolMixin,
):
    """Derived-index facade assembled from focused mixins."""

    _DEFAULT_ZONE_WEIGHTS = {}
    _ZONE_WEIGHT_MIN = 1e-6
    _ZONE_WEIGHT_MAX = 1e6
    _ZONE_SCOPES = ("all", "semantic_only")
    _DEFAULT_DEDUP_THRESHOLD = None
    _DEFAULT_TRANSCRIPT_ZONES = frozenset({"raw"})

    def _resolve_embedder(self, prefer: str):
        """Resolve the patchable public embedder seam."""
        return get_embedder(prefer)

    def _write_retry(self):
        """Resolve the patchable public retry seam."""
        return with_write_retry

    def _grep_has_timeout(self) -> bool:
        """Read the patchable public grep capability seam."""
        return _GREP_HAS_TIMEOUT

    def _grep_engine(self):
        """Read the public regex-engine seam."""
        return _grep_engine

    def _grep_bounded_search(self, compiled, text: str):
        """Use the public bounded grep seam."""
        return _grep_bounded_search(compiled, text)


__all__ = [
    "BrainIndex", "GrepPatternError", "Hit", "RRF_K_EXACT", "RRF_K_FUSE",
    "SCHEMA_VERSION", "INDEX_FORMAT_VERSION", "FAMILY_MIN_BODY",
    "_ExactLeg", "_FamilyCollapse", "_NotePlan", "_ResumeState", "_SearchTrace",
    "_GREP_HAS_TIMEOUT", "_TEMPORAL_INTENT_RE", "_boilerplate_patterns",
    "_env_float", "_family_collapse_enabled", "_family_min_body", "_fusion_k_from_env",
    "_grep_bounded_search", "_grep_engine", "_matches_boilerplate_pattern",
    "_recency_factor", "_today", "rerank_gate_enabled", "with_write_retry", "get_embedder",
]
