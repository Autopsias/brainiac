"""Public copies of the shared files that keep COS in this private tree (ADR 0013 §5).

The COS (Chief of Staff, the private email assistant) and the second brain share
a few files: the routines manifest, `pyproject.toml`, `.pre-commit-config.yaml`
and the Cowork verb survey. The private copy must keep its COS lines, because
COS keeps running here. So the export writes a COS-free copy of each one instead
of copying it byte for byte. `tools/export_check_no_cos.py` then proves the
result names no COS path.

Every rewrite is a plain function of the file's text. The verb survey is the one
exception: it is REGENERATED from the COS-free export by the survey's own
normative derivation (`tests/test_cowork_skill_verbs.py`), so its numbers are
measured on the public tree, never edited by hand.
That derivation lives under `tests/`, which never ships, so an export run from a
tree without it stops and names the missing file rather than ship the private survey.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

SURVEY = "docs/operations/cowork-skill-verb-survey.json"
SURVEY_DERIVATION = "tests/test_cowork_skill_verbs.py"
COS_ROUTINE = "chief-of-staff-nightly"
# A hook's body is every line indented DEEPER than its `- id:`, so the match
# stops at a sibling hook and at the next `- repo:`, whichever comes first.
_HOOK = re.compile(r"(?m)^(?:[ \t]*#.*\n)*(?P<ind>[ \t]*)- id: [\w-]+\n(?:(?P=ind)[ \t]+\S.*\n)*(?:[ \t]*\n)?")
_COS_TOOL_IN_COMMENT = re.compile(r"`?tools/cos_[\w.]+`?")


def drop_cos_routine(text: str, _is_cos: Callable[[str], bool]) -> str:
    """The umbrella's fold list names the COS nightly; the public umbrella has none."""
    return re.sub(rf'(?m)^[ \t]*"{COS_ROUTINE}",\n', "", text)


def drop_cos_ruff_ignores(text: str, is_cos: Callable[[str], bool]) -> str:
    """Drop every per-file-ignores entry keyed on a COS path, with the comment
    paragraph right above it, and stop comments naming a `tools/cos_*` file."""
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        key = re.match(r'\s*"([^"]+)"\s*=', line)
        if key and is_cos(key.group(1).rstrip("*")):
            while out and out[-1].lstrip().startswith("#"):
                out.pop()
            continue
        if line.lstrip().startswith("#"):
            line = _COS_TOOL_IN_COMMENT.sub("a private tool", line)
        out.append(line)
    return "".join(out)


def drop_cos_hooks(text: str, is_cos: Callable[[str], bool]) -> str:
    """Drop every pre-commit hook (with its comment paragraph) whose entry runs a COS file."""
    def keep(m: re.Match) -> str:
        entry = re.search(r"(?m)^[ \t]+entry:(.*)$", m.group(0))
        cos = entry and any(is_cos(tok.strip("'\"")) for tok in entry.group(1).split())
        return "" if cos else m.group(0)
    return _HOOK.sub(keep, text)


TEXT_REWRITES: dict[str, Callable[[str, Callable[[str], bool]], str]] = {
    "routines/manifest.json": drop_cos_routine,
    "src/brain/_assets/routines/manifest.json": drop_cos_routine,
    "pyproject.toml": drop_cos_ruff_ignores,
    ".pre-commit-config.yaml": drop_cos_hooks,
}

# Runs in a child with the EXPORT's src/ first on the path, so the parser, the
# broker tools and the packager's skill list are the COS-free ones.
_REGEN = r"""
import importlib.util, json, sys
from pathlib import Path
out, derivation, survey_path = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
spec = importlib.util.spec_from_file_location("_verb_survey", derivation)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.REPO, m.CANONICAL_SKILLS_DIR = out, out / ".claude" / "skills"
from brain.cli import VM_ALLOWED
s = json.loads(survey_path.read_text(encoding="utf-8"))
d = m.derive()
s.update(d)
s["bundles"] = m.derive_bundles()
verbs = d["verbs"]
s["present_on_broker"] = sorted(v["verb"] for v in verbs if v["on_broker"])
s["missing_from_broker"] = sorted(v["verb"] for v in verbs if not v["on_broker"])
s["disposition_counts"] = {k: sum(1 for v in verbs if v["disposition"] == k)
                           for k in sorted({v["disposition"] for v in verbs})}
s["vm_allowed_dispositions"] = {v: m._disposition(v) for v in sorted(VM_ALLOWED)}
s["vm_allowed_verbs_no_bundle_uses"] = sorted(set(VM_ALLOWED) - {v["verb"] for v in verbs})
dirs = m._skill_dirs()
s["direct_vault_note_reads"] = sorted(
    f"{p.name}:{' '.join(h.split())}" for p in dirs for f in m._scanned_files(p)
    for h in m.direct_reads(f.read_text(encoding="utf-8", errors="replace")))
s["authority"]["cli_subcommand_count"] = d["cli_parser_choices"]
old_raw, old, _b, _f = m.scan(dirs, m._SUPERSEDED_INVOCATION)
new_raw, new, _b, _f = m.scan(dirs)
s["superseded_pattern_reconciliation"].update({
    "superseded_raw": old_raw, "superseded_distinct": len(old),
    "current_raw": new_raw, "current_distinct": len(new), "net": new_raw - old_raw,
    "delta": {t: [old.get(t, 0), new.get(t, 0)] for t in sorted(set(old) | set(new))
              if old.get(t, 0) != new.get(t, 0)}})
s["public_copy"] = ("Regenerated at export time by tools/export_public_copies.py from the "
                    "COS-free tree: every derived column above is measured on the public "
                    "skills and CLI. The dated notes keep their original wording.")
survey_path.write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
"""


def regenerate_survey(repo_root: Path, output_dir: Path) -> None:
    derivation = repo_root / SURVEY_DERIVATION
    if not derivation.is_file():
        raise SystemExit(f"FAIL: cannot regenerate {SURVEY} for the export: {derivation} is missing")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BRAIN", "PYTHON"))}
    env.update({"PYTHONPATH": str(output_dir / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
    p = subprocess.run([sys.executable, "-c", _REGEN, str(output_dir), str(derivation),
                        str(output_dir / SURVEY)], cwd=output_dir, env=env,
                       capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        raise SystemExit(f"FAIL: regenerating {SURVEY} for the export failed:\n"
                         f"{(p.stderr or p.stdout).strip()[-1500:]}")


def write_public_copies(repo_root: Path, output_dir: Path, exported: set[str],
                        is_cos: Callable[[str], bool]) -> list[str]:
    """Rewrite each shared file the export carries; returns the paths rewritten."""
    done = []
    for rel, rewrite in TEXT_REWRITES.items():
        if rel in exported:
            path = output_dir / rel
            path.write_text(rewrite(path.read_text(encoding="utf-8"), is_cos), encoding="utf-8")
            done.append(rel)
    if SURVEY in exported:
        regenerate_survey(repo_root, output_dir)
        done.append(SURVEY)
    return done
