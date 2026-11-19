"""Index duplicate-family collapse."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _FamilyMixin:
    """Index duplicate-family collapse."""

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
