"""COS learning-config operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._layout import host_dir

def autocap_config_path(vault=None) -> Path:
    return host_dir(vault) / "autocap-config.json"

def _autocap_defaults() -> dict[str, Any]:
    return {
        "min_volume": _env_int(AUTOCAP_MIN_VOLUME_ENV, DEFAULT_AUTOCAP_MIN_VOLUME),
        "min_lower_bound": _env_float(AUTOCAP_MIN_LOWER_BOUND_ENV,
                                      DEFAULT_AUTOCAP_MIN_LOWER_BOUND),
        "undo_hours": _env_int(AUTOCAP_UNDO_HOURS_ENV, DEFAULT_AUTOCAP_UNDO_HOURS),
        # LRN-02: post-graduation the only negative signal left is an undo, so
        # a graduated category keeps sampling (1-in-K back through the batch)
        # and its evidence is recency-windowed rather than all-time.
        "exploration_k": _env_int(AUTOCAP_EXPLORATION_K_ENV,
                                  DEFAULT_AUTOCAP_EXPLORATION_K),
        "window_days": _env_int(AUTOCAP_WINDOW_DAYS_ENV,
                                DEFAULT_AUTOCAP_WINDOW_DAYS),
        "window_verdicts": _env_int(AUTOCAP_WINDOW_VERDICTS_ENV,
                                    DEFAULT_AUTOCAP_WINDOW_VERDICTS),
        "bulk_accept_max_batch": _env_int(AUTOCAP_BULK_MAX_BATCH_ENV,
                                          DEFAULT_AUTOCAP_BULK_MAX_BATCH),
    }

def _env_int(env: str, default: int) -> int:
    try:
        return int(os.environ.get(env, default))
    except ValueError:
        return default

def _env_float(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, default))
    except ValueError:
        return default

def load_autocap_config(vault=None) -> dict[str, Any]:
    """Owner-editable, HOST-only criteria store (never skill text — the
    'learned pattern thresholds live in cos-ops config' requirement). Missing
    file = pure env-var defaults for every pattern.

    ``patterns`` and ``categories`` are two INDEPENDENT per-key override maps
    over the same defaults — the category key is additive (LRN-02), it never
    reuses or collides with the opaque ``pattern`` key's own overrides.
    """
    defaults = _autocap_defaults()
    p = autocap_config_path(vault)
    patterns: dict[str, Any] = {}
    categories: dict[str, Any] = {}
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            defaults.update({k: v for k, v in raw.items() if k in defaults})
            if isinstance(raw.get("patterns"), dict):
                patterns = raw["patterns"]
            if isinstance(raw.get("categories"), dict):
                categories = raw["categories"]
    return {"defaults": defaults, "patterns": patterns, "categories": categories}

def _pattern_config(vault, pattern: str) -> dict[str, Any]:
    cfg = load_autocap_config(vault)
    out = dict(cfg["defaults"])
    out.update(cfg["patterns"].get(pattern, {}) if isinstance(cfg["patterns"], dict) else {})
    return out

def _category_config(vault, category: str) -> dict[str, Any]:
    cfg = load_autocap_config(vault)
    out = dict(cfg["defaults"])
    cats = cfg["categories"]
    out.update(cats.get(category, {}) if isinstance(cats, dict) else {})
    return out

__all__ = ['autocap_config_path', '_autocap_defaults', '_env_int', '_env_float', 'load_autocap_config', '_pattern_config', '_category_config']
