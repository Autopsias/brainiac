"""Outcomes-report shape + date-gate logic for the `brain` maintenance verbs
(CUT-03): `check` / `health` / `curate` / `integrity` / `promote-scan` / `maintain`.

Pure, dependency-free helpers — no I/O, no BrainCore import (mirrors brain.brief).
``check``/``health``/``curate``/``integrity``/``promote-scan`` are the FOLDED
maintenance rituals from ``routines/manifest.json`` (per-task ``disposition``); ``maintain`` is
the single sanctioned host task (``brain-nightly``) that multiplexes the
weekly/monthly cadences into one OS scheduler entry via date gates, per
``routines/manifest.json`` ``locked_counts``.

The three-bucket shape here is the generic, vault-agnostic structured
equivalent of the owner-vault scheduled-task outcomes contract's three-block
report (✅ Auto-remediated / ⚠ Action Required / 🚧 Blocked): a thin host-side
scheduled-task wrapper can render ``render_outcomes_markdown`` directly, or
consume the structured JSON.

MEM-03/AUT-02 (session s08) add two more pure pieces: recommendations
lifecycle (open -> aging -> surfaced -> resolved) file-format helpers, and
markdown renderers for the Sunday curation/promotion-scan findings. This
module still does NO I/O — ``BrainCore.maintain`` (src/brain/core/) is the
one place that reads/writes `.brain/memory/{recommendations-open.jsonl,
recommendations-log.md,hot.md}`, guarded by an idempotency key so a re-run
never duplicates a hot-queue entry (ADR-0003 Ruling 5/HARDENED:codex).
"""
from __future__ import annotations

import datetime
import itertools
import json
import logging
import re
import shlex
from pathlib import Path
from typing import Any

from .maintenance_invariant_fields import invariant_health_history_fields as invariant_health_history_fields

_log = logging.getLogger("brain.maintenance")



# The 2026-08-16 size ratchet moved every fold family into its own sibling
# module (outcomes -> escalation -> recommendations -> graphfolds -> golden ->
# hotmd -> sweep -> retention -> folds_4 -> watchdog -> healthlog -> notify).
# All names are re-exported below so every `brain.maintenance.<name>` caller
# is unchanged; folds/folds_2/folds_3 were already re-exported above.
from .maintenance_outcomes import (  # noqa: E402,F401  (facade re-export)
    _DATED_ARTIFACT as _DATED_ARTIFACT,
    action_required_item as action_required_item,
    auto_fixed_item as auto_fixed_item,
    blocked_item as blocked_item,
    build_outcomes as build_outcomes,
    framework_sync_finding as framework_sync_finding,
    reap_future_dated_artifacts as reap_future_dated_artifacts,
    render_outcomes_markdown as render_outcomes_markdown,
)

from .maintenance_escalation import (  # noqa: E402,F401  (facade re-export)
    FAILURE_ESCALATE_THRESHOLD as FAILURE_ESCALATE_THRESHOLD,
    SKIP_ESCALATE_THRESHOLD as SKIP_ESCALATE_THRESHOLD,
    WRITER_BUSY_GRACE_HOURS as WRITER_BUSY_GRACE_HOURS,
    _ALL_BRANCHES as _ALL_BRANCHES,
    _CADENCE_DAYS as _CADENCE_DAYS,
    _MONDAY as _MONDAY,
    _STALE_CADENCE_MULTIPLIER as _STALE_CADENCE_MULTIPLIER,
    _SUNDAY as _SUNDAY,
    _TUESDAY as _TUESDAY,
    _WEDNESDAY as _WEDNESDAY,
    _WEEKLY_TRIGGER_WEEKDAY as _WEEKLY_TRIGGER_WEEKDAY,
    _next_trigger as _next_trigger,
    branch_escalation as branch_escalation,
    maintain_branches as maintain_branches,
    maintain_escalation as maintain_escalation,
)

from .maintenance_recommendations import (  # noqa: E402,F401  (facade re-export)
    DEFAULT_AUTORESEARCH_STALE_DAYS as DEFAULT_AUTORESEARCH_STALE_DAYS,
    DEFAULT_RECOMMENDATION_AGING_DAYS as DEFAULT_RECOMMENDATION_AGING_DAYS,
    aggregate_stale_links as aggregate_stale_links,
    autoresearch_staleness as autoresearch_staleness,
    curation_finding_key as curation_finding_key,
    parse_recommendation_lines as parse_recommendation_lines,
    promote_scan_finding_key as promote_scan_finding_key,
    recommendations_aging_scan as recommendations_aging_scan,
    render_curation_hot_entry as render_curation_hot_entry,
    render_graphify_hot_entry as render_graphify_hot_entry,
    render_recommendation_hot_entry as render_recommendation_hot_entry,
    render_recommendation_lines as render_recommendation_lines,
    resolve_recommendation as resolve_recommendation,
)

