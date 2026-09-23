"""Shared primitives of `package_clients.py` — repo roots, the ValidationError type, frontmatter/JSON readers (batch-2 drain).

Moved verbatim out of `tools/package_clients.py`; the parent re-imports every
name so `package_clients.ValidationError`, `parse_skill_frontmatter` and the
path constants keep their original module path (`tests/test_windows_portability`
and `tests/test_release` import them there).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ADR 0013: the public export leaves the COS (the private email assistant) out,
# `src/brain/cos/` included. A tree without it packages no COS skill, tool
# mirror or asset; this private tree keeps every one of them, unchanged.
COS_PRESENT = (REPO_ROOT / "src" / "brain" / "cos" / "__init__.py").is_file()


def without_cos(names: list[str], shape: str = "{}") -> list[str]:
    """``names`` unchanged where the COS is present, else minus every name whose
    path (``shape`` filled with it) is a COS path per the exporter's list."""
    if COS_PRESENT:
        return names
    from export_cleanroom import is_cos_path  # a sibling in tools/
    return [n for n in names if not is_cos_path(shape.format(n))]

PLUGINS_DIR = REPO_ROOT / "plugins"
DIST_DIR = REPO_ROOT / "dist"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"

# Accept CRLF as well as LF. `.gitattributes` now normalizes the checkout to LF,
# but a file can still reach this parser with CRLF -- an older clone, an archive,
# or an edit saved in a Windows editor -- and the LF-only anchor turned that into
# a bare "no YAML frontmatter block found" on a file whose frontmatter is right
# there. Belt to .gitattributes' braces; one character each way.
FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)


class ValidationError(Exception):
    pass


def _log(msg: str) -> None:
    print(msg)


# ---------------------------------------------------------------------------
# Frontmatter parsing (stdlib-only mini-parser, same posture as tools/validate.py)
# ---------------------------------------------------------------------------


def parse_skill_frontmatter(skill_md_path: Path) -> dict:
    text = skill_md_path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise ValidationError(f"{skill_md_path}: no YAML frontmatter block found")
    fm_text = m.group(1)
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(fm_text)
    except ImportError:
        data = _mini_yaml_parse(fm_text)
    if not isinstance(data, dict):
        raise ValidationError(f"{skill_md_path}: frontmatter did not parse to a mapping")
    return data


def _mini_yaml_parse(fm_text: str) -> dict:
    """Minimal top-level ``key: value`` parser — good enough for name/description."""
    out: dict = {}
    key = None
    for line in fm_text.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if line.startswith((" ", "\t")):
            continue  # nested block — not needed for name/description checks
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if val.startswith('"') and val.endswith('"') and len(val) >= 2:
                val = val[1:-1]
            out[key] = val
    return out


def validate_skill_md(skill_md_path: Path) -> None:
    fm = parse_skill_frontmatter(skill_md_path)
    for required in ("name", "description"):
        if not fm.get(required):
            raise ValidationError(f"{skill_md_path}: frontmatter missing '{required}'")


def validate_json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{path}: invalid JSON — {exc}") from exc
