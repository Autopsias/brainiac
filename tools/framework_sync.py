#!/usr/bin/env python3
"""S12 (HYG-02) — framework-sync drift audit: catch the three copies of every
skill drifting apart (the canonical ``.claude/skills/`` tree, the Codex mirror
at ``.agents/skills/``, and the ``plugins/*/skills/`` marketplace copies),
plus verify ``CLAUDE.md`` still imports ``@AGENTS.md`` on its own line.

Reuses ``tools/package_clients.py``'s KERNEL_SKILLS/EXTRAS_SKILLS/
BRAINIAC_SKILLS mapping and directory constants — the mapping is not
duplicated here.

**SKILL_VERSION trap:** ``package_clients.stamp_skill_version`` appends a
generated ``<!-- SKILL_VERSION: ... -->`` line to DISTRIBUTED copies of
brainiac-manager skills only (never the canonical source, never kernel/extras
copies). Comparing raw bytes would make every synced brainiac-* mirror
false-positive as "drifted" on that line alone. Every hash in this module is
computed on STAMP-NORMALIZED content (the marker stripped first).

Usage:
    python3 tools/framework_sync.py            # audit repo, exit 0=clean 1=drift
    python3 tools/framework_sync.py --json     # machine-readable report

Folded into the Monday ``health`` branch of ``brain maintain``
(``src/brain/core.py`` / ADR-0003 Ruling 5) as a health FINDING — it never
auto-fixes; the remedy is always "re-run tools/package_clients.py".
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from package_clients import (  # noqa: E402  (reuse the mapping, don't duplicate it)
    ALL_SKILLS, BRAINIAC_SKILLS, KERNEL_SKILLS, EXTRAS_SKILLS,
    CLAUDE_SKILLS_DIR, AGENTS_SKILLS_DIR, PLUGINS_DIR,
)

# Mirrors package_clients.stamp_skill_version's marker pattern exactly — keep
# the two in sync if that format ever changes.
_SKILL_VERSION_RE = re.compile(r"^<!-- SKILL_VERSION:.*?-->\n?", re.MULTILINE)


def _normalized_bytes(path: Path) -> bytes:
    text = path.read_text(encoding="utf-8")
    text = _SKILL_VERSION_RE.sub("", text)
    # stamp_skill_version inserts an extra blank line ahead of the marker
    # (``text + "\n" + marker``); once the marker itself is stripped that
    # blank line is meaningless trailing whitespace, not real drift.
    text = text.rstrip("\n") + "\n"
    return text.encode("utf-8")


def _hash_tree(root: Path) -> dict[str, str]:
    """{relative-posix-path: sha256 of stamp-normalized content} for every
    file under ``root``. Binary-safe fallback not needed — skill trees are
    text (SKILL.md + markdown resources)."""
    out: dict[str, str] = {}
    for f in sorted(root.rglob("*")):
        if f.is_file():
            rel = f.relative_to(root).as_posix()
            try:
                out[rel] = hashlib.sha256(_normalized_bytes(f)).hexdigest()
            except UnicodeDecodeError:
                out[rel] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


def _skill_targets(
    claude_skills_dir: Path, agents_skills_dir: Path, plugins_dir: Path,
    kernel_skills: list[str], extras_skills: list[str], brainiac_skills: list[str],
) -> dict[str, list[tuple[str, Path]]]:
    """skill name -> [(mirror label, mirror dir), ...] to compare against the
    canonical ``claude_skills_dir/<name>/``."""
    targets: dict[str, list[tuple[str, Path]]] = {}
    for name in kernel_skills + extras_skills:
        mirrors = [(".agents/skills", agents_skills_dir / name)]
        plugin_name = "profile-a-kernel" if name in kernel_skills else "profile-a-extras"
        mirrors.append((f"plugins/{plugin_name}/skills", plugins_dir / plugin_name / "skills" / name))
        targets[name] = mirrors
    for name in brainiac_skills:
        targets[name] = [
            (".agents/skills", agents_skills_dir / name),
            ("plugins/brainiac-manager/skills", plugins_dir / "brainiac-manager" / "skills" / name),
        ]
    return targets


def check_skill_drift(
    claude_skills_dir: Path = CLAUDE_SKILLS_DIR,
    agents_skills_dir: Path = AGENTS_SKILLS_DIR,
    plugins_dir: Path = PLUGINS_DIR,
    kernel_skills: list[str] | None = None,
    extras_skills: list[str] | None = None,
    brainiac_skills: list[str] | None = None,
) -> list[dict]:
    """Compare canonical vs every mirror, file by file, stamp-normalized.
    Returns a list of per-file drift reports; empty list = clean."""
    kernel_skills = KERNEL_SKILLS if kernel_skills is None else kernel_skills
    extras_skills = EXTRAS_SKILLS if extras_skills is None else extras_skills
    brainiac_skills = BRAINIAC_SKILLS if brainiac_skills is None else brainiac_skills

    drift: list[dict] = []
    targets = _skill_targets(
        claude_skills_dir, agents_skills_dir, plugins_dir,
        kernel_skills, extras_skills, brainiac_skills,
    )
    for name, mirrors in targets.items():
        canonical_dir = claude_skills_dir / name
        if not canonical_dir.is_dir():
            drift.append({"skill": name, "mirror": None, "path": None,
                          "reason": f"canonical skill dir missing: {canonical_dir}"})
            continue
        canonical_hashes = _hash_tree(canonical_dir)
        for label, mirror_dir in mirrors:
            if not mirror_dir.is_dir():
                drift.append({"skill": name, "mirror": label, "path": None,
                              "reason": f"missing mirror dir: {mirror_dir}"})
                continue
            mirror_hashes = _hash_tree(mirror_dir)
            for rel in sorted(set(canonical_hashes) | set(mirror_hashes)):
                c, m = canonical_hashes.get(rel), mirror_hashes.get(rel)
                if c != m:
                    reason = ("missing in mirror" if m is None else
                              "missing in canonical" if c is None else "content differs")
                    drift.append({"skill": name, "mirror": label, "path": rel, "reason": reason})
    return drift


def check_claude_md_import(claude_md_path: Path = REPO_ROOT / "CLAUDE.md") -> dict:
    """CLAUDE.md's line-1 directive (AGENTS.md is canonical) must still
    ``@AGENTS.md``-import verbatim on its own line."""
    if not claude_md_path.is_file():
        return {"ok": False, "reason": f"missing {claude_md_path}"}
    text = claude_md_path.read_text(encoding="utf-8")
    if re.search(r"(?m)^@AGENTS\.md\s*$", text):
        return {"ok": True, "reason": None}
    return {"ok": False, "reason": "CLAUDE.md no longer imports @AGENTS.md on its own line"}


def audit(**kwargs) -> dict:
    """kwargs forward selectively to check_skill_drift / check_claude_md_import
    by name (claude_md_path vs the skill-drift params)."""
    claude_md_path = kwargs.pop("claude_md_path", REPO_ROOT / "CLAUDE.md")
    drift = check_skill_drift(**kwargs)
    claude_md = check_claude_md_import(claude_md_path)
    return {
        "clean": not drift and claude_md["ok"],
        "skill_drift": drift,
        "claude_md_import": claude_md,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    args = parser.parse_args()

    report = audit()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for d in report["skill_drift"]:
            print(f"DRIFT  {d['skill']} [{d['mirror']}] {d['path']}: {d['reason']}")
        if not report["claude_md_import"]["ok"]:
            print(f"DRIFT  CLAUDE.md: {report['claude_md_import']['reason']}")
        if report["clean"]:
            print("OK: canonical .claude/skills/ matches .agents/skills/ + plugins/ mirrors; "
                  "CLAUDE.md imports @AGENTS.md.")
        else:
            print(f"\n{len(report['skill_drift'])} drifted file(s)"
                  + ("" if report["claude_md_import"]["ok"] else " + CLAUDE.md import broken"))
    return 0 if report["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
