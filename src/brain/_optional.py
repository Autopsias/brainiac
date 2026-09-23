"""The COS guard: is the private Chief of Staff installed in this engine?

The public release strips ``brain/cos`` (ADR 0013). Core code that reaches COS
asks ``cos_available()`` first, in one of the two shapes the export checker
accepts (``tools/export_check_no_cos.py``): inside ``if cos_available():``, or
after ``if not cos_available(): return ...`` at the top of the function.

A bare ``find_spec("brain.cos")`` is not enough. A pip upgrade from a COS build
to a COS-free one can leave ``brain/cos/__pycache__/`` behind, and Python reads
that orphan folder as a namespace package (pip#11835), so the import "works"
and every later attribute access fails. The guard therefore demands a real
``__init__.py`` AND one named submodule on disk. It never imports ``brain.cos``.

A PyInstaller binary (``packaging/*/brain-*.spec``) is the one exception: it
packs every module into an archive, so no source file exists on disk. There the
guard asks the frozen importer, which only knows modules the build packed —
no pip upgrade can leave an orphan inside a frozen archive.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from importlib.machinery import PathFinder

# Any real COS build carries this module; an orphaned __pycache__ never does.
_PROBE_SUBMODULE = "_layout.py"


def cos_available(brain_dirs: list[str] | None = None) -> bool:
    """True only when ``brain.cos`` is a real package with source on disk.

    ``brain_dirs`` overrides where ``brain`` lives (tests); by default it is
    this installed package's own directory.
    """
    if brain_dirs is None and getattr(sys, "frozen", False):
        return importlib.util.find_spec("brain.cos") is not None
    dirs = brain_dirs if brain_dirs is not None else [os.path.dirname(os.path.abspath(__file__))]
    spec = PathFinder.find_spec("brain.cos", dirs)
    origin = getattr(spec, "origin", None)
    if not origin or os.path.basename(origin) != "__init__.py" or not os.path.isfile(origin):
        return False
    return os.path.isfile(os.path.join(os.path.dirname(origin), _PROBE_SUBMODULE))