from .maintenance_graphfolds import (  # noqa: E402,F401  (facade re-export)
    DEFAULT_GRAPHIFY_COOLDOWN_DAYS as DEFAULT_GRAPHIFY_COOLDOWN_DAYS,
    DEFAULT_GRAPHIFY_DRIFT_PCT as DEFAULT_GRAPHIFY_DRIFT_PCT,
    DEFAULT_GRAPH_ORPHAN_GROWTH_MAX as DEFAULT_GRAPH_ORPHAN_GROWTH_MAX,
    DEFAULT_KL_ORPHAN_SUSTAINED_GROWTH as DEFAULT_KL_ORPHAN_SUSTAINED_GROWTH,
    GRAPHIFY_BACKOFF_MAX_MULTIPLIER as GRAPHIFY_BACKOFF_MAX_MULTIPLIER,
    GRAPHIFY_COOLDOWN_DAYS_ENV as GRAPHIFY_COOLDOWN_DAYS_ENV,
    GRAPHIFY_DRIFT_PCT_ENV as GRAPHIFY_DRIFT_PCT_ENV,
    GRAPH_ORPHAN_GROWTH_MAX_ENV as GRAPH_ORPHAN_GROWTH_MAX_ENV,
    KL_ORPHAN_SUSTAINED_GROWTH_ENV as KL_ORPHAN_SUSTAINED_GROWTH_ENV,
    KL_ORPHAN_TRAILING_DAYS as KL_ORPHAN_TRAILING_DAYS,
    graph_hygiene_orphan_growth as graph_hygiene_orphan_growth,
    graphify_backoff_days as graphify_backoff_days,
    graphify_drift as graphify_drift,
    graphify_drift_marker_due as graphify_drift_marker_due,
    kl_orphan_sustained_growth as kl_orphan_sustained_growth,
    render_graph_hygiene_hot_entry as render_graph_hygiene_hot_entry,
    render_kl_orphan_sustained_growth_hot_entry as render_kl_orphan_sustained_growth_hot_entry,
    should_alert_graph_hygiene_growth as should_alert_graph_hygiene_growth,
    should_alert_kl_orphan_sustained_growth as should_alert_kl_orphan_sustained_growth,
    should_trigger_drift_graphify as should_trigger_drift_graphify,
)

from .maintenance_golden import (  # noqa: E402,F401  (facade re-export)
    DEFAULT_GOLDEN_CODEX_TIMEOUT_SECONDS as DEFAULT_GOLDEN_CODEX_TIMEOUT_SECONDS,
    DEFAULT_GOLDEN_RETRY_BASE_MINUTES as DEFAULT_GOLDEN_RETRY_BASE_MINUTES,
    GOLDEN_CODEX_TIMEOUT_SECONDS_ENV as GOLDEN_CODEX_TIMEOUT_SECONDS_ENV,
    GOLDEN_EXIT_ACTION_REQUIRED as GOLDEN_EXIT_ACTION_REQUIRED,
    GOLDEN_EXIT_OK as GOLDEN_EXIT_OK,
    GOLDEN_EXIT_REGRESSION as GOLDEN_EXIT_REGRESSION,
    GOLDEN_EXIT_TRANSIENT as GOLDEN_EXIT_TRANSIENT,
    GOLDEN_RETRY_BASE_MINUTES_ENV as GOLDEN_RETRY_BASE_MINUTES_ENV,
    GOLDEN_RETRY_MAX_MULTIPLIER as GOLDEN_RETRY_MAX_MULTIPLIER,
    MAX_GOLDEN_CODEX_TIMEOUT_SECONDS as MAX_GOLDEN_CODEX_TIMEOUT_SECONDS,
    _GOLDEN_VALID_DISPOSITIONS as _GOLDEN_VALID_DISPOSITIONS,
    _GOLDEN_VALID_EXIT_CODES as _GOLDEN_VALID_EXIT_CODES,
    build_codex_golden_prompt as build_codex_golden_prompt,
    golden_attempt_due as golden_attempt_due,
    golden_codex_timeout_seconds as golden_codex_timeout_seconds,
    golden_probes_path as golden_probes_path,
    golden_retry_backoff_minutes as golden_retry_backoff_minutes,
    parse_codex_final_message as parse_codex_final_message,
    render_cos_waiting_hot_entry as render_cos_waiting_hot_entry,
    render_promote_scan_hot_entry as render_promote_scan_hot_entry,
    update_golden_attempt_marker as update_golden_attempt_marker,
    validate_golden_probe_doc as validate_golden_probe_doc,
)

