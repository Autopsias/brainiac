"""Explicit engine-only update path for hosts that do not manage Claude plugins."""
from __future__ import annotations

from .update_channels_2 import (
    _finalize_update, _run_dist_rebuild, _run_engine_refresh, _run_workspace_restage,
)


def run_engine_only_flow(*, engine_src, brainiac_home, claude_home, run,
                         dry_run, callbacks) -> dict:
    """Keep the existing engine/channel guards and doctor gate, without Claude writes.

    This opt-in does not install, update or claim to update any Claude plugins,
    marketplace or hooks. Mixed/stale surfaces still fail the final doctor gate.
    """
    result = {'ok': False, 'steps': {}, 'before_after': [], 'residual_human_steps': [],
              'mode': 'engine-only'}
    for name in ('capability_probe', 'marketplace_refresh', 'plugin_reinstall', 'session_hook'):
        result['steps'][name] = {'ok': True, 'skipped': True,
                                  'detail': 'explicit engine-only mode; no Claude mutation'}
    before = callbacks.run_doctor(brainiac_home=brainiac_home, claude_home=claude_home)
    table = {row['surface']: row['detail'] for row in before['rows']}
    result['steps']['doctor_before'] = callbacks.render_human(before)
    refreshed = _run_engine_refresh(engine_src, brainiac_home, table, run,
                                   dry_run=dry_run, callbacks=callbacks)
    result['steps']['engine_refresh'] = refreshed
    if not refreshed['ok'] and not dry_run:
        result['notes'] = f"engine refresh failed: {refreshed['detail']}"
        return result
    dist, has_source, no_source = _run_dist_rebuild(
        engine_src, run, dry_run=dry_run, callbacks=callbacks)
    result['steps']['dist_rebuild'] = dist
    if not dist['ok'] and not dry_run:
        result['notes'] = f"dist rebuild failed: {dist['detail']}"
        return result
    result['steps']['workspace_restage'] = _run_workspace_restage(
        engine_src, brainiac_home, run, dry_run=dry_run, engine_src_available=has_source,
        no_checkout_detail=no_source, callbacks=callbacks)
    after = callbacks.run_doctor(brainiac_home=brainiac_home, claude_home=claude_home)
    _finalize_update(result, table, after, callbacks)
    return result
