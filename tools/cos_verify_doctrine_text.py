"""Text-comparison sub-checks of the doctrine auditor.

One function per comparison ``cos_verify_doctrine.audit`` makes against
DOCTRINE.md or its copies: mirror digests, quoted rule lines, version
anchors, the Cowork zip's fourth copy, the superseded SKILL.md pin.
``audit`` keeps its name and module (the selfcheck, the tests and the
nightly's exit-3 gate name it there); every parent value it needs
(``rule_blocks``, the compiled regexes) arrives as a parameter — this module
never imports ``cos_verify_doctrine``.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Callable, Pattern


def mirror_digest_problem(doctrine_paths: list[Path],
                          digests: list[str]) -> str | None:
    if len(set(digests)) != 1:
        return (
            "the three DOCTRINE.md mirrors are NOT byte-identical: "
            + "; ".join(f"{p.parent.parent.parent.name}/…={d[:12]}…"
                        for p, d in zip(doctrine_paths, digests)))
    return None


def unquoted_rule_findings(
        judge_src: str, doctrine: str,
        rule_blocks: Callable[[str], dict[str, str]]) -> tuple[list[str], int]:
    """(findings, rule_lines_quoted) for the `RULES THAT BIND` bodies."""
    findings: list[str] = []
    quoted = 0
    for name, block in rule_blocks(judge_src).items():
        for line in block.splitlines():
            if not line.strip():
                continue
            quoted += 1
            if line not in doctrine:
                findings.append(
                    f"{name}: this rule line is in the batch templates and NOT in "
                    f"DOCTRINE.md — {line.strip()[:90]!r}")
    return findings, quoted


def anchor_findings(
        doctrine: str, kernel_re: Pattern[str], ext_re: Pattern[str],
        echeck_re: Pattern[str]) -> tuple[list[str], re.Match | None,
                                          re.Match | None, list[int]]:
    """(findings, kernel match, extraction match, echeck ids) for the
    version anchors and the contiguity of the E-check list."""
    findings: list[str] = []
    km, em = kernel_re.search(doctrine), ext_re.search(doctrine)
    if not km:
        findings.append("DOCTRINE.md states no `kernel_version` — "
                        "`brain cos-run-begin --skill` refuses such a bundle")
    if not em:
        findings.append("DOCTRINE.md states no `extraction_rules_version`")
    echecks = sorted({int(n) for n in echeck_re.findall(doctrine)})
    if not echecks:
        findings.append(
            "DOCTRINE.md defines ZERO `- **EN** ·` self-eval checks — "
            "`cos_deploy.read_skill` then freezes `None` as the run's expected "
            "count, `expected_check_count` can derive nothing, and "
            "`check_self_eval` can only ever score DEGRADED. A control that "
            "cannot pass is not a control")
    elif echecks != list(range(1, len(echecks) + 1)):
        findings.append(
            f"DOCTRINE.md's E-check ids are NOT contiguous from 1: {echecks}. "
            f"`check_self_eval` demands a result for every id in 1..{len(echecks)} "
            "(the COUNT is what the manifest freezes), so a gap makes every "
            "night unpassable")
    return findings, km, em, echecks


def zip_copy_warnings(repo: Path, mirror_digest: str) -> list[str]:
    """THE FOURTH COPY, and it is a ZIP (review 2026-08-15). `dist/cowork-skills/
    chief-of-staff.skill` is what a COWORK session uploads and reads, it
    carries its own `chief-of-staff/DOCTRINE.md` member, and this file bound
    exactly three paths — so the round that corrected the doctrine left a
    Cowork session loading the UNCORRECTED text, with no gate anywhere able to
    see it. Measured then: the member was 62,229 bytes at a pre-rework digest
    while the mirrors were 63,983.

    A WARNING, NOT A FINDING, and deliberately: the zip is a BUILD ARTIFACT
    (gitignored, rebuilt by `tools/package_clients.py`), so a stale one means
    "rebuild the zips", not "the rules handed to the model are not the rules
    the validator applies". `cos_nightly.sh` dies at exit 3 on anything this
    returns 1 for, and a doc-build drift must not have the blast radius of a
    mailbox guard — the same call SKILL.md's pin already gets, for the same
    reason. An ABSENT zip is silent: not every checkout has built one.
    """
    warnings: list[str] = []
    zip_path = repo / "dist" / "cowork-skills" / "chief-of-staff.skill"
    if zip_path.exists():
        import zipfile                                        # noqa: PLC0415
        try:
            with zipfile.ZipFile(zip_path) as z:
                members = [n for n in z.namelist() if n.endswith("DOCTRINE.md")]
                if not members:
                    warnings.append(
                        f"{zip_path.name} carries no DOCTRINE.md member — a "
                        "Cowork session installing it gets no doctrine at all")
                for name in members:
                    zdig = hashlib.sha256(z.read(name)).hexdigest()
                    if zdig != mirror_digest:
                        warnings.append(
                            f"{zip_path.name}!{name} is STALE: {zdig[:12]}… vs "
                            f"the mirrors' {mirror_digest[:12]}…. A Cowork session "
                            "loads this copy, and no other gate can see it — "
                            "rebuild with `python3 tools/package_clients.py`")
        except (OSError, zipfile.BadZipFile) as exc:
            warnings.append(f"{zip_path.name} could not be read ({exc})")
    return warnings


def skill_pin_warnings(skill_path: Path, km: re.Match | None,
                       kernel_re: Pattern[str]) -> list[str]:
    warnings: list[str] = []
    if skill_path.exists():
        sk = kernel_re.search(skill_path.read_text(encoding="utf-8"))
        if km and (not sk or sk.group(1) != km.group(1)):
            warnings.append(
                f"SKILL.md pins kernel_version "
                f"{sk.group(1) if sk else None!r} but DOCTRINE.md is "
                f"{km.group(1)!r} — the calibration pin reads SKILL.md and the "
                "run manifest reads DOCTRINE.md, so they should not disagree. "
                "SKILL.md is superseded and binds nothing, so this does NOT "
                "stop the night")
    return warnings
