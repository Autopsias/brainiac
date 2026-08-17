#!/usr/bin/env python3
"""Re-export shim — the registry helper moved into the wheel (PRV-10, 2026-08-17).

The implementation lives at ``src/brain/workspaces.py`` so the installed
engine (and the provision drain inside it) can import it without a checkout.
Skills that ``sys.path.insert(<repo>/tools)`` and ``import workspace_registry``
keep working through this shim.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from brain.workspaces import *  # noqa: F401,F403
    from brain.workspaces import _demo  # noqa: F401
except ImportError:
    # Checkout-only use (no installed engine): reach the sibling src/ tree.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from brain.workspaces import *  # noqa: F401,F403
    from brain.workspaces import _demo  # noqa: F401

if __name__ == "__main__":
    _demo()
