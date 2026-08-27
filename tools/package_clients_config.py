"""Validation of the three STATIC config artifacts: the plugin marketplace,
the Codex config, and the Claude settings file.

Split out of `tools/package_clients.py` on 2026-08-26. That file had reached
519 lines against the 500 limit and had kept the whole-project `quality-ratchet`
CI job red since 2026-08-23 — and a permanently red gate is one everyone learns
to scroll past, which is the failure this project keeps rediscovering.

These three moved and the other two validators did NOT, deliberately.
`validate_all_skill_sources` and `validate_compat_marker` read the skill LISTS
that `package_clients` defines, so moving them would need those lists imported
back and create a cycle between the two modules. These three need only the
primitives that already live in `package_shared`, so they move cleanly.

Re-imported into `package_clients`, so `--validate-only`, the pre-commit hook
and anything importing these names by their old path are unaffected.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from package_shared import (  # noqa: E402
    PLUGINS_DIR, REPO_ROOT, ValidationError, _log, validate_json_file)


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
