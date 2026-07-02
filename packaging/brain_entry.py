"""Frozen-binary entry point for the `brain` CLI.

PyInstaller freezes a *script*, not a package, so a relative-import module
(``src/brain/cli.py`` uses ``from . import ...``) cannot be the entry script
directly — it loses its package context and ImportErrors at launch. This shim
is the entry script: it imports the package the normal way (the package dir is
on ``pathex`` in the .spec) and delegates to ``brain.cli.main``.

This is the SAME ``main()`` that ``[project.scripts] brain = "brain.cli:main"``
exposes for a pip install, so the frozen binary and the pip console-script run
identical code.

DIST-02 (bundled model): when running frozen, the e5-small ONNX model is
bundled at ``_INTERNAL/e5-small/`` (PyInstaller ``sys._MEIPASS``). We export
``BRAIN_MODEL_CACHE`` at that dir ONLY when it is not already set, so
``OnnxEmbedder`` finds the bundled snapshot offline — no HF download, no network
on the corporate read path.
"""
import os
import sys


def _wire_bundled_model() -> None:
    """If frozen and the e5-small model is bundled inline, point the embedder at it."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass and not os.environ.get("BRAIN_MODEL_CACHE"):
        bundled = os.path.join(meipass, "e5-small")
        if os.path.isdir(bundled):
            os.environ["BRAIN_MODEL_CACHE"] = bundled


_wire_bundled_model()

from brain.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
