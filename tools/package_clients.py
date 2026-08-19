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
   ``plugins/brainiac-kernel/skills/`` and the EXTRAS split into
   ``plugins/brainiac-extras/skills/`` (the two plugins listed in
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
import shutil
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# batch-2 drain: shared primitives and the version-propagation lane moved to
# siblings and re-imported here, so every one of these names keeps its
# `package_clients` module path (tests import them there).
from package_shared import (  # noqa: E402,F401
    DIST_DIR, FRONTMATTER_RE, PLUGINS_DIR, PYPROJECT_PATH, REPO_ROOT,
    ValidationError, _log, _mini_yaml_parse, parse_skill_frontmatter,
    validate_json_file, validate_skill_md)
from package_versions import (  # noqa: E402,F401
    NPM_PACKAGE_JSON, PLUGIN_NAMES, VERSION_STAMP_PATH, VERSION_STAMP_RE,
    read_source_version, stamp_skill_version, validate_monotonic_version,
    validate_npm_version_lockstep, validate_plugin_version_lockstep,
    validate_version_stamp, write_compat_marker, write_npm_version,
    write_plugin_versions, write_version_stamp)

# The split: KERNEL = always-useful daily skills. EXTRAS = optional
# maintenance/admin skills, installed separately ("one command away").
KERNEL_SKILLS = [
    "kb-curator",
    "promote",
    "vault-ingestion",
    "vault-eval",
    "save-conversation",
    "voice",
    "brain-inbox",
    "vm-doctor",
    "graph-explorer",
]
EXTRAS_SKILLS = [
    "curation",
    "improve",
    "task-registrar",
    "autoresearch",
    "chief-of-staff",
]
ALL_SKILLS = KERNEL_SKILLS + EXTRAS_SKILLS

# brainiac-manager: host lifecycle skills (own plugin, own sync target).
# Not part of ALL_SKILLS — host Claude Code/Codex only, never zipped for
# Cowork (Cowork can't run install.sh/launchd/brain-doctor-on-the-host
# anyway). Most of these mutate host state; brainiac-health (OBS-03) is the
# one read-only exception, grouped here because it's a host-only surface
# (doctor/status/registry) rather than daily vault work.
BRAINIAC_SKILLS = [
    "brainiac-install",
    "brainiac-update",
    "brainiac-uninstall",
    "brainiac-cowork-setup",
    "brainiac-health",
]

CLAUDE_SKILLS_DIR = REPO_ROOT / ".claude" / "skills"
AGENTS_SKILLS_DIR = REPO_ROOT / ".agents" / "skills"
COWORK_DIST_DIR = REPO_ROOT / "dist" / "cowork-skills"

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
# 1b. Engine wheel assets — mirror repo-root scaffold/registration assets
#     into src/brain/_assets/ (PYP-02). The _assets tree mirrors the repo-root
#     LAYOUT so every REPO_ROOT-relative resolution in brain.init and
#     scripts/register_tasks.py works unchanged when loaded from the wheel.
# ---------------------------------------------------------------------------

ENGINE_ASSETS_DIR = REPO_ROOT / "src" / "brain" / "_assets"

# Repo-relative files shipped in the wheel. Directories are copied whole.
ENGINE_ASSET_FILES = [
    "AGENTS.md",
    "routines/manifest.json",
    # task registration (registrar + BOTH host tasks' installer surface —
    # routines/manifest.json locked_counts host budget = 2):
    "scripts/register_tasks.py",
    "scripts/install-brief-mac.sh",
    "scripts/install-brief-windows.ps1",
    "scripts/brain-brief-mac.plist",
    "scripts/brain-brief.sh",
    "scripts/brain-synthesis.sh",
    "scripts/brain-synthesis-mac.plist",
    # session-start alert hook (installer copies it into ~/.claude/hooks/) +
    # BOTH Cowork VM probes (staged into every workspace by
    # update.stage_engine_and_skills / cowork_workspace_install.sh): the
    # un-fakeable retrieval self-test, and the negative boundary probe that
    # proves the VM leg refuses every host-broker verb. The boundary probe
    # rides the wheel for the same reason the self-test does — a staged
    # workspace has no `scripts/` dir of its own, so a hand copy is the only
    # alternative and it does not survive the next re-stage.
    "scripts/brainiac-alerts.sh",
    "scripts/vm-selftest.sh",
    "scripts/vm-boundary-probe.sh",
    # FL-03 COS retro miner. brain-synthesis.sh resolves it SCRIPT-RELATIVE
    # (`../tools/cos_retro.py`), so it must ride the wheel beside the wrapper:
    # launchd runs the INSTALLED `_assets/scripts/brain-synthesis.sh`, and a
    # registered workspace has no `tools/` dir of its own.
    "tools/cos_retro.py", "tools/cos_retro_scanners.py",
    "tools/cos_browser_scan.mjs",
    # INS-01 host run validator. `brain.cos_runverify` RE-EXECUTES these two
    # rather than reading the run's report of them (the outcome-contract verdict
    # and the ledger-derived counters), so an installed engine with no `tools/`
    # dir of its own has to carry them or every nightly scores INCONCLUSIVE.
    "tools/cos_contract.py", "tools/cos_contract_criteria.py", "tools/cos_contract_criteria_2.py",
    "tools/cos_contract_snapshot_shape.py", "tools/cos_contract_verdict_clauses.py", "tools/cos_reconcile_metrics.py",
    # batch-2 drain: the shipped checkers import these siblings at load time
    # (the sync prunes unlisted mirrors, and an installed engine has no tools/).
    "tools/cos_contract_ledger_scan.py", "tools/cos_contract_provenance.py",
    "tools/cos_reconcile_rows.py", "tools/cos_reconcile_guard.py", "tools/cos_reconcile_append.py",
    "tools/cos_reconcile_steps.py",  # closure: cos_reconcile_metrics imports it at module load
    # `brain graph-report` HTML shell — the payload <script type="application/
    # json"> block is spliced in at render time by src/brain/graphreport.py;
    # everything else here is static (CSS/JS/WebGL viewer).
    "assets/graph-explorer-template.html",
]
ENGINE_ASSET_DIRS = [
    "templates",
    "overlay/template",
]


def _engine_asset_pairs() -> list[tuple[Path, Path]]:
    """(source, dest) file pairs for the engine asset mirror."""
    pairs = [(REPO_ROOT / rel, ENGINE_ASSETS_DIR / rel) for rel in ENGINE_ASSET_FILES]
    for rel in ENGINE_ASSET_DIRS:
        src_dir = REPO_ROOT / rel
        if not src_dir.is_dir():
            raise ValidationError(f"engine asset dir missing: {src_dir}")
        for f in sorted(src_dir.rglob("*")):
            if f.is_file():
                pairs.append((f, ENGINE_ASSETS_DIR / f.relative_to(REPO_ROOT)))
    return pairs


def sync_engine_assets() -> list[Path]:
    written: list[Path] = []
    expected: set[Path] = set()
    for src, dst in _engine_asset_pairs():
        if not src.is_file():
            raise ValidationError(f"engine asset source missing: {src}")
        expected.add(dst)
        if dst.exists() and dst.read_bytes() == src.read_bytes():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        written.append(dst)
    # prune stale mirrored files (a renamed/deleted source must not linger)
    if ENGINE_ASSETS_DIR.is_dir():
        for f in sorted(ENGINE_ASSETS_DIR.rglob("*")):
            if f.is_file() and f not in expected:
                f.unlink()
                written.append(f)
    return written


def validate_engine_assets() -> None:
    """Hard error if src/brain/_assets/ drifts from the repo-root originals."""
    for src, dst in _engine_asset_pairs():
        if not src.is_file():
            raise ValidationError(f"engine asset source missing: {src}")
        if not dst.is_file():
            raise ValidationError(
                f"engine asset mirror missing: {dst} — run tools/package_clients.py")
        if dst.read_bytes() != src.read_bytes():
            raise ValidationError(
                f"engine asset mirror stale: {dst} != {src} — run tools/package_clients.py")


# ---------------------------------------------------------------------------
# 2. Claude Code marketplace plugins
# ---------------------------------------------------------------------------


def sync_plugin_skills() -> list[Path]:
    written: list[Path] = []
    mapping = {
        "brainiac-kernel": KERNEL_SKILLS,
        "brainiac-extras": EXTRAS_SKILLS,
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
    if not zip_path.exists():
        raise ValidationError(f"missing {zip_path} — run tools/package_clients.py to build it")
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
        ("brainiac-kernel", KERNEL_SKILLS),
        ("brainiac-extras", EXTRAS_SKILLS),
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

            _log("[1b/4] Engine wheel assets — syncing repo-root scaffold/registration assets into src/brain/_assets/ ...")
            for p in sync_engine_assets():
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
            npm_path = write_npm_version(version)
            _log(f"  wrote {npm_path.relative_to(REPO_ROOT)} — npm bootstrap installer")

        _log("\nValidating ...")
        validate_all_skill_sources()
        _log("  OK: all SKILL.md frontmatter (canonical + .agents/skills + plugin copies)")
        validate_engine_assets()
        _log("  OK: src/brain/_assets/ engine asset mirror matches the repo-root originals (PYP-02)")
        validate_marketplace()
        _log("  OK: .claude-plugin/marketplace.json + every plugin.json")
        validate_codex_config()
        _log("  OK: .codex/config.toml parses")
        validate_claude_settings()
        _log("  OK: .claude/settings.json (extraKnownMarketplaces present)")
        # dist/ is gitignored build output, so on a clean checkout (CI) these
        # artifacts do not exist and are not a repo invariant to validate. The
        # full build path always creates dist/ first, so it still checks them.
        if DIST_DIR.exists():
            for name in ALL_SKILLS:
                validate_cowork_zip(COWORK_DIST_DIR / f"{name}.skill", version)
            _log(f"  OK: all {len(ALL_SKILLS)} dist/cowork-skills/*.skill zips re-open, parse, and carry the SSOT VERSION")
            validate_compat_marker()
            _log("  OK: dist/COMPAT + SKILL_VERSION markers match")
        else:
            _log("  SKIP: dist/ absent (gitignored build output) — cowork zips + COMPAT marker not validated")
        validate_plugin_version_lockstep(version)
        _log("  OK: all plugin.json versions match the pyproject.toml SSOT (ADR-0004 Ruling 5)")
        validate_npm_version_lockstep(version)
        _log("  OK: packaging/npm/brainiac-install/package.json matches the pyproject.toml SSOT")
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
