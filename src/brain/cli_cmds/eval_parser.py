"""Register retrieval evaluation commands."""

from __future__ import annotations


def _add_eval(sub) -> None:
    sp = sub.add_parser(
        "eval",
        help="host-only retrieval evaluation utilities (real-query replay never writes the ledger)",
        description="Host-only retrieval evaluation. Real-query replay reads a private query-log export and never appends capture records. It has no target qrels: matching live-index fingerprints are reported as vault_same ranking/configuration signals; changed fingerprints are drift_or_mixed and remain report-only.",
    )
    eval_sub = sp.add_subparsers(dest="eval_cmd", required=True)
    replay = eval_sub.add_parser(
        "replay",
        help="replay a host query-log export: stability, overlap, rank movement, and latency",
        description="Replay a private host query-log JSONL export without writing any new capture records. Reports Jaccard@k, top-1 stability, rank movement, candidate-digest presence, and latency delta, separated into vault_same and drift_or_mixed fingerprint groups. The log has no target qrels. Thresholds are optional and are evaluated only over vault_same records; an empty comparable subset exits successfully.",
    )
    replay.add_argument(
        "--against", required=True, help="host query-log JSONL month file to replay"
    )
    replay.add_argument(
        "--fail-under-top1",
        type=float,
        default=None,
        help="fail only if vault_same top-1 stability is below [0,1]; drift_or_mixed rows are report-only",
    )
    replay.add_argument(
        "--fail-under-jaccard",
        type=float,
        default=None,
        help="fail only if vault_same Jaccard@k is below [0,1]; the log has no target qrels",
    )
    replay.add_argument("--json", action="store_true")


def add_parser(sub) -> None:
    _add_eval(sub)
