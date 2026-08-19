"""COS layout operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403

def ops_dir(vault: Path | str | None = None) -> Path:
    return config.cos_ops_dir(vault)

def host_dir(vault=None) -> Path:
    return ops_dir(vault) / "host"

def shared_dir(vault=None) -> Path:
    return ops_dir(vault) / "shared"

def drop_dir(vault=None) -> Path:
    return ops_dir(vault) / "drop"

def proposal_drop_dir(vault=None) -> Path:
    return drop_dir(vault) / "proposal-drop"

def verdict_drop_dir(vault=None) -> Path:
    return drop_dir(vault) / "verdict-drop"

def evidence_dir(vault=None) -> Path:
    return host_dir(vault) / "evidence"

def proposals_dir(vault=None) -> Path:
    return host_dir(vault) / "proposals"

def hold_dir(vault=None) -> Path:
    return host_dir(vault) / "hold"

def corrections_db_path(vault=None) -> Path:
    return host_dir(vault) / "corrections.sqlite"

def priority_map_path(vault=None) -> Path:
    return shared_dir(vault) / "priority-map.md"

def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)

def _ts(dt: _dt.datetime | None = None) -> str:
    return (dt or _utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")

def _parse_ts(s: str) -> _dt.datetime | None:
    try:
        out = _dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return out if out.tzinfo else out.replace(tzinfo=_dt.timezone.utc)
    except (ValueError, TypeError):
        return None

def _env_days(env: str, default: int) -> int:
    try:
        return int(os.environ.get(env, default))
    except ValueError:
        return default

__all__ = ['ops_dir', 'host_dir', 'shared_dir', 'drop_dir', 'proposal_drop_dir', 'verdict_drop_dir', 'evidence_dir', 'proposals_dir', 'hold_dir', 'corrections_db_path', 'priority_map_path', '_utcnow', '_ts', '_parse_ts', '_env_days']
