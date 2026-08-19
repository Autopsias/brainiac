"""COS public-seam access."""
from __future__ import annotations

import sys


def public(name: str):
    """Return one patched public COS seam."""
    return getattr(sys.modules[__package__], name)
