#!/usr/bin/env python3
"""Doctrine v2 is a QUOTATION of the code that enforces it — prove it still is.

WHY THIS EXISTS. Doctrine v1 was 6,339 lines of prose describing rules that
lived, differently worded, in `tools/cos_judge.py`. Two copies of a rule is one
rule and one rumour, and the rumour is the one people read. v2 quotes the
validator's own strings, so this check is what keeps the quotation honest:

  1. every `RULES THAT BIND` line in cos_judge.py's four batch templates appears
     BYTE-IDENTICALLY in DOCTRINE.md;
  2. the three skill mirrors of DOCTRINE.md are byte-identical to each other;
  3. DOCTRINE.md carries `kernel_version` + `extraction_rules_version` (the run
     manifest refuses a bundle without the first) and defines a CONTIGUOUS
     `E1..En` self-eval list. v6.0 required ZERO E-checks here; that inverted
     in v7.0, because zero made `check_self_eval` structurally ungradeable —
     `read_skill` froze `None` and every night scored "valid but ungradeable".
     Contiguity is the load-bearing half: `check_self_eval` demands a result
     for every id in `range(1, expected + 1)` where `expected` is the COUNT, so
     a list defining E1, E2, E5 freezes 3 and then demands an E3 nothing
     defines — an unpassable night;
  4. SKILL.md (superseded) pins the SAME kernel_version — a WARNING, not a
     finding. `cos_nightly.sh` dies at exit 3 on anything this returns 1 for, so
     until 2026-08-12 a concurrent session bumping the version in a document
     that is marked superseded and BINDS NOTHING took the owner's mail
     automation offline for as many mornings as the mismatch survived. A
     doc-drift check must not have the blast radius of a mailbox guard, so this
     one prints and continues; 1-3 above still fail the night, because there the
     rules handed to the model really are not the rules the validator applies.

    python3 tools/cos_verify_doctrine.py            # 0 clean or warned, 1 drift
    python3 tools/cos_verify_doctrine.py --json
    python3 tools/cos_verify_doctrine.py --selfcheck    # prove it can FAIL

ponytail: no config, no registry — the mirror list and the template names are
the only two facts, and both are one line each.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_verify_doctrine_text import (  # noqa: E402
    anchor_findings, mirror_digest_problem, skill_pin_warnings,
    unquoted_rule_findings, zip_copy_warnings)

REPO = Path(__file__).resolve().parents[1]
JUDGE = REPO / "tools" / "cos_judge.py"
#: batch-2 drain: the batch templates moved out of `cos_judge.py` into the
#: sibling prompts module; the doctrine reads them wherever they live.
PROMPTS = REPO / "tools" / "cos_judge_prompts.py"
MIRRORS = (
    REPO / ".claude" / "skills" / "chief-of-staff",
    REPO / ".agents" / "skills" / "chief-of-staff",
    REPO / "plugins" / "brainiac-extras" / "skills" / "chief-of-staff",
)
#: The CATEGORY template joined on 2026-08-13. It is a batch with binding
#: rules like the other four, and a template outside this tuple is a rule
#: the doctrine is not obliged to quote — which is the "one rule, one
#: rumour" state this whole file exists to prevent.
TEMPLATES = ("CATEGORY_PROMPT", "TRIAGE_PROMPT", "STAGING_PROMPT",
             "HOLD_PROMPT", "DRAFT_PROMPT")

KERNEL_RE = re.compile(r'^\s*kernel_version:\s*["\']([^"\']+)["\']', re.MULTILINE)
EXT_RE = re.compile(r'^\s*extraction_rules_version:\s*["\']([^"\']+)["\']', re.MULTILINE)
ECHECK_RE = re.compile(r"^- \*\*E(\d{1,2})\*\*\s*·", re.MULTILINE)


def templates_src(repo: Path) -> str:
    """The batch templates' source text — `cos_judge.py` + its prompts sibling."""
    return ((repo / "tools" / "cos_judge.py").read_text(encoding="utf-8")
            + "\n" + (repo / "tools" / "cos_judge_prompts.py").read_text(
                encoding="utf-8"))


