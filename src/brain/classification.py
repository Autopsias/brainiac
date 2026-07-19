"""Classification tiers + the deny-by-default egress filter (CORE-02).

This is the *egress-decision mechanism*, NOT containment. It only decides what
the cooperative `brain` CLI is willing to print to stdout. Any file-capable
harness can read the Markdown directly and bypass it entirely — that is why real
containment of sensitive tiers is **workspace projection** (see
``brain.projection``) plus the host/VM trust split, not this filter. The
consensus-hardening tests (tests/test_direct_file_read.py) prove this distinction.

Tiers, low -> high sensitivity:
    Public < Internal < Confidential < Restricted < MNPI

Default-deny (load-bearing): a note whose ``classification`` is missing, empty,
or unrecognised is treated as MNPI (rank 4, most restrictive) at every surfacing
boundary — fail-closed, never fail-open.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, Sequence

TIERS: tuple[str, ...] = ("Public", "Internal", "Confidential", "Restricted", "MNPI")
RANK: dict[str, int] = {t: i for i, t in enumerate(TIERS)}

# The tier an unlabelled / unrecognised note is treated as (most restrictive).
DEFAULT_DENY_TIER = "MNPI"
DEFAULT_DENY_RANK = RANK[DEFAULT_DENY_TIER]

# Default egress cap on the TRUSTED HOST: the full vault (owner decision,
# 2026-07-10 — the old Internal default starved every real query: a curated
# vault keeps its load-bearing notes at Confidential/Restricted, so the host
# surface answered from stale low-tier scraps while competitors read
# everything). $BRAIN_DEFAULT_MAX_TIER narrows it back (e.g. "Internal") for
# deployments that want the conservative gate; an unrecognised value falls
# back to the full-vault default. The untrusted VM leg does NOT inherit this:
# role=vm resolves to VM_DEFAULT_MAX_TIER below (the trifecta break lives at
# the role boundary, not on the owner's own host).
DEFAULT_MAX_TIER = os.environ.get("BRAIN_DEFAULT_MAX_TIER", "MNPI")
if DEFAULT_MAX_TIER not in RANK:
    DEFAULT_MAX_TIER = "MNPI"

# Conservative default for the untrusted read+draft leg (role=vm): surface
# Public + Internal only unless a human explicitly elevates with --max-tier.
VM_DEFAULT_MAX_TIER = "Internal"

# Hard server-side ceiling for the untrusted VM leg (codex 2026-07-19). On
# role=vm, --max-tier is a caller-controlled argument the LLM can set itself;
# without a clamp the egress-starvation hint ("re-run with --max-tier
# Restricted") turns the intended HUMAN-gated elevation into an instruction the
# model follows autonomously. This is the CLI analogue of the MCP adapter's
# BRAIN_MAX_EGRESS_TIER: a VM caller can never exceed this ceiling regardless of
# what it types. The host operator raises it deliberately via the env var (the
# real human gate); an unrecognised value falls back to VM_DEFAULT_MAX_TIER.
VM_EGRESS_CEILING_ENV = "BRAIN_VM_MAX_EGRESS_TIER"


def vm_egress_ceiling() -> str:
    """The tier a role=vm caller may never exceed. Operator-settable via
    $BRAIN_VM_MAX_EGRESS_TIER; defaults to the conservative VM cap."""
    val = os.environ.get(VM_EGRESS_CEILING_ENV, "").strip()
    return val if val in RANK else VM_DEFAULT_MAX_TIER


def clamp_to(requested: str, ceiling: str) -> str:
    """Return the lower (less-sensitive) of two tiers by rank — a caller can
    never widen egress past the ceiling, only narrow within it."""
    req = requested if requested in RANK else DEFAULT_DENY_TIER
    ceil = ceiling if ceiling in RANK else VM_DEFAULT_MAX_TIER
    return req if RANK[req] <= RANK[ceil] else ceil


def normalize(value: object) -> str:
    """Map a raw frontmatter value to a recognised tier, default-deny on miss."""
    if isinstance(value, str) and value.strip() in RANK:
        return value.strip()
    return DEFAULT_DENY_TIER


def rank(value: object) -> int:
    """Effective sensitivity rank, default-deny (unlabelled -> MNPI rank)."""
    return RANK[normalize(value)]


def is_default_denied(value: object) -> bool:
    """True iff the raw value would be coerced to the default-deny tier."""
    return not (isinstance(value, str) and value.strip() in RANK)


# Lowercased tier -> canonical, for detecting casing mistakes (F-04).
_CANON_BY_LOWER = {t.lower(): t for t in TIERS}


def casing_mismatch(value: object) -> str | None:
    """If ``value`` is a KNOWN tier in the wrong case (e.g. 'internal'), return
    its canonical form; else None.

    DESIGN DECISION (F-04): the filter keeps STRICT matching — a non-canonical
    value is default-denied (fail-closed), never silently up-ranked (which would
    be fail-OPEN). But a wrong-case known tier is almost always an authoring slip
    that would make the note invisible forever, so we surface it as a diagnostic
    here (and in redaction_report) instead of letting it vanish silently. The
    fix-at-source is tools/validate.py, which flags non-canonical casing.
    """
    if isinstance(value, str):
        v = value.strip()
        if v not in RANK and v.lower() in _CANON_BY_LOWER:
            return _CANON_BY_LOWER[v.lower()]
    return None


@dataclass(frozen=True)
class ClassificationFilter:
    """Deny-by-default egress filter applied as the FINAL stage before stdout.

    A note is surfaceable iff its effective rank <= the caller's max-tier rank.
    Unlabelled/unrecognised -> MNPI -> only surfaceable when max_tier is MNPI
    (the explicit human-gated path).
    """

    max_tier: str = DEFAULT_MAX_TIER

    def __post_init__(self) -> None:
        if self.max_tier not in RANK:
            raise ValueError(
                f"unknown max_tier {self.max_tier!r}; expected one of {TIERS}"
            )

    @property
    def max_rank(self) -> int:
        return RANK[self.max_tier]

    def allows(self, classification: object) -> bool:
        return rank(classification) <= self.max_rank

    def filter(self, items: Iterable[dict], key: str = "classification") -> list[dict]:
        """Drop any item whose classification exceeds the cap. Pure; no mutation."""
        return [it for it in items if self.allows(it.get(key))]

    def redaction_report(self, items: Sequence[dict], key: str = "classification") -> dict:
        """How many items were withheld and why (for an honest CLI footer)."""
        denied = [it for it in items if not self.allows(it.get(key))]
        default_denied = sum(1 for it in denied if is_default_denied(it.get(key)))
        # Surface wrong-case known tiers (F-04) so they don't vanish silently.
        casing = sorted({
            f"{it.get(key)!r}->{c}"
            for it in items
            if (c := casing_mismatch(it.get(key))) is not None
        })
        report = {
            "total": len(items),
            "surfaced": len(items) - len(denied),
            "withheld": len(denied),
            "withheld_unlabelled_default_deny": default_denied,
            "max_tier": self.max_tier,
        }
        if casing:
            report["casing_mismatch_warnings"] = casing
        return report
