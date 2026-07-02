#!/usr/bin/env python3
"""S08 (SKL-02/03/04) — package the kernel skills for three clients.

Canonical skill content lives at ``.claude/skills/<name>/SKILL.md`` (Claude
Code auto-load). This script is the single source of truth for turning that
canonical copy into the other two client layouts, plus building the Cowork
upload ZIPs, so future skill edits stay in sync by re-running this script
instead of hand-editing three places:

1. **Codex** — mirrors every kernel+extras skill into ``.agents/skills/<name>/``
   (Codex's native repo-root skill scan location; no config.toml entry needed
   for auto-load — see ``docs/harness-wiring.md`` / Codex Agent Skills docs).
2. **Claude Code marketplace** — copies the KERNEL split into
   ``plugins/profile-a-kernel/skills/`` and the EXTRAS split into
   ``plugins/profile-a-extras/skills/`` (the two plugins listed in
   ``.claude-plugin/marketplace.json``).
3. **Cowork** — zips each of the 8 skills individually into
   ``dist/cowork-skills/<name>.skill`` (a zip with ``<name>/SKILL.md`` at its
   root, ready for the Cowork "Save skill" upload flow).

It also validates every artifact it produces or touches:
- every ``SKILL.md`` frontmatter parses and carries ``name`` + ``description``
- ``.claude-plugin/marketplace.json`` and every ``plugin.json`` parse as JSON
  and carry ``name`` (+ ``version`` for every plugin.json)
- ``.codex/config.toml`` parses as TOML
- ``.claude/settings.json`` parses as JSON
- every produced ``dist/cowork-skills/*.skill`` zip re-opens and its inner
  SKILL.md re-parses

Usage:
    python3 tools/package_clients.py                 # sync + build + validate
    python3 tools/package_clients.py --validate-only  # validate what's on disk
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The split: KERNEL = always-useful daily skills. EXTRAS = optional
# maintenance/admin skills, installed separately ("one command away").
KERNEL_SKILLS = [
    "kb-curator",
    "promote",
    "vault-ingestion",
    "vault-eval",
    "save-conversation",
]
EXTRAS_SKILLS = [
    "curation",
    "improve",
    "task-registrar",
]
ALL_SKILLS = KERNEL_SKILLS + EXTRAS_SKILLS

CLAUDE_SKILLS_DIR = REPO_ROOT / ".claude" / "skills"
AGENTS_SKILLS_DIR = REPO_ROOT / ".agents" / "skills"
PLUGINS_DIR = REPO_ROOT / "plugins"
COWORK_DIST_DIR = REPO_ROOT / "dist" / "cowork-skills"

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


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


# ---------------------------------------------------------------------------
# 1. Codex — mirror .claude/skills/<name>/ -> .agents/skills/<name>/
# ---------------------------------------------------------------------------


def sync_agents_skills() -> list[Path]:
    written: list[Path] = []
    AGENTS_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    for name in ALL_SKILLS:
        src_dir = CLAUDE_SKILLS_DIR / name
        dst_dir = AGENTS_SKILLS_DIR / name
        if not src_dir.is_dir():
            raise ValidationError(f"canonical skill dir missing: {src_dir}")
        if dst_dir.exists():
            shutil.rmtree(dst_dir)
        shutil.copytree(src_dir, dst_dir)
        written.append(dst_dir / "SKILL.md")
    return written


# ---------------------------------------------------------------------------
# 2. Claude Code marketplace plugins
# ---------------------------------------------------------------------------


def sync_plugin_skills() -> list[Path]:
    written: list[Path] = []
    mapping = {
        "profile-a-kernel": KERNEL_SKILLS,
        "profile-a-extras": EXTRAS_SKILLS,
    }
    for plugin_name, skills in mapping.items():
        plugin_skills_dir = PLUGINS_DIR / plugin_name / "skills"
        plugin_skills_dir.mkdir(parents=True, exist_ok=True)
        for name in skills:
            src_dir = CLAUDE_SKILLS_DIR / name
            dst_dir = plugin_skills_dir / name
            if dst_dir.exists():
                shutil.rmtree(dst_dir)
            shutil.copytree(src_dir, dst_dir)
            written.append(dst_dir / "SKILL.md")
    return written


# ---------------------------------------------------------------------------
# 3. Cowork — one ZIP per skill
# ---------------------------------------------------------------------------


def build_cowork_zips() -> list[Path]:
    COWORK_DIST_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in ALL_SKILLS:
        src_dir = CLAUDE_SKILLS_DIR / name
        zip_path = COWORK_DIST_DIR / f"{name}.skill"
        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(src_dir.rglob("*")):
                if f.is_file():
                    arcname = f"{name}/{f.relative_to(src_dir).as_posix()}"
                    zf.write(f, arcname)
        written.append(zip_path)
    return written


def validate_cowork_zip(zip_path: Path) -> None:
    name = zip_path.stem
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad is not None:
            raise ValidationError(f"{zip_path}: corrupt member {bad}")
        inner = f"{name}/SKILL.md"
        if inner not in zf.namelist():
            raise ValidationError(f"{zip_path}: missing {inner}")
        fm_text = zf.read(inner).decode("utf-8")
    m = FRONTMATTER_RE.match(fm_text)
    if not m:
        raise ValidationError(f"{zip_path}: inner SKILL.md has no frontmatter")
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(m.group(1))
    except ImportError:
        data = _mini_yaml_parse(m.group(1))
    if not data.get("name") or not data.get("description"):
        raise ValidationError(f"{zip_path}: inner SKILL.md missing name/description")


# ---------------------------------------------------------------------------
# Validation of the static marketplace/plugin/config artifacts
# ---------------------------------------------------------------------------


def validate_json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{path}: invalid JSON — {exc}") from exc


def validate_marketplace() -> None:
    mp_path = REPO_ROOT / ".claude-plugin" / "marketplace.json"
    if not mp_path.exists():
        raise ValidationError(f"missing {mp_path}")
    data = validate_json_file(mp_path)
    if not data.get("name"):
        raise ValidationError(f"{mp_path}: missing top-level 'name'")
    if not data.get("owner", {}).get("name"):
        raise ValidationError(f"{mp_path}: missing owner.name")
    plugins = data.get("plugins") or []
    if not plugins:
        raise ValidationError(f"{mp_path}: 'plugins' array is empty")
    seen_names = set()
    for entry in plugins:
        pname = entry.get("name")
        if not pname:
            raise ValidationError(f"{mp_path}: a plugin entry is missing 'name'")
        if pname in seen_names:
            raise ValidationError(f"{mp_path}: duplicate plugin name '{pname}'")
        seen_names.add(pname)
        if not entry.get("source"):
            raise ValidationError(f"{mp_path}: plugin '{pname}' missing 'source'")
        # Every plugin.json must ALSO carry name + version (source of truth).
        plugin_json_path = PLUGINS_DIR / pname / ".claude-plugin" / "plugin.json"
        if not plugin_json_path.exists():
            raise ValidationError(f"missing {plugin_json_path} for marketplace entry '{pname}'")
        pdata = validate_json_file(plugin_json_path)
        if pdata.get("name") != pname:
            raise ValidationError(
                f"{plugin_json_path}: name '{pdata.get('name')}' != marketplace entry '{pname}'"
            )
        if not pdata.get("version"):
            raise ValidationError(f"{plugin_json_path}: missing 'version'")


def validate_codex_config() -> None:
    cfg_path = REPO_ROOT / ".codex" / "config.toml"
    if not cfg_path.exists():
        raise ValidationError(f"missing {cfg_path}")
    try:
        import tomllib

        with cfg_path.open("rb") as fh:
            tomllib.load(fh)
    except ModuleNotFoundError:
        _log(f"  (tomllib unavailable on this interpreter — skipping strict TOML parse of {cfg_path})")


def validate_claude_settings() -> None:
    settings_path = REPO_ROOT / ".claude" / "settings.json"
    if not settings_path.exists():
        raise ValidationError(f"missing {settings_path}")
    data = validate_json_file(settings_path)
    known = data.get("extraKnownMarketplaces") or {}
    if not known:
        raise ValidationError(f"{settings_path}: extraKnownMarketplaces is empty")


def validate_all_skill_sources() -> None:
    for name in ALL_SKILLS:
        validate_skill_md(CLAUDE_SKILLS_DIR / name / "SKILL.md")
        validate_skill_md(AGENTS_SKILLS_DIR / name / "SKILL.md")
    for plugin_name, skills in (
        ("profile-a-kernel", KERNEL_SKILLS),
        ("profile-a-extras", EXTRAS_SKILLS),
    ):
        for name in skills:
            validate_skill_md(PLUGINS_DIR / plugin_name / "skills" / name / "SKILL.md")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="skip sync/build, only validate what is currently on disk",
    )
    args = parser.parse_args()

    try:
        if not args.validate_only:
            _log(f"[1/3] Codex — syncing {len(ALL_SKILLS)} skills into .agents/skills/ ...")
            for p in sync_agents_skills():
                _log(f"  wrote {p.relative_to(REPO_ROOT)}")

            _log("[2/3] Claude Code marketplace — syncing kernel + extras plugin skills ...")
            for p in sync_plugin_skills():
                _log(f"  wrote {p.relative_to(REPO_ROOT)}")

            _log("[3/3] Cowork — building per-skill ZIPs into dist/cowork-skills/ ...")
            for p in build_cowork_zips():
                _log(f"  wrote {p.relative_to(REPO_ROOT)}")

        _log("\nValidating ...")
        validate_all_skill_sources()
        _log("  OK: all SKILL.md frontmatter (canonical + .agents/skills + plugin copies)")
        validate_marketplace()
        _log("  OK: .claude-plugin/marketplace.json + every plugin.json")
        validate_codex_config()
        _log("  OK: .codex/config.toml parses")
        validate_claude_settings()
        _log("  OK: .claude/settings.json (extraKnownMarketplaces present)")
        for name in ALL_SKILLS:
            validate_cowork_zip(COWORK_DIST_DIR / f"{name}.skill")
        _log(f"  OK: all {len(ALL_SKILLS)} dist/cowork-skills/*.skill zips re-open and parse")

    except ValidationError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    _log("\nAll three client packages built + validated OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
