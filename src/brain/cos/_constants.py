"""COS immutable definitions."""
from __future__ import annotations

import re
from typing import Any

from .. import config, provenance

PROPOSAL_TTL_DAYS_ENV = "BRAIN_COS_PROPOSAL_TTL_DAYS"

DEFAULT_PROPOSAL_TTL_DAYS = 14

BATCH_TTL_DAYS_ENV = "BRAIN_COS_BATCH_TTL_DAYS"

DEFAULT_BATCH_TTL_DAYS = 7

GC_DAYS_ENV = "BRAIN_COS_GC_DAYS"

DEFAULT_GC_DAYS = 30

BATCH_SCHEMA = "cos_ingestion_batch/v1"

EVIDENCE_SCHEMA = "cos-evidence-manifest/v1"

BROKER_KEY_PREFIX = "cosbroker:"

CORRECT_KEY_PREFIX = "coscorrect:"

_ACCEPT_ALL = "accept all"

_REJECT_ALL = "reject all"

_ACCEPT_PARTIAL_RE = re.compile(r"^accept\s*:\s*(?P<ids>[a-z0-9,\s-]+?)\s*(?:\(.*\))?$",
                                re.IGNORECASE)

SECRET_PATTERNS = provenance.SECRET_PATTERNS

secret_findings = provenance.secret_findings

scrub = provenance.scrub

_PERMS = {"host": 0o700, "shared": 0o755, "drop": 0o775}

APPROVED_ANCHOR_SCHEMA = "cos_approved_anchor/v1"

_APPROVED_DIRNAME = "cos-approved"

ATTACHMENT_ANCHOR_SCHEMA = "cos_attachment_anchor/v1"

_ATTACHMENT_ANCHOR_DIRNAME = "cos-attachment-anchors"

class ApprovedQueueUnsafe(config.HostPathUnsafe):
    """The configured approved queue resolves inside something the VM can see.

    Then the whole point is lost, so it is refused rather than used (fail
    closed) — the same posture ``querylog`` takes when ``$BRAIN_INDEX_DIR`` is
    pointed into the vault. Subclasses ``config.HostPathUnsafe`` because the
    RULE now lives in ``config`` (the single-writer lock needs it too and
    ``config`` cannot import ``cos``) — one rule, one exception type."""

class ApprovedRefused(RuntimeError):
    """These bytes are not the bytes the host approved. Never sign them."""

class ReleaseRecordsUnreadable(RuntimeError):
    """The attachment RELEASE records could not be read as a whole.

    Deliberately not an :class:`ApprovedRefused` (nothing here is a verdict
    about bytes) and deliberately not swallowed: these records arm the drain's
    fail-closed refusal of a released-but-unanchored file, they live on the
    mount, and treating "unreadable" as "there are none" is the fail-open the
    refusal exists to close."""

class ApprovedTooLarge(ApprovedRefused):
    """The entry is bigger than the caller's cap — the read stopped at it.

    Distinct from a plain refusal so a caller with its own size policy (the
    ingest drain's ``MAX_INGEST_BYTES``) can report "file_too_large" rather
    than "unreadable", without a second reading routine that enforces the cap
    on a path it re-opens."""

class ApprovedKeyUnavailable(RuntimeError):
    """The host key could not be resolved, so no anchor can be VERIFIED.

    Deliberately NOT an :class:`ApprovedRefused`: a locked keychain, a
    scheduler running as the wrong user, a missing ``cryptography`` or a key
    rotation must read as "this host cannot sign right now" — the same
    fail-closed, leave-it-in-place answer the ordinary draft path gives — never
    as "someone tampered with the bytes"."""

vm_visible_roots = config.vm_visible_roots

MODE_HOST_PRIVATE = 0o600

MODE_VM_READABLE = 0o644     # shared/: host writes, VM reads

MODE_VM_WRITABLE = 0o664     # drop/: VM writes, host reads (dir is 0775)

_APPEND_LOCK_SECONDS = 10.0

ATTACHMENT_HOLD_SCHEMA = "cos_attachment_hold/v1"

RUN_MANIFEST_SCHEMA = "cos_run_manifest/v1"

RUN_ID_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}-run[0-9]+$")

_RUN_NUMBER_RE = re.compile(r"run([0-9]+)")

RUN_VALID = "VALID"

RUN_VALID_DEGRADED = "VALID_DEGRADED"

RUN_INVALID = "INVALID"

RUN_INCONCLUSIVE = "INCONCLUSIVE"

RUN_VERDICTS = (RUN_VALID, RUN_VALID_DEGRADED, RUN_INVALID, RUN_INCONCLUSIVE)

CLAIMABLE_VERDICTS = (RUN_VALID, RUN_VALID_DEGRADED)

RUNS_MIGRATION_MARKER = ".carried-forward-from-mount.json"

MAX_RUN_DIGITS = 9

_LEDGER_GLOB = "_cos_ingestion_ledger_*.jsonl"

_LEDGER_RUN_RE = re.compile(r"^_cos_ingestion_ledger_(.+)\.jsonl$")

_LEDGER_ID_KEYS = ("proposal_id", "id")

