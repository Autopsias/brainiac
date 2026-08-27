"""Register signed-ingest commands."""

from __future__ import annotations

from .. import classification as cls


def _add_ingest(sub) -> None:
    sp = sub.add_parser(
        "ingest",
        help="host-broker: drain <vault>/inbox/ — extract to Markdown, archive originals immutably, commit through the signed write path (ING-01)",
    )
    sp.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would happen; no moves, no writes, no signing",
    )
    sp.add_argument("--json", action="store_true")


def _add_ingest_transcript(sub) -> None:
    sp = sub.add_parser(
        "ingest-transcript",
        help="host-broker: promote one transcript .md into raw/ with explicit provenance (ING-04) — origin is a source audio/video path, or 'verbal'",
    )
    sp.add_argument("path", help="path to the transcript .md file")
    sp.add_argument(
        "--origin",
        required=True,
        help="source audio/video file path, or the literal string 'verbal'",
    )
    sp.add_argument(
        "--language",
        default=None,
        help="ISO 639-1 code (default: detected from filename)",
    )
    sp.add_argument(
        "--document-date",
        default=None,
        dest="document_date",
        help="YYYY-MM-DD the underlying meeting/recording happened (optional)",
    )
    sp.add_argument("--classification", default="Internal", choices=cls.TIERS)
    sp.add_argument("--json", action="store_true")


def _add_write(sub) -> None:
    sp = sub.add_parser(
        "write", help="host-broker: write a note (audited, fails closed)"
    )
    sp.add_argument("relpath")
    sp.add_argument("--content", default=None, help="content (default: read stdin)")
    sp.add_argument("--reason", default="")
    sp.add_argument(
        "--untrusted-author",
        action="store_true",
        help=(
            "the note text was written by an untrusted leg (a confined model "
            "session, a VM): apply the draft-drain's controls — safe id, no "
            "forged host-only provenance, trust marker — before signing"
        ),
    )
    sp.add_argument("--json", action="store_true")


def _add_audit_key(sub) -> None:
    sp = sub.add_parser(
        "audit-key",
        help="host-broker: provision the audit signing key (create-if-absent, NEVER rotates)",
    )
    sp.add_argument("--json", action="store_true")


def _add_verify_audit(sub) -> None:
    sp = sub.add_parser("verify-audit", help="verify the Ed25519 audit chain")
    sp.add_argument(
        "--check-content",
        action="store_true",
        help="also flag notes whose current bytes differ from the last signed content hash (detects post-commit edits)",
    )
    sp.add_argument(
        "--pubkey",
        help="verify against this PUBLIC key PEM file instead of the resolved signing key — external verification that never touches the private key (VULN-3388)",
    )
    sp.add_argument("--json", action="store_true")


def _add_audit_pubkey(sub) -> None:
    sp = sub.add_parser(
        "audit-pubkey",
        help="host-broker: export the audit PUBLIC key for external log verification (VULN-3388)",
    )
    sp.add_argument(
        "--out",
        help="write the PEM to FILE instead of stdout (prints the sha256 fingerprint either way)",
    )
    sp.add_argument("--json", action="store_true")


def _add_vm_egress_tier(sub) -> None:
    sp = sub.add_parser(
        "vm-egress-tier",
        help="host-broker: set the HOST-SIGNED egress ceiling a role=vm session may not exceed (VULN-3386)",
    )
    sp.add_argument(
        "tier", nargs="?", default=None,
        help="tier to sign (Public|Internal|Confidential|Restricted|MNPI); omit to remove and fall back to Internal",
    )
    sp.add_argument("--json", action="store_true")


def _add_anchor(sub) -> None:
    sp = sub.add_parser(
        "anchor", help="publish the signed chain head OFF-HOST (host; SEC-03)"
    )
    sp.add_argument(
        "--anchor-dir", required=True, help="off-host append-only anchor dir"
    )
    sp.add_argument("--json", action="store_true")


def _add_verify_anchor(sub) -> None:
    sp = sub.add_parser(
        "verify-anchor",
        help="verify the live chain vs the off-host anchor (detect rewrite)",
    )
    sp.add_argument("--anchor-dir", required=True)
    sp.add_argument("--json", action="store_true")


def _add_backup(sub) -> None:
    sp = sub.add_parser(
        "backup",
        help="encrypted off-device backup of the Markdown truth (host; SEC-03)",
    )
    sp.add_argument("--dest", required=True, help="off-device destination dir")
    sp.add_argument(
        "--no-encrypt",
        action="store_true",
        help="write a PLAINTEXT archive (discouraged off-device; default encrypts)",
    )
    sp.add_argument("--json", action="store_true")


def _add_restore(sub) -> None:
    sp = sub.add_parser("restore", help="restore (decrypt) a backup archive (host)")
    sp.add_argument("--archive", required=True)
    sp.add_argument("--dest", required=True, help="restore destination dir")
    sp.add_argument("--json", action="store_true")


def add_parser(sub) -> None:
    _add_ingest(sub)
    _add_ingest_transcript(sub)
    _add_write(sub)
    _add_audit_key(sub)
    _add_audit_pubkey(sub)
    _add_vm_egress_tier(sub)
    _add_verify_audit(sub)
    _add_anchor(sub)
    _add_verify_anchor(sub)
    _add_backup(sub)
    _add_restore(sub)