from .maintenance_hotmd import (  # noqa: E402,F401  (facade re-export)
    HOT_MD_ROTATE_KEEP_DAYS as HOT_MD_ROTATE_KEEP_DAYS,
    _joined_len as _joined_len,
    rotate_hot_md as rotate_hot_md,
)

from .maintenance_sweep import (  # noqa: E402,F401  (facade re-export)
    WORKSPACE_SWEEP_AGE_ENV as WORKSPACE_SWEEP_AGE_ENV,
    WORKSPACE_SWEEP_DEFAULT_AGE_DAYS as WORKSPACE_SWEEP_DEFAULT_AGE_DAYS,
    WORKSPACE_SWEEP_DIRS_ENV as WORKSPACE_SWEEP_DIRS_ENV,
    sweep_workspace as sweep_workspace,
    workspace_sweep_config as workspace_sweep_config,
)

from .maintenance_retention import (  # noqa: E402,F401  (facade re-export)
    _QUARANTINE_REMEDY as _QUARANTINE_REMEDY,
    DEFAULT_DUPLICATE_RETENTION_DAYS as DEFAULT_DUPLICATE_RETENTION_DAYS,
    DUPLICATE_RETENTION_DAYS_ENV as DUPLICATE_RETENTION_DAYS_ENV,
    _DUPLICATE_SIDECAR_SUFFIX as _DUPLICATE_SIDECAR_SUFFIX,
    _sha256_file as _sha256_file,
    _verify_duplicate_provenance as _verify_duplicate_provenance,
    ingest_quarantine_findings as ingest_quarantine_findings,
    quarantine_summary_due as quarantine_summary_due,
    quarantine_triage_summary as quarantine_triage_summary,
    render_quarantine_summary_hot_entry as render_quarantine_summary_hot_entry,
    retention_fold as retention_fold,
)

from .maintenance_folds_4 import (  # noqa: E402,F401  (facade re-export)
    DECISION_CAPTURE_LOOKBACK_DAYS as DECISION_CAPTURE_LOOKBACK_DAYS,
    DECISION_CAPTURE_MAX_CANDIDATES as DECISION_CAPTURE_MAX_CANDIDATES,
    _AUTODEDUP_DEFAULT_CAP as _AUTODEDUP_DEFAULT_CAP,
    _DECISION_LANGUAGE_RE as _DECISION_LANGUAGE_RE,
    _LEADING_DATE_RE as _LEADING_DATE_RE,
    _PARA_ZONES as _PARA_ZONES,
    _VERSION_ID_RE as _VERSION_ID_RE,
    _WIKILINK_RE as _WIKILINK_RE,
    _body_bytes as _body_bytes,
    _floor_bytes as _floor_bytes,
    _normalize_body_for_hash as _normalize_body_for_hash,
    auto_para as auto_para,
    auto_version_chains as auto_version_chains,
    autodedup_max_per_run as autodedup_max_per_run,
    body_sha256 as body_sha256,
    decision_capture_scan as decision_capture_scan,
    render_autodedup_hot_entry as render_autodedup_hot_entry,
    render_decision_capture_hot_entry as render_decision_capture_hot_entry,
    version_family_key as version_family_key,
)

from .maintenance_watchdog import (  # noqa: E402,F401  (facade re-export)
    DEFAULT_OFFHOST_DAILY_STALE_HOURS as DEFAULT_OFFHOST_DAILY_STALE_HOURS,
    OFFHOST_DAILY_STALE_HOURS_ENV as OFFHOST_DAILY_STALE_HOURS_ENV,
    SYNTHESIS_STALE_DAYS as SYNTHESIS_STALE_DAYS,
    SYNTHESIS_STATE_ENV as SYNTHESIS_STATE_ENV,
    _load_synthesis_entry as _load_synthesis_entry,
    _union_by_run_id as _union_by_run_id,
    latest_synthesis_cost as latest_synthesis_cost,
    offhost_watchdog_findings as offhost_watchdog_findings,
    synthesis_heartbeat_finding as synthesis_heartbeat_finding,
)