_LEDGER_DIGEST_KEYS = ("content_sha256", "proposal_sha256", "sha256")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

QUARANTINE_NO_LEDGER = "no-ledger-row"

QUARANTINE_NO_MANIFEST = "no-run-manifest"

PRODUCER_STAMP_KEYS: tuple[str, ...] = ("bundle_version", "extraction_rules_version")

_STRIPPED_CLAIM_KEYS: tuple[str, ...] = provenance.HOST_ONLY_KEYS + PRODUCER_STAMP_KEYS

KIND_SUPERSEDE = "supersede"

VERSION_LINK_RUN_EVENT = "version-link-run"

BATCH_CAP_TOTAL = 12

BATCH_SUBCAP_INGESTION = 8

BATCH_SUBCAP_SUPERSEDE = 4

_MARKER_RANK_LABEL = {0: "draft", 100: "final"}

_CONSUME_JOURNAL = "consume-pending.json"

_SIG_FAILED = "digest/signature verification failed"

_NO_ANSWER = "no answered owner-inbox entry for this batch"

BEHAVIOUR_OBSERVATIONS = ("owner_replied", "owner_flagged", "owner_read",
                          "owner_archived", "untouched")

LEGACY_REJOIN_PREFIX = "content-rejoin:"

_OVERRIDE_LINE_RE = re.compile(
    r"^\s*[-*]\s*(?P<id>[a-z0-9][a-z0-9-]*)\s*:\s*(?P<prio>high|normal|low|exclude)\s*$",
    re.IGNORECASE)

HOLD_RECORD_SCHEMA = "cos_hold_record/v1"

AUTOCAP_MIN_VOLUME_ENV = "BRAIN_COS_AUTOCAP_MIN_VOLUME"

DEFAULT_AUTOCAP_MIN_VOLUME = 8

AUTOCAP_MIN_LOWER_BOUND_ENV = "BRAIN_COS_AUTOCAP_MIN_LOWER_BOUND"

DEFAULT_AUTOCAP_MIN_LOWER_BOUND = 0.85

AUTOCAP_UNDO_HOURS_ENV = "BRAIN_COS_AUTOCAP_UNDO_HOURS"

DEFAULT_AUTOCAP_UNDO_HOURS = 24

_UNPATTERNED = {"", "unclassified", "unknown", None}

AUTOCAP_EXPLORATION_K_ENV = "BRAIN_COS_AUTOCAP_EXPLORATION_K"

DEFAULT_AUTOCAP_EXPLORATION_K = 5

AUTOCAP_WINDOW_DAYS_ENV = "BRAIN_COS_AUTOCAP_WINDOW_DAYS"

DEFAULT_AUTOCAP_WINDOW_DAYS = 90

AUTOCAP_WINDOW_VERDICTS_ENV = "BRAIN_COS_AUTOCAP_WINDOW_VERDICTS"

DEFAULT_AUTOCAP_WINDOW_VERDICTS = 50

AUTOCAP_BULK_MAX_BATCH_ENV = "BRAIN_COS_AUTOCAP_BULK_MAX_BATCH"

DEFAULT_AUTOCAP_BULK_MAX_BATCH = 3

CATEGORY_UNCLASSIFIED = "unclassified"   # already in _UNPATTERNED: never graduates

LANE_TEXT = "text"

LANE_ATTACHMENT = "attachment"

LANES = (LANE_TEXT, LANE_ATTACHMENT)

DISPOSITION_NEVER = "never"

DISPOSITION_PROPOSE = "propose"

_FORWARD_WRAPPER_RE = re.compile(
    r"^(?:-{2,}\s*(?:original message|forwarded message)\s*-{2,}"
    r"|begin forwarded message:"
    r"|(?:from|sent|to|cc|bcc|subject|date|reply-to)\s*:.*"
    r"|on\b.{0,160}\bwrote:)$",
    re.IGNORECASE)

PATTERN_AUTOCAPTURE_STATUS = (
    "owner-batch by default: auto-capture requires BOTH producer keys "
    "(`category` + `extraction_rules_version`) AND a category that has "
    "GRADUATED under that candidate's own ruleset version. Graduation comes "
    "only from accumulated owner verdicts, so a new or re-versioned category "
    "always starts here. A candidate missing either key never reaches the "
    "graduation test — see `unstamped_batched`")

KEEPER_HORIZON_DAYS_ENV = "BRAIN_COS_KEEPER_HORIZON_DAYS"

DEFAULT_KEEPER_HORIZON_DAYS = 7

INGEST_SWEEP_MAX_BYTES_ENV = "BRAIN_COS_SWEEP_MAX_BYTES"

DEFAULT_INGEST_SWEEP_MAX_BYTES = 200 * 1024 * 1024

INGEST_SWEEP_DOWNLOADS_ENV = "BRAIN_COS_DOWNLOADS_DIR"

INGEST_SWEEP_SKEW_SECONDS = 300          # manifest ts vs file mtime clock skew

INGEST_SWEEP_SIZE_TOLERANCE = 0.10       # when the manifest carries a size

