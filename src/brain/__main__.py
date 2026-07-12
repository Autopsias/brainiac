"""``python -m brain`` entry point.

Mirrors the ``brain = "brain.cli:main"`` console script so the CLI is reachable
PATH-independently via the running interpreter (used by ``run_full_init``'s
post-seed index build, and handy in tests/CI). Keep in lockstep with the
console-script target in ``pyproject.toml``.
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
