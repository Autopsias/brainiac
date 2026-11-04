"""Subprocess steps that FINISH a freshly seeded vault.

Split out of ``init.py`` (2026-08-17) because both functions are the same
kind of thing — work that must happen after ``seed_sample_notes`` has put
files on disk, run as a subprocess so ``brain.init`` stays ``BrainCore``-free
and light (importing ``BrainCore`` would pull the embedder into every
``brain init``, even a dry-run scaffold).

Both SOFT-FAIL by contract: a box with no embedder, or no signing key, still
completes ``brain init``. What they must never do is fail silently — each
returns a report the caller renders into its ``steps`` list.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import config


def _build_index(vault: str | os.PathLike[str] | None) -> dict[str, Any]:
    """Build the derived index for a freshly-seeded vault via a subprocess.

    ONB fix (2026-07-11): ``seed_sample_notes`` writes notes to ``vault/brain/``
    but nothing indexed them, so `brain init --full --apply` left a vault where
    the very first `brain search` returned zero hits (the documented "init then
    search" quickstart was broken). We shell out to `brain rebuild` rather than
    constructing a ``BrainCore`` here so this module stays index-free and light
    (importing BrainCore would pull the embedder into every `brain init`, even a
    dry-run/scaffold) — matching the module's "filesystem + subprocess only"
    contract. Invoked via ``python -m brain`` (see ``brain/__main__.py``) so it
    is PATH-independent. Soft-fails: a rebuild error is reported, never aborts
    init (a box without the embedder can still scaffold; the user reruns
    `brain rebuild` once the engine is whole).
    """
    argv = [sys.executable, "-m", "brain"]
    if vault is not None:
        argv += ["--vault", str(vault)]
    argv.append("rebuild")
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    except Exception as exc:  # subprocess spawn failure, timeout, etc.
        return {"performed": False, "ok": False,
                "reason": f"{type(exc).__name__}: {exc}"}
    if proc.returncode == 0:
        return {"performed": True, "ok": True}
    return {"performed": True, "ok": False,
            "reason": f"rebuild exit {proc.returncode}",
            "stderr": (proc.stderr or "").strip()[-500:]}


def _sign_seeded_notes(
    vault: str | os.PathLike[str] | None, created: list[str],
) -> dict[str, Any]:
    """Put the freshly-seeded notes into the audit chain.

    ONB fix (2026-08-17): ``seed_sample_notes`` writes its notes with a plain
    ``write_text``, so a brand-new vault was born with every seed note OUTSIDE
    the audit chain. ``invariants.unsigned_notes`` counted them, and because
    that metric ratchets on its best-ever value, the floor was then set to the
    seed count — the watchdog reported "no regression" forever while the notes
    it was built to notice sat there permanently accepted. Measured on a real
    vault: 13 unsigned notes, floor 13.

    Signing them is the fix rather than excluding them: the invariant exists to
    say "this content was admitted by the host", and for seeded notes that is
    simply TRUE — the host did write them. Excluding them would make the metric
    read zero by definition instead of by fact, and the generated-map exclusion
    it would imitate covers files that are regenerated from other notes, which
    these are not.

    Same contract as :func:`_build_index` and for the same reason: a subprocess
    per note (this module stays BrainCore-free and light), and SOFT-FAILS — a
    box with no signing key still inits, it just leaves the notes unsigned, and
    the invariant will say so honestly. Content is passed on stdin, never
    through a shell, so the note's exact bytes are signed.
    """
    signed, failed = [], []
    for rel in created:
        relpath = f"brain/{rel}"
        target = Path(config.vault_root(vault, allow_missing=True)) / relpath
        try:
            body = target.read_bytes()
        except OSError as exc:
            failed.append({"path": relpath, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        argv = [sys.executable, "-m", "brain"]
        if vault is not None:
            argv += ["--vault", str(vault)]
        argv += ["write", relpath, "--reason", "seeded by `brain init`"]
        try:
            proc = subprocess.run(argv, input=body, capture_output=True, timeout=300)
        except Exception as exc:  # spawn failure, timeout
            failed.append({"path": relpath, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        if proc.returncode == 0:
            signed.append(relpath)
        else:
            failed.append({"path": relpath,
                           "reason": f"write exit {proc.returncode}",
                           "stderr": (proc.stderr or b"").decode(
                               "utf-8", "replace").strip()[-300:]})
    return {"performed": True, "ok": not failed,
            "signed": signed, "failed": failed}
