"""Register index-storage commands."""

from __future__ import annotations

from .. import classification as cls


def _add_draft_capture(sub) -> None:
    sp = sub.add_parser(
        "draft-capture",
        help="VM-side capture: stage a candidate note as a plain DRAFT (no sign, no index, no WAL) for the host to drain later",
    )
    sp.add_argument(
        "--id", default=None, help="note id (default: from frontmatter or content hash)"
    )
    sp.add_argument(
        "--source",
        action="store_true",
        help="stage as a raw/ source (vs a brain/ note)",
    )
    sp.add_argument("--content", default=None, help="note text (default: read stdin)")
    sp.add_argument("--json", action="store_true")


def _add_rebuild(sub) -> None:
    sp = sub.add_parser(
        "rebuild", help="rebuild the derived index from vault/ (always safe)"
    )
    sp.add_argument("--json", action="store_true")
    sp.add_argument(
        "--progress",
        action="store_true",
        help="force stderr progress lines even when stderr isn't a TTY (same as BRAIN_PROGRESS=1)",
    )


def _add_warmup(sub) -> None:
    sp = sub.add_parser(
        "warmup",
        help="HOST-ONLY (S02/CS-01): resolve + download the live embedding model now (stderr progress), instead of deferring to the first real semantic search. Never on role=vm — the VM model is pre-staged by the host and HuggingFace is off its egress allowlist. Does not rebuild the index; run `brain sync` after if `brain status` reported embedder: pending.",
    )
    sp.add_argument("--json", action="store_true")
    sp.add_argument(
        "--progress",
        action="store_true",
        help="force stderr progress lines even when stderr isn't a TTY (same as BRAIN_PROGRESS=1)",
    )


def _add_sync(sub) -> None:
    sp = sub.add_parser(
        "sync",
        help="incremental upsert by path+hash + delete-propagation (no full rebuild); drains capture drafts first (host)",
    )
    sp.add_argument(
        "--no-drain",
        action="store_true",
        help="skip the host capture drain (read-only/VM leg)",
    )
    sp.add_argument(
        "--publish",
        action="store_true",
        help="republish the read-only snapshot after reconcile so the VM's next read sees the just-committed note (closes the capture loop)",
    )
    sp.add_argument(
        "--progress",
        action="store_true",
        help="force stderr progress lines even when stderr isn't a TTY (same as BRAIN_PROGRESS=1)",
    )
    sp.add_argument("--json", action="store_true")


def _add_snapshot(sub) -> None:
    sp = sub.add_parser(
        "snapshot", help="publish a read-only, generation-stamped index snapshot (host)"
    )
    sp.add_argument(
        "--dest", default=None, help="snapshot dir (default: vault/.brain/snapshot)"
    )
    sp.add_argument("--json", action="store_true")


def _add_restore_index(sub) -> None:
    sp = sub.add_parser(
        "restore-index",
        help="fast-recover the live index from the published snapshot (host) — seconds, no re-embed; use instead of `rebuild` when the index is corrupt/empty",
    )
    sp.add_argument(
        "--force",
        action="store_true",
        help="restore even if the live index has MORE notes than the snapshot (the snapshot is older — you may lose notes)",
    )
    sp.add_argument(
        "--dry-run", action="store_true", help="report what would happen; write nothing"
    )
    sp.add_argument("--json", action="store_true")


def _add_status(sub) -> None:
    sp = sub.add_parser(
        "status", help="report index stats + read-only snapshot generation/age"
    )
    sp.add_argument("--snapshot-dest", default=None)
    sp.add_argument("--json", action="store_true")


def _add_project(sub) -> None:
    sp = sub.add_parser(
        "project",
        help="write a classification-filtered copy of the vault (real containment)",
    )
    sp.add_argument(
        "--dest", required=True, help="destination directory (recreated each run)"
    )
    sp.add_argument("--max-tier", default="Internal", choices=cls.TIERS)
    sp.add_argument("--json", action="store_true")


def add_parser(sub) -> None:
    _add_draft_capture(sub)
    _add_rebuild(sub)
    _add_warmup(sub)
    _add_sync(sub)
    _add_snapshot(sub)
    _add_restore_index(sub)
    _add_status(sub)
    _add_project(sub)
