"""OW-01 — never let `brain update` swap the owner's COS-bearing engine for a
build that has no COS, split out of ``update_channels.py`` at the 2026-09-23
size ratchet (the guard's own file-size cost, not new debt on that module).

Ruling A-03: `brain update` never gates on an attestation, so this is a
refusal with a report, never a gate — it decides on the install ABOUT TO BE
REPLACED (never the calling process's own interpreter) and on the SOURCE the
update is about to install from, and refuses only when the first carries the
private ``brain/cos`` module and the second does not.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .doctor import CHANNEL_PIP_USER, CHANNEL_PIPX, CHANNEL_PYPI_UV


def _dir_carries_cos(root: Path) -> bool:
    """A checkout or install root carries the private COS module either
    laid out flat (a wheel/venv install: ``<root>/brain/cos``) or under
    ``src/`` (a checkout: ``<root>/src/brain/cos``), or IS the package dir
    itself (a setuptools editable finder maps ``brain`` straight to
    ``<checkout>/src/brain``)."""
    return (
        (root / "brain" / "cos" / "__init__.py").exists()
        or (root / "src" / "brain" / "cos" / "__init__.py").exists()
        or (root.name == "brain" and (root / "cos" / "__init__.py").exists())
    )


def _venv_site_packages(install_root: Path) -> list[Path]:
    """Every site-packages dir under an install root, POSIX or Windows,
    python-version-agnostic — the same glob shape `detect_install_channel`
    already uses to spot an editable marker."""
    found = list((install_root / "lib").glob("*/site-packages"))
    windows = install_root / "Lib" / "site-packages"
    if windows.is_dir():
        found.append(windows)
    return found


def _editable_candidate_dirs(site_packages: Path) -> list[Path]:
    """Best-effort resolution of an editable install's real source tree from
    the marker(s) ``pip install -e`` leaves behind — a plain ``.pth`` naming
    the checkout root, or a ``__editable___<x>_finder.py`` carrying a
    ``MAPPING`` dict of quoted paths. Ponytail: a text scan, not a real
    import — good enough to answer "does the CURRENTLY installed editable
    checkout carry COS", which is all the guard below needs."""
    candidates: list[Path] = []
    for marker in site_packages.glob("__editable__*"):
        try:
            text = marker.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith(("import ", "#")):
                candidate = Path(line)
                if candidate.is_dir():
                    candidates.append(candidate)
        for quoted in re.findall(r"""['"]([^'"]+)['"]""", text):
            candidate = Path(quoted)
            if candidate.is_dir():
                candidates.append(candidate)
    return candidates


def target_carries_cos(brain_bin: Optional[Path]) -> bool:
    """Whether the install ABOUT TO BE REPLACED already carries the private
    COS module — judged on that install's own site-packages (or, for an
    editable checkout, the source tree its editable marker names). Never on
    the running process: `phase_local_deploy` invokes `sys.executable -m
    brain update`, and `sys.executable` can be a completely different,
    COS-free interpreter than the venv this call is about to overwrite —
    `brain_bin` is the resolved TARGET binary, not `sys.executable`.

    No resolvable target (nothing installed yet) means nothing to protect.
    """
    if brain_bin is None or not brain_bin.exists():
        return False
    # A `brain` on PATH is often a symlink into the venv (pipx, uv tool), so
    # judge the resolved install root as well as the literal one.
    roots = {brain_bin.parent.parent, brain_bin.resolve().parent.parent}
    for site_packages in (sp for root in roots for sp in _venv_site_packages(root)):
        if _dir_carries_cos(site_packages):
            return True
        for candidate in _editable_candidate_dirs(site_packages):
            if _dir_carries_cos(candidate):
                return True
    return False


def new_source_carries_cos(
    channel: str, engine_src: Optional[Path], has_checkout: bool
) -> bool:
    """Whether the SOURCE `brain update` is about to install FROM carries
    COS. The three PyPI channels publish the public, COS-free build
    unconditionally; a venv-wheel refresh with no checkout resolved falls
    back to that same PyPI wheel (see the `has_checkout` branch below).
    Anything else is a checkout, which carries COS only when its own tree
    does — an export folder or a public clone does not."""
    if channel in (CHANNEL_PYPI_UV, CHANNEL_PIPX, CHANNEL_PIP_USER):
        return False
    if not has_checkout or engine_src is None:
        return False
    return (engine_src / "src" / "brain" / "cos" / "__init__.py").exists()


def cos_guard_refusal(old_version: str, channel: str, brainiac_home: Path) -> dict:
    """OW-01: never swap a COS-bearing install for a build with no COS. A
    refusal with a report, per ruling A-03 — never an attestation gate."""
    detail = (
        "refusing to replace a COS-bearing install with a build that has no "
        "COS — reinstall from the private checkout instead: "
        f"{brainiac_home / 'venv' / 'bin' / 'pip'} install --upgrade "
        "-e '<private-checkout>[mcp]'"
    )
    return {
        "ok": False,
        "old_version": old_version,
        "new_version": old_version,
        "detail": detail,
        "channel": channel,
    }