from .maintenance_healthlog import (  # noqa: E402,F401  (facade re-export)
    DEFAULT_GOLDEN_REGRESSION_PCT as DEFAULT_GOLDEN_REGRESSION_PCT,
    DEFAULT_HEALTH_ARCHIVE_RETENTION_DAYS as DEFAULT_HEALTH_ARCHIVE_RETENTION_DAYS,
    DEFAULT_HEALTH_HISTORY_MAX_BYTES as DEFAULT_HEALTH_HISTORY_MAX_BYTES,
    DEFAULT_HEALTH_HISTORY_READ_WINDOW_DAYS as DEFAULT_HEALTH_HISTORY_READ_WINDOW_DAYS,
    DEFAULT_LATENCY_REGRESSION_PCT as DEFAULT_LATENCY_REGRESSION_PCT,
    DEFAULT_QUARANTINE_REGRESSION_PCT as DEFAULT_QUARANTINE_REGRESSION_PCT,
    DEFAULT_REMEDIATION_COST_ALERT_USD as DEFAULT_REMEDIATION_COST_ALERT_USD,
    DEFAULT_REMEDIATION_COST_REGRESSION_MULTIPLE as DEFAULT_REMEDIATION_COST_REGRESSION_MULTIPLE,
    BRAIN_REMEDIATION_COST_ALERT_USD_ENV as BRAIN_REMEDIATION_COST_ALERT_USD_ENV,
    HEALTH_ARCHIVE_RETENTION_DAYS_ENV as HEALTH_ARCHIVE_RETENTION_DAYS_ENV,
    HEALTH_HISTORY_LOCK_STALE_SECONDS as HEALTH_HISTORY_LOCK_STALE_SECONDS,
    HEALTH_HISTORY_MAX_BYTES_ENV as HEALTH_HISTORY_MAX_BYTES_ENV,
    HEALTH_HISTORY_READ_WINDOW_DAYS_ENV as HEALTH_HISTORY_READ_WINDOW_DAYS_ENV,
    HEALTH_TREND_MIN_BASELINE_DAYS as HEALTH_TREND_MIN_BASELINE_DAYS,
    HEALTH_TREND_MIN_CURRENT_SAMPLES as HEALTH_TREND_MIN_CURRENT_SAMPLES,
    HEALTH_TREND_MIN_DAYS as HEALTH_TREND_MIN_DAYS,
    SPARSE_METRICS as SPARSE_METRICS,
    _DAILY_REDUCERS as _DAILY_REDUCERS,
    _acquire_health_history_lock as _acquire_health_history_lock,
    _append_sparse_metrics as _append_sparse_metrics,
    _bucket_daily as _bucket_daily,
    _count_files as _count_files,
    _prune_health_archive as _prune_health_archive,
    _prune_old_files as _prune_old_files,
    _record_date as _record_date,
    _release_health_history_lock as _release_health_history_lock,
    _rotate_health_history as _rotate_health_history,
    append_health_record as append_health_record,
    new_health_run_id as new_health_run_id,
    read_health_history as read_health_history,
    read_sparse_history as read_sparse_history,
)

from .maintenance_notify import (  # noqa: E402,F401  (facade re-export)
    DEFAULT_NOTIFY_MARKER_RETENTION_DAYS as DEFAULT_NOTIFY_MARKER_RETENTION_DAYS,
    NOTIFY_ENV as NOTIFY_ENV,
    NOTIFY_MARKER_RETENTION_DAYS_ENV as NOTIFY_MARKER_RETENTION_DAYS_ENV,
    _notify_marker_dir as _notify_marker_dir,
    _notify_marker_path as _notify_marker_path,
    _prune_notify_markers as _prune_notify_markers,
    degradation_findings as degradation_findings,
    fire_and_mark_notifications as fire_and_mark_notifications,
    fire_notification as fire_notification,
    mark_notified as mark_notified,
    pending_notifications as pending_notifications,
    should_notify as should_notify,
)

# (Pre-existing s15-era fold re-exports, kept LAST: maintenance_folds copies
# parent constants at import time, so the facades above must bind first.)
from .maintenance_folds import auto_dedup_tier1 as auto_dedup_tier1  # noqa: E402
from .maintenance_folds_2 import collect_health_metrics as collect_health_metrics, health_trend as health_trend  # noqa: E402
from .maintenance_folds_3 import refresh_navigation as refresh_navigation  # noqa: E402
