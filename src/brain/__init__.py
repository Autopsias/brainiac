"""Profile A `brain` — local any-LLM second-brain core engine.

Built FROM SCRATCH (FLEET/AGPL): basic-memory was a clean-room design reference
only — no fork, no vendored modules, no imports. See docs/clean-room-log.md.

Public surface:
- `brain.core.BrainCore` — the engine (importable, UNFILTERED; in-process use
  bypasses the egress filter by design — it is NOT the integration surface).
- `brain.cli` — the integration surface: applies the deny-by-default
  classification filter as the final stage before stdout.

The Markdown files in `vault/` are the single source of truth. The SQLite index
is a derived, disposable cache — delete-and-rebuild is always safe.
"""

__version__ = "0.2.0"

__all__ = ["__version__"]
