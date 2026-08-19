"""COS taxonomy operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._learning_ledger import log_defect

def ingest_taxonomy(vault=None, *, log: bool = False) -> dict[str, Any]:
    """The active ingest taxonomy, in the STRICT convention s03 declared
    (docs/cos-ingest-taxonomy.md §5) — mirrored engine-side here:

    - ABSENT       -> ``mode="off"``: the whole category feature is off. No
                      stamping, no engine refusal, no defect.
    - UNPARSEABLE  -> ``mode="fail-closed"``: EVERY candidate is `propose`
                      (never `always`, never `never`), plus a logged defect.
    - one bad rule -> that rule already resolved to `propose` in the parser,
                      with a warning; the rest of the file still applies.
    """
    from .. import overlay as ov

    rep = ov.load_ingest_rules(vault)
    if not rep.get("present"):
        return {"mode": "off", "rules": {}, "warnings": []}
    rules = rep.get("rules") or {}
    issues = list(rep.get("issues") or [])
    if issues or not rules:
        if log:
            log_defect(vault, "ingest-taxonomy-unparseable",
                       "; ".join(issues) or "no category rules parsed from ingest.md")
        return {"mode": "fail-closed", "rules": {}, "warnings": rep.get("warnings", [])}
    return {"mode": "active", "rules": rules, "warnings": rep.get("warnings", [])}

def resolve_category(vault, claimed: Any, *, lane: str = LANE_TEXT,
                     taxonomy: dict[str, Any] | None = None) -> tuple[str, str]:
    """Validate a VM-CLAIMED category against the owner's taxonomy.

    Returns ``(category, disposition)``. An unknown/absent claim resolves to
    ``unclassified``/`propose` — the sentinel that is already in
    ``_UNPATTERNED`` and therefore can never graduate. A rule scoped to the
    OTHER lane is simply not consulted (the lane's own `propose` default
    applies), per docs/cos-ingest-taxonomy.md §3.
    """
    tax = taxonomy if taxonomy is not None else ingest_taxonomy(vault)
    if tax.get("mode") != "active":
        return CATEGORY_UNCLASSIFIED, DISPOSITION_PROPOSE
    cat = str(claimed or "").strip()
    rule = tax["rules"].get(cat)
    if not cat or not isinstance(rule, dict):
        return CATEGORY_UNCLASSIFIED, DISPOSITION_PROPOSE
    rule_lane = rule.get("lane", "both")
    if rule_lane not in ("both", lane):
        return cat, DISPOSITION_PROPOSE
    return cat, str(rule.get("disposition") or DISPOSITION_PROPOSE)

__all__ = ['ingest_taxonomy', 'resolve_category']