def rule_blocks(judge_src: str) -> dict[str, str]:
    """The `RULES THAT BIND` body of each batch template, as written."""
    out: dict[str, str] = {}
    for name in TEMPLATES:
        body = judge_src.split(name + ' = """', 1)[1].split('"""', 1)[0]
        m = re.search(
            r"RULES THAT BIND[^\n]*\n(.*?)(?=\n\{vocab\}|\nOWNER TAXONOMY|\nANSWER)",
            body, re.S)
        if not m:
            raise ValueError(f"{name} has no RULES THAT BIND block")
        out[name] = m.group(1).rstrip()
    return out


def audit(repo: Path = REPO) -> dict:
    judge = templates_src(repo)
    mirrors = [repo / p.relative_to(REPO) for p in MIRRORS]
    doctrine_paths = [d / "DOCTRINE.md" for d in mirrors]
    findings: list[str] = []   # FATAL — the night must not run on these
    warnings: list[str] = []   # printed, and the night runs anyway

    missing = [str(p) for p in doctrine_paths if not p.exists()]
    if missing:
        return {"ok": False, "warnings": [],
                "findings": [f"no DOCTRINE.md at {p}" for p in missing]}

    texts = [p.read_text(encoding="utf-8") for p in doctrine_paths]
    digests = [hashlib.sha256(t.encode("utf-8")).hexdigest() for t in texts]
    mirror_problem = mirror_digest_problem(doctrine_paths, digests)
    if mirror_problem is not None:
        findings.append(mirror_problem)

    doctrine = texts[0]
    quoted_findings, quoted = unquoted_rule_findings(judge, doctrine,
                                                     rule_blocks)
    findings.extend(quoted_findings)
    anchor, km, em, echecks = anchor_findings(doctrine, KERNEL_RE, EXT_RE,
                                              ECHECK_RE)
    findings.extend(anchor)
    warnings.extend(zip_copy_warnings(repo, digests[0]))
    warnings.extend(skill_pin_warnings(mirrors[0] / "SKILL.md", km, KERNEL_RE))

    return {"ok": not findings, "findings": findings, "warnings": warnings,
            "doctrine_sha256": digests[0], "mirrors": [str(p) for p in doctrine_paths],
            "doctrine_lines": len(doctrine.splitlines()),
            "rule_lines_quoted": quoted,
            "kernel_version": km.group(1) if km else None,
            "extraction_rules_version": em.group(1) if em else None,
            "echecks_defined": len(echecks)}


