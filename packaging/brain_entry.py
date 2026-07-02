"""Frozen-binary entry point for the `brain` CLI.

PyInstaller freezes a *script*, not a package, so a relative-import module
(``src/brain/cli.py`` uses ``from . import ...``) cannot be the entry script
directly — it loses its package context and ImportErrors at launch. This shim
is the entry script: it imports the package the normal way (the package dir is
on ``pathex`` in the .spec) and delegates to ``brain.cli.main``.

This is the SAME ``main()`` that ``[project.scripts] brain = "brain.cli:main"``
exposes for a pip install, so the frozen binary and the pip console-script run
identical code.
"""
import sys

from brain.cli import main

if __name__ == "__main__":
    sys.exit(main())
