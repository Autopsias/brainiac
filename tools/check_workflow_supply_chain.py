#!/usr/bin/env python3
"""Fail when a PRIVILEGED workflow job installs an unpinned, network-fetched
package.

Why this exists (2026-08-07): the Codex cloud review found `npm install -g
npm@latest` running in `.github/workflows/npm-publish.yml`, a job that holds
`id-token: write` and can therefore mint an npm-trusted publishing token. Any
install-time lifecycle code in that unpinned fetch runs with that privilege.
Semgrep's `p/github-actions` pack does NOT cover this -- measured the same day,
it returns zero findings on that exact file -- so buying the pack alone would
have left the one class that actually bit us undetected.

The check is deliberately narrow. It is not "no unpinned installs anywhere":
a `contents: read` job that pip-installs a linter is a different risk from a
job that can publish. Privilege is what turns an unpinned fetch into a
supply-chain hole, so privilege is what scopes the rule.

A job counts as PRIVILEGED when its effective permissions (job-level if
present, else workflow-level) grant any `write`, are `write-all`, or are
ABSENT -- absent means the repository default applies, which can be write.
Fail closed: an unreadable or unparseable workflow is an error, never a pass.

Run `--self-test` to prove the checker can still fail. A check that returns
"clean" because its input was empty is worse than no check.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # never degrade to a vacuous pass
    sys.exit("check_workflow_supply_chain: PyYAML is required (pip install pyyaml)")

# Each entry is (regex, plain-language reason). Patterns match one `run:` line.
UNPINNED_INSTALL = [
    (re.compile(r"\bnpm\s+(?:install|i|add)\b[^\n]*@latest\b"),
     "npm install of an @latest package"),
    (re.compile(r"\bnpm\s+(?:install|i|add)\s+(?:-g\s+|--global\s+)?(?![-@]|\S+@\d)\S+"),
     "npm install of a package with no pinned version"),
    (re.compile(r"\bcurl\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba)?sh\b"),
     "curl piped straight into a shell"),
    (re.compile(r"\bwget\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba)?sh\b"),
     "wget piped straight into a shell"),
    (re.compile(r"\bpip\s+install\b(?![^\n]*(?:==|--require-hashes|\s-r\s|\s-e\s|\s\.))"),
     "pip install with no pinned version"),
    (re.compile(r"\buv\s+tool\s+install\b(?![^\n]*==)"),
     "uv tool install with no pinned version"),
]

WORKFLOW_GLOBS = ("*.yml", "*.yaml")


def _is_privileged(perms: object) -> bool:
    """Effective permissions that let a job write something outside itself."""
    if perms is None:
        return True  # repository default applies; assume it can write
    if isinstance(perms, str):
        return perms != "read-all"
    if isinstance(perms, dict):
        return any(str(v).lower() == "write" for v in perms.values())
    return True


def _run_steps(job: dict) -> list[tuple[str, str]]:
    """(step name, run script) for every step that runs a shell command."""
    out = []
    for step in job.get("steps") or []:
        if isinstance(step, dict) and isinstance(step.get("run"), str):
            out.append((str(step.get("name") or step.get("id") or "<unnamed>"),
                        step["run"]))
    return out


def scan_text(text: str, label: str) -> list[str]:
    """Findings for one workflow document. Raises on unparseable YAML."""
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        raise ValueError(f"{label}: not a YAML mapping")
    top_perms = doc.get("permissions")
    findings = []
    for job_name, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        perms = job["permissions"] if "permissions" in job else top_perms
        if not _is_privileged(perms):
            continue
        for step_name, script in _run_steps(job):
            for line_no, line in enumerate(script.splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                for pattern, reason in UNPINNED_INSTALL:
                    if pattern.search(line):
                        findings.append(
                            f"{label}: job '{job_name}' step '{step_name}' "
                            f"(run line {line_no}) -- {reason}: {line.strip()}")
                        break
    return findings


def scan_repo(root: Path) -> list[str]:
    wf_dir = root / ".github" / "workflows"
    if not wf_dir.is_dir():
        return []
    findings = []
    for glob in WORKFLOW_GLOBS:
        for path in sorted(wf_dir.glob(glob)):
            rel = path.relative_to(root).as_posix()
            findings.extend(scan_text(path.read_text(encoding="utf-8"), rel))
    return findings


# The known positive. It is the shape of the real 2026-08-07 finding, kept
# here so the checker proves it can still fail before it reports a clean tree.
_SELF_TEST_POSITIVE = """
name: probe
on: [push]
permissions:
  contents: read
  id-token: write
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - name: Ensure a new enough npm
        run: |
          npm install -g npm@latest
      - run: npm publish --access public
"""

_SELF_TEST_NEGATIVE = """
name: probe-clean
on: [push]
permissions:
  contents: read
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: pip install pip-audit
"""


def self_test(*, quiet: bool = False) -> int:
    """A read-only job installing an unpinned linter is NOT the risk; a
    privileged job installing @latest is. Assert both directions."""
    hits = scan_text(_SELF_TEST_POSITIVE, "<self-test-positive>")
    if not hits:
        print("SELF-TEST FAILED: the known positive was not flagged", file=sys.stderr)
        return 1
    misses = scan_text(_SELF_TEST_NEGATIVE, "<self-test-negative>")
    if misses:
        print(f"SELF-TEST FAILED: the known negative was flagged: {misses}",
              file=sys.stderr)
        return 1
    if not quiet:
        print(f"self-test OK: known positive flagged ({len(hits)} finding(s)), "
              "known negative clean")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".", help="repository root to scan")
    ap.add_argument("--self-test", action="store_true",
                    help="prove the checker still fails on a known positive")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    if self_test(quiet=True) != 0:  # never report clean from a broken checker
        return 2
    findings = scan_repo(Path(args.root).resolve())
    if findings:
        print("\nUnpinned network install inside a privileged workflow job:\n",
              file=sys.stderr)
        for f in findings:
            print(f"  {f}", file=sys.stderr)
        print("\nPin the version, or drop the write permission from that job.\n",
              file=sys.stderr)
        return 1
    print("workflow supply-chain check: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