def _selfcheck() -> int:
    """Every assertion above, probed with a KNOWN POSITIVE in a scratch repo."""
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        (repo / "tools").mkdir(parents=True)
        shutil.copy2(JUDGE, repo / "tools" / "cos_judge.py")
        shutil.copy2(PROMPTS, repo / "tools" / "cos_judge_prompts.py")
        for m in MIRRORS:
            d = repo / m.relative_to(REPO)
            d.mkdir(parents=True)
            shutil.copy2(m / "DOCTRINE.md", d / "DOCTRINE.md")
            if (m / "SKILL.md").exists():
                shutil.copy2(m / "SKILL.md", d / "SKILL.md")
        first = repo / MIRRORS[0].relative_to(REPO) / "DOCTRINE.md"

        clean = audit(repo)
        assert clean["ok"] and not clean["warnings"], \
            "the scratch copy does not pass clean"
        # THE KNOWN POSITIVE for the exit code itself: a clean tree really is 0,
        # so a `main` that returned 0 unconditionally could not pass the FATAL
        # probes below, and one that returned 1 unconditionally could not pass
        # this line or the WARNING probe.
        assert main([], repo) == 0, "a clean tree does not exit 0"
        good = first.read_text(encoding="utf-8")
        skill = repo / MIRRORS[0].relative_to(REPO) / "SKILL.md"

        def probe(text: str, needle: str) -> None:
            """FATAL: reported as DRIFT, and the nightly's `|| die … 3` fires."""
            first.write_text(text, encoding="utf-8")
            r = audit(repo)
            assert not r["ok"] and any(needle in f for f in r["findings"]), \
                f"probe {needle!r} went UNDETECTED: {r['findings']}"
            assert main([], repo) == 1, f"fatal probe {needle!r} did not exit 1"
            first.write_text(good, encoding="utf-8")

        # 1. a quoted rule line altered by ONE character
        line = next(l for l in rule_blocks(templates_src(REPO))
                    ["TRIAGE_PROMPT"].splitlines() if l.strip())
        probe(good.replace(line, line[:-1] + "X", 1), "NOT in DOCTRINE.md")
        # 2. the mirrors drift apart
        probe(good + "\ndrift\n", "NOT byte-identical")
        # 3. the version anchors go missing / disagree
        probe(good.replace('kernel_version: "', 'kernel_versionX: "', 1),
              "states no `kernel_version`")
        probe(good.replace('extraction_rules_version: "',
                           'extraction_rules_versionX: "', 1),
              "states no `extraction_rules_version`")
        # 4a. the whole self-eval list is deleted — back to v6.0's ungradeable
        #     state, where `check_self_eval` could neither pass nor fail.
        probe("\n".join(l for l in good.splitlines() if not ECHECK_RE.match(l)),
              "defines ZERO")
        # 4b. a stray id opens a GAP. `check_self_eval` scores against the
        #     COUNT, so E1..E10 plus an invented E99 demands an E11 nothing
        #     defines — the night can never pass.
        probe(good + "\n- **E99** · a check nobody numbered contiguously.\n",
              "NOT contiguous from 1")
        # 5. the SUPERSEDED SKILL.md pin drifts — reported, and the night runs.
        #    A concurrent session bumping a version in a document that binds
        #    nothing used to exit 3 here and take the mailbox automation down
        #    with it (review 2026-08-12).
        assert skill.exists(), "no SKILL.md in the scratch copy to drift"
        skill_good = skill.read_text(encoding="utf-8")
        skill.write_text(skill_good.replace('kernel_version: "chief-of-staff v',
                                            'kernel_version: "chief-of-staff w', 1),
                         encoding="utf-8")
        r = audit(repo)
        assert any("should not disagree" in w for w in r["warnings"]), \
            f"the SKILL.md pin drift went UNDETECTED: {r}"
        assert r["ok"] and not r["findings"], \
            f"a superseded doc's version pin is still FATAL: {r['findings']}"
        assert main([], repo) == 0, "a warning-only tree stops the night"
        skill.write_text(skill_good, encoding="utf-8")

        # 6. THE FOURTH COPY — the Cowork zip. Three arms, because the
        #    interesting one is the middle one: absent (silent), stale
        #    (warned), fresh (silent). Without the fresh arm a check that
        #    warned unconditionally would pass the stale arm too.
        import zipfile                                        # noqa: PLC0415
        zp = repo / "dist" / "cowork-skills" / "chief-of-staff.skill"
        zp.parent.mkdir(parents=True, exist_ok=True)
        assert not zp.exists()
        assert not audit(repo)["warnings"], "an ABSENT zip must be silent"
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("chief-of-staff/DOCTRINE.md", good + "\nstale\n")
        assert any("is STALE" in w for w in audit(repo)["warnings"]), \
            "a stale Cowork zip went UNDETECTED — the fourth copy is unguarded"
        assert audit(repo)["ok"], "a stale BUILD ARTIFACT must not stop the night"
        zp.unlink()
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("chief-of-staff/DOCTRINE.md", good)
        assert not audit(repo)["warnings"], \
            "a FRESH zip warns anyway — the check cannot tell them apart"
        zp.unlink()
    print("selfcheck OK — every assertion was proven able to fail")
    return 0


def main(argv: list[str], repo: Path = REPO) -> int:
    if "--selfcheck" in argv:
        return _selfcheck()
    res = audit(repo)
    if "--json" in argv:
        print(json.dumps(res, indent=2))
    else:
        for w in res["warnings"]:
            print(f"WARNING: {w}")
        for f in res["findings"]:
            print(f"DRIFT: {f}")
        if res["ok"]:
            print(f"OK: DOCTRINE.md ({res['doctrine_lines']} lines, "
                  f"{res['kernel_version']}, ext {res['extraction_rules_version']}) "
                  f"quotes all {res['rule_lines_quoted']} enforced rule lines "
                  f"verbatim, is identical across 3 mirrors, and defines "
                  f"{res['echecks_defined']} E-checks")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
