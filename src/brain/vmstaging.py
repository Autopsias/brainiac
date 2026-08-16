"""VM-facing staged artifacts: the frozen Linux ELFs, their freshness verdict,
and the ``brain doctor`` surface that reports them.

WHY THIS MODULE EXISTS (2026-08-16). The Cowork VM binary was the one staged
artifact with NO freshness check, and the knowledge about it was split in two:
``cowork_workspace_install.sh`` staged the ELFs, ``update.py`` did not, and
``doctor.py`` had no row for them. Each half looked complete on its own.

The concrete failure: ``cowork_workspace_install.sh``'s leg (a) is the ELFs,
while ``update.py``'s own "(a)" is the PYTHON SOURCE. Same letter, different
leg — so every ``brain update`` refreshed the engine, model and skills while
``.brain/bin/`` kept whatever binary was last staged by hand. A 0.17.0 binary
sat under a 0.20.12 engine for a month.

It stayed invisible because ``SHA256SUMS`` proves a binary is INTACT, never
that it is CURRENT, and the macOS host cannot execute a Linux ELF to ask its
version. Hence the ``.version`` marker written by
``tools/build_brain_binary.sh``: freshness has to be a file the host can read.

It was not cosmetic. ``tools/cowork_session_bootstrap.sh`` does
``ln -sf bin/brain-linux-$(uname -m) $BRAIN_VAULT/.brain/brain`` and
PATH-prepends it, so the ELF OVERWRITES the zero-install shim and is what
``brain`` means inside a bootstrapped session. A stale one cannot read a newer
snapshot schema and falls back to HashEmbedder — a real-model index queried
with random vectors, and no error raised.

Generalised: integrity is not freshness. Any staged artifact needs a version
marker the host can read WITHOUT executing it, or it cannot be checked at all.
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

ARCHES = ("x86_64", "aarch64")


def stage_vm_binaries(brain_dir: Path, dist: Path) -> dict:
    """Copy each frozen ELF, its ``.version`` marker, and rewrite SHA256SUMS.

    Regenerating the manifest is not optional: ``cowork_session_bootstrap.sh``
    verifies it before trusting a binary, so new ELFs under an old manifest
    would be REFUSED and the VM session would abort.
    """
    bin_dir = brain_dir / "bin"
    status: dict[str, Any] = {"shipped": 0, "versions": {}, "missing_version": []}
    staged_any = False
    for arch in ARCHES:
        src = dist / f"brain-linux-{arch}"
        if not src.is_file():
            continue
        bin_dir.mkdir(parents=True, exist_ok=True)
        dst = bin_dir / f"brain-linux-{arch}"
        shutil.copyfile(src, dst)
        dst.chmod(0o755)
        staged_any = True
        status["shipped"] += 1
        marker = src.with_name(src.name + ".version")
        dst_marker = bin_dir / f"brain-linux-{arch}.version"
        if marker.is_file():
            shutil.copyfile(marker, dst_marker)
            status["versions"][arch] = marker.read_text(encoding="utf-8").strip()
        else:
            # An ELF built before the marker existed. Drop any leftover marker
            # rather than let it vouch for a binary it does not describe.
            dst_marker.unlink(missing_ok=True)
            status["missing_version"].append(arch)

    if staged_any:
        lines = []
        for path in sorted(bin_dir.glob("brain-linux-*")):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {path.name}")
        (bin_dir / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return status


def stage_vm_support_files(brain_dir: Path, engine_src: Path, packaged_script) -> dict:
    """The VM-facing extras that ride alongside engine+model+skills.

    Moved here from ``update.py`` on 2026-08-16 (size ratchet) — it is the same
    concern as the rest of this module: what a Cowork VM reads out of
    ``.brain/``. ``packaged_script`` is injected rather than imported to keep
    this module free of a cycle back into ``update``.

    (c/c2/c3) offline semantic stack (DV-04) — the re-stage previously staged
    ONLY engine+skills, so a `brain update` shipped the fixed engine but left
    the VM without vendored tokenizers/sqlite-vec and with a stale shim/prompt:
    semantic search silently stayed on the hash fallback. Stage all three from
    the SAME shared helper the installer uses, so update == install.
    """
    vendor_status: dict[str, str] = {}
    try:
        import sys as _sys
        _sys.path.insert(0, str(engine_src / "tools"))
        import vendor_semantic_deps as _vsd  # type: ignore

        _vsd.write_shim(brain_dir)                       # vendored-deps-aware shim
        vendor_status = _vsd.stage_vendor(brain_dir)     # tokenizers + sqlite-vec per arch
    except Exception as exc:  # advisory — a networkless host degrades to lexical
        vendor_status = {"error": f"{type(exc).__name__}: {exc}"}

    # session prompt — the instruction the Cowork agent follows each session.
    prompt_src = engine_src / "docs" / "install" / "cowork-session-prompt.md"
    if prompt_src.exists():
        routines_dst = brain_dir / "routines"
        routines_dst.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(prompt_src, routines_dst / "cowork-session-prompt.md")

    # conventions contract — the AGENTS.md the session prompt points the agent at
    # (cowork_workspace_install.sh leg, line ~218). The re-stage previously
    # SKIPPED it, so an AGENTS.md change (e.g. the retrieval-discipline block)
    # never reached Cowork on `brain update`: the doctor's staged-version check
    # reads the engine stamp and is blind to a stale contract. Copy it so
    # update == install for the contract too.
    agents_src = engine_src / "AGENTS.md"
    if agents_src.exists():
        shutil.copyfile(agents_src, brain_dir / "AGENTS.md")

    # The two VM probes, staged 0755 so a Cowork session can run either one
    # straight from the workspace root. Both ship in the wheel via
    # _assets/scripts:
    #   vm-selftest.sh        the un-fakeable PASS/FAIL retrieval self-test —
    #                         proves the VM leg WORKS (step 8).
    #   vm-boundary-probe.sh  the NEGATIVE half — proves it REFUSES everything
    #                         the host broker holds. It was hand-copied into
    #                         one workspace on 2026-07-31 and would have
    #                         vanished on the next re-stage; a boundary claim
    #                         is only worth what can be re-measured on demand.
    for script_name in ("vm-selftest.sh", "vm-boundary-probe.sh"):
        script_src = packaged_script(script_name, engine_src)
        if script_src is not None:
            dst = brain_dir / script_name
            shutil.copyfile(script_src, dst)
            dst.chmod(0o755)
    return vendor_status


def vm_binaries_verdict(status: dict, ssot: str | None) -> tuple[bool, str]:
    """(ok, human detail) for a staged-binary set.

    A version that DISAGREES with SSOT is a definite defect and fails the
    re-stage. A MISSING marker does not: every ELF built before 2026-08-16
    lacks one, the ELFs are optional (a VM with python3 uses the shim), and the
    rebuild needs Docker — so failing hard would block the engine and skill
    legs that staged perfectly, on exactly the hosts least able to fix it. It
    is reported instead, and the doctor row below shows UNKNOWN on every run,
    so it can never be silent.
    """
    versions = set(status["versions"].values())
    if not versions:
        return True, "no versioned ELF staged"
    if versions == {ssot} and ssot is not None:
        return True, f"staged {ssot}"
    return False, (
        f"staged VM binary is {sorted(versions)} but SSOT is {ssot!r} — a bootstrapped "
        "VM session PATH-prefers this binary, so retrieval would silently run the old "
        "engine. Rebuild with tools/build_brain_binary_linux.sh"
    )


def check_staged_vm_binaries(registry_entries: list[dict], ssot: str) -> list[dict]:
    """``brain doctor`` surface for ``.brain/bin/brain-linux-*``.

    An unversioned binary is UNKNOWN, never assumed current — that assumption
    is exactly what let a three-release-old ELF hide behind a passing
    SHA256SUMS check.
    """
    from .doctor import (
        CURRENT,
        NOT_DETECTABLE,
        STALE,
        UNKNOWN,
        _cowork_vault_dir,
        _row,
    )

    remediation = "tools/build_brain_binary_linux.sh, then /brainiac-update"
    rows = []
    for entry in registry_entries:
        if entry.get("target") == "host":
            continue
        vault_dir = _cowork_vault_dir(entry)
        surface = f"Staged VM binary ({vault_dir})"
        bin_dir = Path(vault_dir) / ".brain" / "bin"
        elves = [
            p for p in sorted(bin_dir.glob("brain-linux-*"))
            if p.suffix != ".version" and p.name != "SHA256SUMS"
        ] if bin_dir.is_dir() else []
        if not elves:
            # Legitimately optional: a VM with python3 runs the staged source
            # through the .brain/brain shim and needs no ELF at all.
            rows.append(_row(surface, NOT_DETECTABLE,
                             f"no frozen ELF staged in {bin_dir} — fine when the VM has "
                             "python3 (the .brain/brain shim runs the staged source)"))
            continue
        seen: dict[str, str] = {}
        unversioned = []
        for elf in elves:
            marker = elf.with_name(elf.name + ".version")
            if marker.is_file():
                seen[elf.name] = marker.read_text(encoding="utf-8").strip()
            else:
                unversioned.append(elf.name)
        if unversioned:
            rows.append(_row(
                surface, UNKNOWN,
                f"no .version marker for {', '.join(unversioned)} — built before the marker "
                "existed, so freshness cannot be proven (SHA256SUMS proves integrity only)",
                remediation=remediation,
                raw={"unversioned": unversioned, "versioned": seen}))
            continue
        stale = {name: v for name, v in seen.items() if v != ssot}
        if stale:
            detail = ", ".join(f"{n}={v}" for n, v in sorted(stale.items()))
            rows.append(_row(
                surface, STALE,
                f"staged {detail} != SSOT {ssot} — a bootstrapped VM session PATH-prefers "
                "this binary over the shim, so retrieval silently runs the old engine",
                remediation=remediation, raw={"staged": seen}))
        else:
            rows.append(_row(surface, CURRENT,
                             f"staged {ssot} == SSOT {ssot} ({len(seen)} arch(es))",
                             raw={"staged": seen}))
    return rows
