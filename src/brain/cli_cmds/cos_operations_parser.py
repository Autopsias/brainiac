"""Register COS broker commands."""

from __future__ import annotations

from .. import classification as cls


def _add_cos_broker(sub) -> None:
    sp = sub.add_parser(
        "cos-broker",
        help="HOST broker step (also wired into `brain maintain`): claim + validate proposal drops, expire/requeue, consume owner-inbox answers (only ACCEPTED candidates move to capture-inbox for signing), release due holds, enqueue at most one signed batch, GC.",
    )
    sp.add_argument("--json", action="store_true")


def _add_cos_correct(sub) -> None:
    sp = sub.add_parser(
        "cos-correct",
        help="HOST-only correction of record: append ONE correction_events row (round, msg_key, corrected_bucket, corrected_tier). Append-only; rejects unknown (un-ledgered) and duplicate keys.",
    )
    sp.add_argument("--round", type=int, required=True, dest="round_")
    sp.add_argument("--msg-key", required=True)
    sp.add_argument("--bucket", required=True, help="corrected_bucket")
    sp.add_argument("--tier", required=True, help="corrected_tier")
    sp.add_argument("--json", action="store_true")


def _add_cos_evidence(sub) -> None:
    sp = sub.add_parser(
        "cos-evidence",
        help="HOST-only trust-gate evidence signer: `sign` writes a bundle + Ed25519-signed versioned manifest (bundle/model version, snapshot generation, dataset window, source-ledger hash) under the host-private evidence dir; `verify` re-checks signature + hashes (a stale/edited JSON fails).",
    )
    sp.add_argument("action", choices=("sign", "verify"))
    sp.add_argument("--bundle-version", default=None)
    sp.add_argument("--model-version", default=None)
    sp.add_argument("--dataset-window", default=None)
    sp.add_argument(
        "--file",
        action="append",
        default=[],
        dest="files",
        help="payload file to include (repeatable)",
    )
    sp.add_argument("--name", default="evidence")
    sp.add_argument("--dir", default=None, help="[verify] bundle dir to verify")
    sp.add_argument("--json", action="store_true")


def _add_cos_priority_map(sub) -> None:
    sp = sub.add_parser(
        "cos-priority-map",
        help="HOST-only: generate the VM-readable shared/priority-map.md from type:person/company notes via a host-produced filtered projection (default tier policy: the FULL vault, NOT capped to Internal) + owner overrides from the overlay cos/ category.",
    )
    sp.add_argument("--max-tier", default=None, choices=cls.TIERS)
    sp.add_argument("--json", action="store_true")


def _add_cos_report(sub) -> None:
    sp = sub.add_parser(
        "cos-report",
        help="HOST-only: shadow-mode calibration report — rounds completed, per-bucket precision, from the verdict-drop shadow ledger x correction_events (calibration = reduce(verdicts, corrections)).",
    )
    sp.add_argument("--json", action="store_true")


def _add_cos_ingest_sweep(sub) -> None:
    sp = sub.add_parser(
        "cos-ingest-sweep",
        help="HOST-only (also wired into `brain maintain`): claim VM ingest-manifest lines (drop/ingest-manifest/) and MOVE exact-filename matches from an explicitly configured dedicated host-only staging dir ($BRAIN_COS_DOWNLOADS_DIR) into <vault>/inbox/ for normal signed ingest. Disabled when unset; shared ~/Downloads and symlinked dirs are refused. Basename-only filenames and file symlinks are refused, 200MB cap, append-only claims (idempotent); files the manifest does not name are never touched.",
    )
    sp.add_argument(
        "--downloads-dir",
        default=None,
        help="dedicated host-only download staging dir to sweep (default: $BRAIN_COS_DOWNLOADS_DIR; unset disables)",
    )
    sp.add_argument(
        "--dry-run",
        action="store_true",
        help="report matches without moving or claiming anything",
    )
    sp.add_argument("--json", action="store_true")


def _add_cos_hold(sub) -> None:
    sp = sub.add_parser(
        "cos-hold",
        help="HOST-only auto-capture hold store: `add` parks an item UNSIGNED until --not-before; a due item enters capture-inbox (then the signed drain) only after expiry. `cancel` is atomic against a concurrent release. `release-due` is also run by the broker fold.",
    )
    sp.add_argument("action", choices=("add", "list", "cancel", "release-due"))
    sp.add_argument("--id", default=None)
    sp.add_argument(
        "--not-before",
        default=None,
        help="[add] ISO timestamp before which the item must NOT be signed",
    )
    sp.add_argument(
        "--content", default=None, help="[add] content (default: read stdin)"
    )
    sp.add_argument("--json", action="store_true")


def _add_cos_spine(sub) -> None:
    sp = sub.add_parser(
        "cos-spine",
        help="HOST-only commitment spine (SP-01/SP-02): `record` appends ONE event (created/rescheduled/completed/cancelled/corrected/reopened) — commitment-kind ingestion candidates are recorded automatically on owner acceptance; this is for the other two named sources (calendar follow-ups, drafts ledger). `radar` prints late/at-risk open commitments. `render` regenerates the VM-readable shared/spine-summary.md projection (also run every broker fold). `grounding-pack` regenerates the BAK-01 shared/grounding-pack.md projection — Internal-safe POINTERS to documents above the VM leg's egress ceiling, from the host-private host/grounding-pack-ids.txt list (also run every broker fold).",
    )
    sp.add_argument("action", choices=("record", "radar", "render", "grounding-pack"))
    sp.add_argument(
        "--event",
        default="created",
        choices=(
            "created",
            "rescheduled",
            "completed",
            "cancelled",
            "corrected",
            "reopened",
        ),
    )
    sp.add_argument(
        "--id",
        dest="commitment_id",
        default=None,
        help="[record] existing commitment id (any event but 'created')",
    )
    sp.add_argument("--direction", default=None, choices=("owed_by_me", "owed_to_me"))
    sp.add_argument("--counterparty", default=None)
    sp.add_argument("--text", default=None)
    sp.add_argument("--topic", default=None)
    sp.add_argument("--due", default=None, help="ISO timestamp")
    sp.add_argument("--source-ref", default=None)
    sp.add_argument("--note", default=None)
    sp.add_argument("--json", action="store_true")


def _add_cos_standing_approval(sub) -> None:
    sp = sub.add_parser(
        "cos-standing-approval",
        help="HOST-only: record (or clear) the owner's STANDING answer to every COS ingestion batch. With one recorded, each batch is still built, signed, enqueued and consumed with its per-candidate content CAS intact — only the keystroke is standing. Stored host-private, off every VM-visible root. No action shows the current state.",
    )
    sp.add_argument(
        "--accept-all", action="store_true",
        help="record a standing ACCEPT for every future batch (requires --reason)")
    sp.add_argument(
        "--clear", action="store_true",
        help="remove the standing answer and restore the manual per-batch gate")
    sp.add_argument("--reason", default=None,
                    help="the owner's words, stored with the record")
    sp.add_argument("--json", action="store_true")


def add_parser(sub) -> None:
    _add_cos_standing_approval(sub)
    _add_cos_broker(sub)
    _add_cos_correct(sub)
    _add_cos_evidence(sub)
    _add_cos_priority_map(sub)
    _add_cos_report(sub)
    _add_cos_ingest_sweep(sub)
    _add_cos_hold(sub)
    _add_cos_spine(sub)
