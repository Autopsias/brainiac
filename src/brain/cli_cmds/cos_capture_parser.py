"""Register COS capture commands."""

from __future__ import annotations


def _add_cos_propose(sub) -> None:
    sp = sub.add_parser(
        "cos-propose",
        help="VM-ALLOWED: drop ONE unsigned COS proposal into the proposal-drop dir (which `brain sync` NEVER reads). Only the host broker's validate -> owner-inbox-batch -> accept flow can move it toward the signed write path. --kind correction drops a correction request (JSON: round, msg_key, corrected_bucket, corrected_tier) into verdict-drop/ instead.",
    )
    sp.add_argument(
        "--id", default=None, help="note id (default: frontmatter or content hash)"
    )
    sp.add_argument("--kind", default="proposal", choices=("proposal", "correction"))
    sp.add_argument("--content", default=None, help="content (default: read stdin)")
    sp.add_argument("--json", action="store_true")


def _add_cos_run_begin(sub) -> None:
    sp = sub.add_parser(
        "cos-run-begin",
        help="HOST-ONLY: freeze the run manifest for a COS run BEFORE it starts (run id, executing SKILL.md path + digest, bundle + extraction-rules versions, expected artifacts). Every later claim stamps candidates from THIS record, never from whatever skill happens to be deployed at claim time.",
    )
    sp.add_argument(
        "--run-id",
        default=None,
        help="<YYYY-MM-DD>-run<N> (default: one past the highest run number on disk)",
    )
    sp.add_argument(
        "--lane",
        default=None,
        choices=("codex-automation", "cowork-desktop"),
        help="assert which surface executes (default: resolve it; an unresolvable lane REFUSES rather than guesses)",
    )
    sp.add_argument(
        "--skill", default=None, help="assert the executing SKILL.md path outright"
    )
    sp.add_argument("--attended", action="store_true",
                    help="a human is about to approve this run's plan and "
                         "watch it apply. REFUSES a dirty or non-git working "
                         "tree: the manifest's `git_commit` is the record of "
                         "WHICH CODE he approved, and it says nothing if "
                         "uncommitted edits sat beside it.")
    sp.add_argument("--json", action="store_true")


def _add_cos_corpus_check(sub) -> None:
    sp = sub.add_parser(
        "cos-corpus-check",
        help="HOST-ONLY (WIR-02): the gate a run passes BEFORE it judges. Reports how many of this run's captured threads carry body text, and REFUSES (exit 3) when none does — the judge's input is the body, so no bodies is a MISSING INPUT, never a quiet night. Some bodyless rows are normal and pass.",
    )
    sp.add_argument("--run-id", required=True, help="<YYYY-MM-DD>-run<N>")
    sp.add_argument("--json", action="store_true")


def _add_cos_corpus_append(sub) -> None:
    sp = sub.add_parser(
        "cos-corpus-append",
        help="HOST-ONLY (WIR-01): save the text a run just READ, as it reads it. ONE row per in-scope thread: --conversation-id with the extracted message text on stdin for a thread whose body was opened, or --bodyless <id>... in one call for the threads that were enumerated and never opened. The ledger keeps the verdict; this keeps the input the verdict was made from.",
    )
    sp.add_argument("--run-id", required=True, help="<YYYY-MM-DD>-run<N>")
    sp.add_argument(
        "--conversation-id",
        default=None,
        help="the ONE thread this text belongs to — the join key back to that run's ingestion ledger",
    )
    sp.add_argument(
        "--bodyless",
        nargs="+",
        default=None,
        metavar="CONV_ID",
        help="conversation ids enumerated but NOT opened (unread, over-cap, no body access on the lane, page not visible) — one empty row each",
    )
    sp.add_argument(
        "--text", default=None, help="the extracted message text (default: read stdin)"
    )
    sp.add_argument("--sender", default=None)
    sp.add_argument("--sent", default=None, help="ISO date or datetime")
    sp.add_argument("--subject", default=None)
    sp.add_argument(
        "--read-lane",
        default=None,
        help="the elected observation lane, as the ledger names it",
    )
    sp.add_argument("--json", action="store_true")


def _add_cos_corpus_close(sub) -> None:
    sp = sub.add_parser(
        "cos-corpus-close",
        help="HOST-ONLY (WIR-01): close this run's corpus (write-once from here, and only a CLOSED corpus is ever deleted by retention). A closed corpus with 0 rows is a quiet night; an unclosed one is a capture stage that died.",
    )
    sp.add_argument("--run-id", required=True, help="<YYYY-MM-DD>-run<N>")
    sp.add_argument("--json", action="store_true")


def _add_cos_corpus_reopen(sub) -> None:
    sp = sub.add_parser(
        "cos-corpus-reopen",
        help="HOST-ONLY: retract a close that certified ZERO rows, after a lane failure turned out to be transient (run 68). A close carrying rows is final and is refused — capture the rest of the night under a new run id.",
    )
    sp.add_argument("--run-id", required=True, help="<YYYY-MM-DD>-run<N>")
    sp.add_argument("--json", action="store_true")


def add_parser(sub) -> None:
    _add_cos_propose(sub)
    _add_cos_run_begin(sub)
    _add_cos_corpus_check(sub)
    _add_cos_corpus_append(sub)
    _add_cos_corpus_close(sub)
    _add_cos_corpus_reopen(sub)