INGEST_SWEEP_RECENCY_ENV = "BRAIN_COS_SWEEP_RECENCY_SECONDS"

DEFAULT_INGEST_SWEEP_RECENCY_SECONDS = 6 * 3600

BATCH_STALE_HOURS_ENV = "BRAIN_COS_BATCH_STALE_HOURS"

DEFAULT_BATCH_STALE_HOURS = 48


__all__ = ['PROPOSAL_TTL_DAYS_ENV', 'DEFAULT_PROPOSAL_TTL_DAYS', 'BATCH_TTL_DAYS_ENV', 'DEFAULT_BATCH_TTL_DAYS', 'GC_DAYS_ENV', 'DEFAULT_GC_DAYS', 'BATCH_SCHEMA', 'EVIDENCE_SCHEMA', 'BROKER_KEY_PREFIX', 'CORRECT_KEY_PREFIX', '_ACCEPT_ALL', '_REJECT_ALL', '_ACCEPT_PARTIAL_RE', 'SECRET_PATTERNS', 'secret_findings', 'scrub', '_PERMS', 'APPROVED_ANCHOR_SCHEMA', '_APPROVED_DIRNAME', 'ATTACHMENT_ANCHOR_SCHEMA', '_ATTACHMENT_ANCHOR_DIRNAME', 'ApprovedQueueUnsafe', 'ApprovedRefused', 'ReleaseRecordsUnreadable', 'ApprovedTooLarge', 'ApprovedKeyUnavailable', 'vm_visible_roots', 'MODE_HOST_PRIVATE', 'MODE_VM_READABLE', 'MODE_VM_WRITABLE', '_APPEND_LOCK_SECONDS', 'ATTACHMENT_HOLD_SCHEMA', 'RUN_MANIFEST_SCHEMA', 'RUN_ID_RE', '_RUN_NUMBER_RE', 'RUN_VALID', 'RUN_VALID_DEGRADED', 'RUN_INVALID', 'RUN_INCONCLUSIVE', 'RUN_VERDICTS', 'CLAIMABLE_VERDICTS', 'RUNS_MIGRATION_MARKER', 'MAX_RUN_DIGITS', '_LEDGER_GLOB', '_LEDGER_RUN_RE', '_LEDGER_ID_KEYS', '_LEDGER_DIGEST_KEYS', '_SHA256_RE', 'QUARANTINE_NO_LEDGER', 'QUARANTINE_NO_MANIFEST', 'PRODUCER_STAMP_KEYS', '_STRIPPED_CLAIM_KEYS', 'KIND_SUPERSEDE', 'VERSION_LINK_RUN_EVENT', 'BATCH_CAP_TOTAL', 'BATCH_SUBCAP_INGESTION', 'BATCH_SUBCAP_SUPERSEDE', '_MARKER_RANK_LABEL', '_CONSUME_JOURNAL', '_SIG_FAILED', '_NO_ANSWER', 'BEHAVIOUR_OBSERVATIONS', 'LEGACY_REJOIN_PREFIX', '_OVERRIDE_LINE_RE', 'HOLD_RECORD_SCHEMA', 'AUTOCAP_MIN_VOLUME_ENV', 'DEFAULT_AUTOCAP_MIN_VOLUME', 'AUTOCAP_MIN_LOWER_BOUND_ENV', 'DEFAULT_AUTOCAP_MIN_LOWER_BOUND', 'AUTOCAP_UNDO_HOURS_ENV', 'DEFAULT_AUTOCAP_UNDO_HOURS', '_UNPATTERNED', 'AUTOCAP_EXPLORATION_K_ENV', 'DEFAULT_AUTOCAP_EXPLORATION_K', 'AUTOCAP_WINDOW_DAYS_ENV', 'DEFAULT_AUTOCAP_WINDOW_DAYS', 'AUTOCAP_WINDOW_VERDICTS_ENV', 'DEFAULT_AUTOCAP_WINDOW_VERDICTS', 'AUTOCAP_BULK_MAX_BATCH_ENV', 'DEFAULT_AUTOCAP_BULK_MAX_BATCH', 'CATEGORY_UNCLASSIFIED', 'LANE_TEXT', 'LANE_ATTACHMENT', 'LANES', 'DISPOSITION_NEVER', 'DISPOSITION_PROPOSE', '_FORWARD_WRAPPER_RE', 'PATTERN_AUTOCAPTURE_STATUS', 'KEEPER_HORIZON_DAYS_ENV', 'DEFAULT_KEEPER_HORIZON_DAYS', 'INGEST_SWEEP_MAX_BYTES_ENV', 'DEFAULT_INGEST_SWEEP_MAX_BYTES', 'INGEST_SWEEP_DOWNLOADS_ENV', 'INGEST_SWEEP_SKEW_SECONDS', 'INGEST_SWEEP_SIZE_TOLERANCE', 'INGEST_SWEEP_RECENCY_ENV', 'DEFAULT_INGEST_SWEEP_RECENCY_SECONDS', 'BATCH_STALE_HOURS_ENV', 'DEFAULT_BATCH_STALE_HOURS']
