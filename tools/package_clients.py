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
3. **Cowork** — zips each kernel+extras skill (ALL_SKILLS) individually into
   ``dist/cowork-skills/<name>.skill`` (a zip with ``<name>/SKILL.md`` at its
   root, ready for the Cowork "Save skill" upload flow). brainiac-manager's
   lifecycle skills (host Claude Code/Codex only) are never zipped for Cowork.
4. **Version marker** — writes ``dist/COMPAT`` (the pyproject.toml version)
   and stamps a generated ``SKILL_VERSION`` line into every DISTRIBUTED copy
   of a brainiac-manager skill (never the canonical source). ``/brainiac-update``
   compares this against the code's own version to detect skill<->code skew.
5. **Plugin version propagation (ADR-0004 Ruling 1/5, s06)** — writes the
   SAME pyproject.toml version into every ``plugins/*/.claude-plugin/plugin.json``.
   Per Ruling 5 (single version line, human-confirmed reconciliation), there is
   no independent plugin version line to preserve; ``--validate-only`` treats
   ANY plugin.json version that differs from pyproject.toml (or from
   dist/COMPAT, or from a distributed SKILL_VERSION stamp) as a hard error —
   skew-is-error, not skew-is-expected.

It also validates every artifact it produces or touches:
- every ``SKILL.md`` frontmatter parses and carries ``name`` + ``description``
- ``.claude-plugin/marketplace.json`` and every ``plugin.json`` parse as JSON
  and carry ``name`` (+ ``version`` for every plugin.json)
- every ``plugin.json`` ``version`` equals the pyproject.toml SSOT version
  (ADR-0004 Ruling 5 — hard error on skew)
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
    "voice",
]
EXTRAS_SKILLS = [
    "curation",
    "improve",
    "task-registrar",
    "autoresearch",
]
ALL_SKILLS = KERNEL_SKILLS + EXTRAS_SKILLS

# brainiac-manager: host-mutating lifecycle skills (own plugin, own sync
# target). Not part of ALL_SKILLS — host Claude Code/Codex only, never
# zipped for Cowork (Cowork can't run install.sh/launchd anyway).
BRAINIAC_SKILLS = [
    "brainiac-install",
    "brainiac-update",
    "brainiac-uninstall",
    "brainiac-cowork-setup",
]

CLAUDE_SKILLS_DIR = REPO_ROOT / ".claude" / "skills"
AGENTS_SKILLS_DIR = REPO_ROOT / ".agents" / "skills"
PLUGINS_DIR = REPO_ROOT / "plugins"
COWORK_DIST_DIR = REPO_ROOT / "dist" / "cowork-skills"
DIST_DIR = REPO_ROOT / "dist"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"

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
# SKILL_VERSION / dist/COMPAT — generated version marker (VERIFY-ROUND-1)
#
# /brainiac-update (s03) compares this marker against ~/brainiac's code
# version to detect skill<->code skew. It MUST be produced here, from the
# same pyproject.toml version the code ships, never hand-maintained in any
# SKILL.md — otherwise the version contract the update skill relies on is
# unenforceable.
# ---------------------------------------------------------------------------


def read_source_version() -> str:
    text = PYPROJECT_PATH.read_text(encoding="utf-8")
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    if not m:
        raise ValidationError(f"{PYPROJECT_PATH}: no top-level version = \"...\" found")
    return m.group(1)


def write_compat_marker(version: str) -> Path:
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    compat_path = DIST_DIR / "COMPAT"
    compat_path.write_text(version + "\n", encoding="utf-8")
    return compat_path


# ---------------------------------------------------------------------------
# Committed version stamp (ADR-0005 Ruling 1) — src/brain/_version.py is the
# from-source fallback the zero-install VM and the clean-room export report.
# It MUST be git-committed (the export ships only git-tracked files), and it
# is rewritten here — release.py runs this packager in the same act as the
# pyproject bump — so staleness is structurally impossible.
# ---------------------------------------------------------------------------

VERSION_STAMP_PATH = REPO_ROOT / "src" / "brain" / "_version.py"
VERSION_STAMP_RE = re.compile(r'(?m)^__version__ = "([^"]+)"$')

VERSION_STAMP_TEMPLATE = '''"""Committed version stamp — GENERATED by tools/package_clients.py; do not hand-edit.

ADR-0005 Ruling 1: the clean-room export ships only git-tracked files, so the
zero-install VM (staged source, no package metadata) can only report a real
version if this stamp is COMMITTED. tools/release.py rewrites it (via
tools/package_clients.py) in the same act as the pyproject.toml bump, so the
tagged release commit always carries the exact released version. On the
pip-installed host, importlib.metadata stays primary; brain/__init__.py reads
this file only as the fallback before 0.0.0+unknown.
"""

__version__ = "{version}"
'''


def write_version_stamp(version: str) -> Path:
    content = VERSION_STAMP_TEMPLATE.format(version=version)
    if not VERSION_STAMP_PATH.exists() or VERSION_STAMP_PATH.read_text(encoding="utf-8") != content:
        VERSION_STAMP_PATH.write_text(content, encoding="utf-8")
    return VERSION_STAMP_PATH


def validate_version_stamp(version: str) -> None:
    """Hard error if src/brain/_version.py is missing or != the pyproject SSOT
    (ADR-0005 Ruling 1 — a stale committed stamp is worse than 0.0.0+unknown)."""
    if not VERSION_STAMP_PATH.exists():
        raise ValidationError(f"missing {VERSION_STAMP_PATH} (ADR-0005 Ruling 1 committed version stamp)")
    m = VERSION_STAMP_RE.search(VERSION_STAMP_PATH.read_text(encoding="utf-8"))
    if not m:
        raise ValidationError(f"{VERSION_STAMP_PATH}: no __version__ = \"...\" line found")
    if m.group(1) != version:
        raise ValidationError(
            f"{VERSION_STAMP_PATH}: stamp {m.group(1)!r} != pyproject.toml SSOT version {version!r} "
            "(ADR-0005 Ruling 1 — rerun tools/package_clients.py and commit the stamp)"
        )


# ---------------------------------------------------------------------------
# Plugin version propagation (ADR-0004 Ruling 1/5, s06) — the three
# plugin.json files carry no independent version line; the SSOT is written
# into all of them at package time, and --validate-only hard-fails any skew.
# ---------------------------------------------------------------------------

PLUGIN_NAMES = ["brainiac-manager", "profile-a-kernel", "profile-a-extras"]


def write_plugin_versions(version: str) -> list[Path]:
    written: list[Path] = []
    for pname in PLUGIN_NAMES:
        plugin_json_path = PLUGINS_DIR / pname / ".claude-plugin" / "plugin.json"
        data = validate_json_file(plugin_json_path)
        if data.get("version") != version:
            data["version"] = version
            plugin_json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(plugin_json_path)
    return written


def validate_monotonic_version(version: str) -> None:
    """ADR-0005 Ruling 5 (GV-01): hard-fail if the SSOT version on disk is not
    strictly greater than the release baseline (highest semver-shaped local
    tag, or itself if no such tag exists — a same-as-baseline version is fine
    here, since --validate-only runs on a checkout that may already be AT the
    just-cut version; only a real regression below the baseline must fail).
    Reuses tools/release.py's guard so the rule lives in exactly one place."""
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    import release as _release  # local import: avoid a hard import-time coupling for callers that only need packaging

    baseline = _release.monotonic_baseline(version)
    if _release._version_key(version) < _release._version_key(baseline):
        raise ValidationError(
            f"pyproject.toml SSOT version {version!r} is lower than the release baseline "
            f"{baseline!r} (highest semver-shaped local tag) — ADR-0005 Ruling 5 monotonic guard"
        )


def validate_plugin_version_lockstep(version: str) -> None:
    """Hard error on ANY plugin.json version != the pyproject SSOT version
    (ADR-0004 Ruling 5: single version line, skew-is-error)."""
    for pname in PLUGIN_NAMES:
        plugin_json_path = PLUGINS_DIR / pname / ".claude-plugin" / "plugin.json"
        data = validate_json_file(plugin_json_path)
        pversion = data.get("version")
        if pversion != version:
            raise ValidationError(
                f"{plugin_json_path}: version {pversion!r} != pyproject.toml SSOT version {version!r} "
                "(ADR-0004 Ruling 5 — single version line, skew is a hard error)"
            )


def stamp_skill_version(skill_md_path: Path, version: str) -> None:
    """Append a generated SKILL_VERSION marker line to a DISTRIBUTED copy of a
    SKILL.md (never the canonical .claude/skills/ source)."""
    text = skill_md_path.read_text(encoding="utf-8")
    marker = f"<!-- SKILL_VERSION: {version} (generated by tools/package_clients.py — do not hand-edit) -->\n"
    # Match only a real marker on its own line — prose may mention
    # "<!-- SKILL_VERSION: ... -->" mid-line (e.g. brainiac-update docs).
    marker_re = re.compile(r"^<!-- SKILL_VERSION:.*?-->\n?", re.MULTILINE)
    if marker_re.search(text):
        text = marker_re.sub(marker, text, count=1)
    else:
        text = text + "\n" + marker
    skill_md_path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Codex — mirror .claude/skills/<name>/ -> .agents/skills/<name>/
# ---------------------------------------------------------------------------


def sync_agents_skills() -> list[Path]:
    written: list[Path] = []
    AGENTS_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    for name in ALL_SKILLS + BRAINIAC_SKILLS:
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
        "brainiac-manager": BRAINIAC_SKILLS,
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


def build_cowork_zips(version: str) -> list[Path]:
    """Zip each kernel+extras skill, plus a top-level ``VERSION`` file stamped
    with the pyproject SSOT (cw-02) — the ONE marker `cowork_workspace_install.sh`
    and `brain doctor` read to tell a current skill bundle from a stale one,
    without polluting the canonical SKILL.md content (unlike the
    brainiac-manager SKILL_VERSION marker, these skills are re-zipped fresh on
    every run — a version file inside is cheaper than parsing frontmatter)."""
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
            zf.writestr(f"{name}/VERSION", version + "\n")
        written.append(zip_path)
    return written


def validate_cowork_zip(zip_path: Path, version: str) -> None:
    name = zip_path.stem
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad is not None:
            raise ValidationError(f"{zip_path}: corrupt member {bad}")
        inner = f"{name}/SKILL.md"
        if inner not in zf.namelist():
            raise ValidationError(f"{zip_path}: missing {inner}")
        fm_text = zf.read(inner).decode("utf-8")
        version_inner = f"{name}/VERSION"
        if version_inner not in zf.namelist():
            raise ValidationError(f"{zip_path}: missing {version_inner} (cw-02 version marker)")
        zipped_version = zf.read(version_inner).decode("utf-8").strip()
        if zipped_version != version:
            raise ValidationError(
                f"{zip_path}: {version_inner} {zipped_version!r} != pyproject.toml SSOT version {version!r}"
            )
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
    for name in ALL_SKILLS + BRAINIAC_SKILLS:
        validate_skill_md(CLAUDE_SKILLS_DIR / name / "SKILL.md")
        validate_skill_md(AGENTS_SKILLS_DIR / name / "SKILL.md")
    for plugin_name, skills in (
        ("profile-a-kernel", KERNEL_SKILLS),
        ("profile-a-extras", EXTRAS_SKILLS),
        ("brainiac-manager", BRAINIAC_SKILLS),
    ):
        for name in skills:
            validate_skill_md(PLUGINS_DIR / plugin_name / "skills" / name / "SKILL.md")


def validate_compat_marker() -> None:
    compat_path = DIST_DIR / "COMPAT"
    if not compat_path.exists():
        raise ValidationError(f"missing {compat_path}")
    version = compat_path.read_text(encoding="utf-8").strip()
    if not version:
        raise ValidationError(f"{compat_path}: empty")
    for name in BRAINIAC_SKILLS:
        for base in (AGENTS_SKILLS_DIR, PLUGINS_DIR / "brainiac-manager" / "skills"):
            skill_path = base / name / "SKILL.md"
            text = skill_path.read_text(encoding="utf-8")
            if f"SKILL_VERSION: {version}" not in text:
                raise ValidationError(f"{skill_path}: missing/mismatched SKILL_VERSION marker (expected {version})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="skip sync/build, only validate what is currently on disk",
    )
    args = parser.parse_args()

    try:
        version = read_source_version()

        if not args.validate_only:
            _log(f"[1/4] Codex — syncing {len(ALL_SKILLS) + len(BRAINIAC_SKILLS)} skills into .agents/skills/ ...")
            for p in sync_agents_skills():
                _log(f"  wrote {p.relative_to(REPO_ROOT)}")

            _log("[2/4] Claude Code marketplace — syncing kernel + extras + brainiac-manager plugin skills ...")
            for p in sync_plugin_skills():
                _log(f"  wrote {p.relative_to(REPO_ROOT)}")

            _log("[3/4] Cowork — building per-skill ZIPs into dist/cowork-skills/ ...")
            for p in build_cowork_zips(version):
                _log(f"  wrote {p.relative_to(REPO_ROOT)}")

            _log("[4/4] Version marker — stamping dist/COMPAT + SKILL_VERSION into distributed copies ...")
            compat_path = write_compat_marker(version)
            _log(f"  wrote {compat_path.relative_to(REPO_ROOT)} ({version})")
            stamp_path = write_version_stamp(version)
            _log(f"  wrote {stamp_path.relative_to(REPO_ROOT)} ({version}) — committed stamp, ADR-0005 Ruling 1")
            for name in BRAINIAC_SKILLS:
                for base in (AGENTS_SKILLS_DIR, PLUGINS_DIR / "brainiac-manager" / "skills"):
                    stamp_skill_version(base / name / "SKILL.md", version)
            _log(f"  propagating version {version} into all {len(PLUGIN_NAMES)} plugin.json (ADR-0004 Ruling 5) ...")
            for p in write_plugin_versions(version):
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
            validate_cowork_zip(COWORK_DIST_DIR / f"{name}.skill", version)
        _log(f"  OK: all {len(ALL_SKILLS)} dist/cowork-skills/*.skill zips re-open, parse, and carry the SSOT VERSION")
        validate_compat_marker()
        _log("  OK: dist/COMPAT + SKILL_VERSION markers match")
        validate_plugin_version_lockstep(version)
        _log("  OK: all plugin.json versions match the pyproject.toml SSOT (ADR-0004 Ruling 5)")
        validate_version_stamp(version)
        _log("  OK: src/brain/_version.py stamp matches the pyproject.toml SSOT (ADR-0005 Ruling 1)")
        validate_monotonic_version(version)
        _log("  OK: pyproject.toml SSOT version is not below the release baseline (ADR-0005 Ruling 5)")

    except ValidationError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    _log("\nAll three client packages built + validated OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
