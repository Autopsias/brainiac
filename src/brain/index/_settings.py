"""Index ranking setting helpers."""
from __future__ import annotations

import datetime as _dt
import os
import re
import sys
from functools import lru_cache

try:
    import regex as _grep_engine
    _GREP_HAS_TIMEOUT = True
except ImportError:  # pragma: no cover - exercised only when `regex` is absent
    _grep_engine = re
    _GREP_HAS_TIMEOUT = False

MAX_GREP_PATTERN_LEN = 200      # absurdly long patterns are the abuse surface, not legitimate use

GREP_REGEX_TIMEOUT_S = 2.0      # per-match wall-clock budget (only enforced with the `regex` engine)

def _today() -> _dt.date:
    """Today, overridable via ``BRAIN_NOW=YYYY-MM-DD`` so recency ranking is
    deterministic in tests (mirrors the injectable-clock pattern used by
    maintenance staleness)."""
    v = os.environ.get("BRAIN_NOW", "").strip()
    if v:
        try:
            return _dt.date.fromisoformat(v[:10])
        except ValueError:
            pass
    return _dt.date.today()

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default

def rerank_gate_enabled(requested: bool | None = None) -> bool:
    """Is the RK-02 adaptive rerank gate live for this call?

    Reranking is ON by default and costs seconds per query (BR-03: ~5.5s p50
    at the shipped window 20). The gate decides, per query, whether that spend
    can buy anything. Precedence mirrors the
    BR-03/ADR-0008 kill-switch pattern exactly: an explicit ``requested``
    (CLI ``--rerank-gate`` / ``--no-rerank-gate``) always wins; absent that,
    ``BRAIN_RERANK_GATE_DISABLED=1`` turns the gate off globally (restoring
    unconditional always-on reranking); absent both, the gate is ON.

    Turning the gate OFF never disables reranking — it only stops the engine
    from skipping it.
    """
    if requested is not None:
        return bool(requested)
    raw = os.environ.get("BRAIN_RERANK_GATE_DISABLED", "").strip().lower()
    return raw not in {"1", "true", "yes", "on"}

_DEFAULT_BOILERPLATE_PATTERNS = (
    "daily-*",
    "graph-health-alert-*",
    "audit-*-writes",
    "*-transcript",
)

def _boilerplate_patterns() -> tuple[str, ...]:
    raw = os.environ.get("BRAIN_INTEGRITY_BOILERPLATE_PATTERNS", "").strip()
    if not raw:
        return _DEFAULT_BOILERPLATE_PATTERNS
    return tuple(p.strip() for p in raw.split(",") if p.strip())

def _matches_boilerplate_pattern(note_id: str, patterns: tuple[str, ...]) -> str | None:
    import fnmatch

    for pat in patterns:
        if fnmatch.fnmatch(note_id, pat):
            return pat
    return None

_TEMPORAL_INTENT_RE = re.compile(
    r"\b(latest|newest|current(?:ly)?|recent(?:ly)?|as of|today|now|up[- ]to[- ]date|"
    r"this (?:week|month|quarter|year)|"
    r"atual(?:mente)?|mais recentes?|últim[oa]s?|recentes?|hoje|"
    r"est[ae] (?:semana|mês|trimestre|ano))\b", re.IGNORECASE)

@lru_cache(maxsize=8)
def _fusion_k_from_env(raw: str) -> int:
    """``$BRAIN_RRF_K`` as a positive int, else ``RRF_K_FUSE``.

    Cached on the raw string so a misconfigured value is reported once per
    process rather than once per query — and so a fresh value (a test changing
    the environment) still re-parses.
    """
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value >= 1:
        return value
    if raw:
        print(f"brain: ignoring BRAIN_RRF_K={raw!r} (want a positive integer); "
              f"fusing at {RRF_K_FUSE}", file=sys.stderr)
    return RRF_K_FUSE

def _recency_factor(date_str: str, today: _dt.date, weight: float,
                    half_life: float) -> float:
    """Gentle multiplicative STALENESS PENALTY for the RRF fusion, bounded to
    ``(1 - weight, 1.0]``. A note dated today (or in the future) is neutral at
    ``1.0``; the penalty deepens as the note ages, halving its distance-from-full
    every ``half_life`` days, asymptoting at ``1 - weight`` for very old notes.
    An undated note (or ``weight<=0``) is neutral at ``1.0`` — undated notes are
    never penalised.

    With ``w_exact_max = 2.25``, raw RRF never exceeds
    ``(2 + w_exact_max) / (k + 1) = 4.25 / (k + 1)`` at the constant the legs
    were fused at (``RRF_K_FUSE``, not ``RRF_K_EXACT``). This factor is a penalty
    in ``(0, 1]``; the separately configured zone-authority multiplier is
    applied after raw RRF and may intentionally exceed 1. Relative order
    between any two dated notes is identical to a symmetric boost, so the newer
    of two topically-similar hits still wins."""
    if weight <= 0 or not date_str:
        return 1.0
    try:
        d = _dt.date.fromisoformat(date_str[:10])
    except ValueError:
        return 1.0
    age = (today - d).days
    if age <= 0:
        return 1.0
    return 1.0 - weight * (1.0 - 0.5 ** (age / half_life))

class GrepPatternError(ValueError):
    """A user-supplied grep pattern was rejected before compilation."""

def _grep_bounded_search_impl(compiled, text: str, *, has_timeout: bool):
    """Search one line through the bounded regex engine."""
    if has_timeout:
        try:
            return compiled.search(text, timeout=GREP_REGEX_TIMEOUT_S)
        except TimeoutError:
            return None
    return compiled.search(text)


SCHEMA_VERSION = 4  # ADR-0008: normalized title projection + aliases table.

INDEX_FORMAT_VERSION = 1  # RB-02: bumped whenever resume-compatibility breaks.

RRF_K_EXACT = 60

RRF_K_FUSE = 3

FAMILY_MIN_BODY = 1024

def _family_min_body() -> int:
    raw = os.environ.get("BRAIN_FAMILY_MIN_BODY", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return FAMILY_MIN_BODY

def _family_collapse_enabled() -> bool:
    return os.environ.get(
        "BRAIN_FAMILY_COLLAPSE_DISABLED", ""
    ).strip().lower() not in {"1", "true", "yes", "on"}

EXACT_WEIGHT_UNIQUE_FULL = 2.25

EXACT_WEIGHT_COLLIDING_FULL = 1.0

EXACT_WEIGHT_PARTIAL_TITLE = 0.25

EXACT_FULL_CAP = 16

EXACT_PARTIAL_CAP = 16
