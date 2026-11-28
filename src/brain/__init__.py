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

def _resolve_version() -> str:
    try:
        # Primary: the installed package's metadata (pip-installed host). This
        # reports what is ACTUALLY installed — the signal the host skew checks
        # (brain status / /brainiac-update Step 2) depend on. ADR-0005 Ruling 1.
        from importlib.metadata import version as _pkg_version

        try:
            # PYP-01: distribution renamed to `brainiac-cli` (`brainiac` is taken
            # on PyPI); the import package stays `brain`.
            return _pkg_version("brainiac-cli")
        except Exception:
            # pre-rename install metadata (an old editable/venv install)
            return _pkg_version("profile-a-brain")
    except Exception:  # PackageNotFoundError or metadata missing
        try:
            # Fallback: the COMMITTED stamp written by tools/package_clients.py in
            # the same act as the pyproject version bump (tools/release.py). This
            # is what the zero-install Cowork VM (staged source, PYTHONPATH-only)
            # and the clean-room export report. ADR-0005 Ruling 1.
            from brain._version import __version__ as _stamped
            return _stamped
        except Exception:  # pragma: no cover - stamp deleted from the tree
            return "0.0.0+unknown"


def __getattr__(name: str) -> str:
    # PEP 562 lazy __version__: avoids scanning site-packages/*.dist-info via
    # importlib.metadata on every lightweight submodule invocation (e.g.
    # `python -I -m brain.egress_guard`), keeping cold-start p95 well <200ms.
    if name == "__version__":
        val = _resolve_version()
        globals()["__version__"] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["__version__"]
