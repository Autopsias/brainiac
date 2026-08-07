#!/usr/bin/env python3
"""REP-03 - build the synthetic COS capture corpus fixture.

Nine real runs exist only as evidence on a laptop nobody else can read, so
REP-01 (the offline replay harness) and WIR-02 (the empty-body refusal guard)
need a corpus that needs no mailbox: hand-written, placeholder-only, and
written through the SAME ``cos_corpus.append_thread``/``close_run`` functions
a real run uses, so the fixture proves the writer/reader round-trips a
synthetic row with no special-casing, not just that some JSON parses.

Run to regenerate: ``python3 eval/fixtures/build_cos_corpus_synthetic.py``

Two files come out of one build, because the corpus row schema
(``cos_corpus.CORPUS_SCHEMA``) has no verdict field: it is evidence of what
a run READ, not a judgment about it (see the module docstring: "the corpus is
evidence; the replay harness re-runs a judgment over it"). Bolting a verdict
onto the row schema would be extending the format to serve one fixture, so
the expected verdicts live in a SEPARATE, plain JSON file keyed by the same
``conversation_id`` that joins a corpus row back to anything else:

  cos-corpus-synthetic.jsonl           - real corpus rows (CORPUS_SCHEMA + a
                                          close record), readable by
                                          ``cos_corpus.read_corpus`` unmodified
  cos-corpus-synthetic-verdicts.json   - {conversation_id: {expected_verdict,
                                          why}}, the correctness check REP-01
                                          runs its harness against

FINDING for the format (report, don't extend): ``cos_corpus`` rows carry no
verdict/label field by design, so any harness that wants ground truth needs a
side-channel keyed on ``conversation_id``. This builder is that side-channel,
not a schema change.

Placeholder-only content: Contoso and Northwind, invented people. No real
counterparty, project codename, or person appears here.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

RUN_ID = "2026-08-02-run999"
NOW = dt.datetime(2026, 8, 2, 6, 0, tzinfo=dt.timezone.utc)

# -- the rows, each with its expected verdict alongside -----------------------
# (conversation_id, kwargs for append_thread, expected_verdict, why)
_LONG_THREAD_BODY = "\n\n".join(
    f'> Reply {n} - Rhea Contoso <rhea@contoso.example>\n'
    f'On the Northwind master services agreement renewal: we are still '
    f'aligned on the Section {n} indemnity cap at 1.5x annual fees, but '
    f'Legal wants one more pass on the data-residency clause before we send '
    f'the redline back. Carrying this to reply {n + 1}.'
    for n in range(1, 23)
) + (
    '\n\n> Reply 23 - Niko Northwind <niko@northwind.example>\n'
    'Agreed on the indemnity cap. We accept the residency clause as drafted '
    'if Contoso confirms the Q4 renewal date stays October 1st. Please '
    'countersign and return by Friday so procurement can close the quarter.'
)

ROWS: list[tuple[str, dict, str, str]] = [
    (
        "<renewal-2026-08-01@contoso.example>",
        dict(
            text=(
                'Niko,\n\n'
                'Following our call, Contoso agrees to renew the Northwind '
                'master services agreement for another 12 months at the '
                'revised rate ($42,000/mo), effective October 1st. This '
                'supersedes the draft you sent in June; please countersign '
                'the attached and return by Friday so we can close the '
                'quarter.\n\n'
                'Best,\nRhea Contoso\nContoso Procurement'
            ),
            sender="Rhea Contoso <rhea@contoso.example>",
            sent="2026-08-01T14:05:00Z",
            subject="RE: Northwind MSA renewal - signature needed",
            read_lane="chrome-plugin",
            body_opened=True,
        ),
        "candidate",
        "substantive, decision-bearing: a contract renewal with a rate, an "
        "effective date, and an action (countersign) - the reference case "
        "for a genuine candidate.",
    ),
    (
        "<newsletter-2026-08-01@northwind.example>",
        dict(
            text=(
                'This Week at Northwind: five tips for a faster Q3 close, '
                'our new office plants, and a recap of last month\'s town '
                'hall. Unsubscribe at any time.'
            ),
            sender="Northwind Weekly <newsletter@northwind.example>",
            sent="2026-08-01T09:00:00Z",
            subject="This Week at Northwind - Issue 214",
            read_lane="chrome-plugin",
            body_opened=True,
        ),
        "not_candidate",
        "newsletter - no decision, no thread-specific content, addressed to "
        "a distribution list.",
    ),
    (
        "<scheduling-2026-08-01@contoso.example>",
        dict(
            text=(
                'Does 3pm Thursday work for you instead? I have a conflict '
                'at 2. Either room is fine.'
            ),
            sender="Avery Contoso <avery@contoso.example>",
            sent="2026-08-01T11:20:00Z",
            subject="RE: quick sync?",
            read_lane="chrome-plugin",
            body_opened=True,
        ),
        "not_candidate",
        "scheduling chatter - a meeting-time negotiation with nothing else "
        "in the body.",
    ),
    (
        "<notification-2026-08-01@billing.example>",
        dict(
            text=(
                'Your invoice #88213 for $1,240.00 has been generated and is '
                'available in your account. This is an automated message; '
                'please do not reply.'
            ),
            sender="Billing <no-reply@billing.example>",
            sent="2026-08-01T03:00:00Z",
            subject="Invoice #88213 available",
            read_lane="chrome-plugin",
            body_opened=True,
        ),
        "not_candidate",
        "automated notification - a no-reply system message, not a human "
        "correspondence.",
    ),
    (
        "<empty-body-2026-08-01@contoso.example>",
        dict(
            text="",
            sender="Rhea Contoso <rhea@contoso.example>",
            sent="2026-08-01T15:00:00Z",
            subject="RE: Northwind MSA renewal - signature needed",
            read_lane="chrome-plugin",
            body_opened=False,
        ),
        "refuse",
        "LOAD-BEARING for WIR-02: an empty body with body_opened=False. The "
        "judge must REFUSE this row (missing input), never emit a verdict "
        "for it; this is what proves the body-pass precondition actually "
        "refuses rather than silently judging on the subject alone.",
    ),
    (
        "<long-thread-2026-07-15@contoso.example>",
        dict(
            text=_LONG_THREAD_BODY,
            sender="Rhea Contoso <rhea@contoso.example>",
            sent="2026-08-01T16:40:00Z",
            subject="RE: Northwind MSA renewal - redline history",
            read_lane="chrome-plugin",
            body_opened=True,
        ),
        "candidate",
        "unusually long thread (23 replies, ~"
        f"{len(_LONG_THREAD_BODY)} chars) to exercise the extraction "
        "window; the substantive decision is in the LAST two replies, past "
        "where a naive head-only extraction would stop.",
    ),
    (
        "<urgent-subject-2026-08-01@contoso.example>",
        dict(
            text=(
                'Hey - just circling back on this, no rush. Let\'s grab '
                '15 min whenever works, nothing pressing on my end.'
            ),
            sender="Avery Contoso <avery@contoso.example>",
            sent="2026-08-01T13:10:00Z",
            subject="URGENT: Contract Terms Review - action needed today",
            read_lane="chrome-plugin",
            body_opened=True,
        ),
        "not_candidate",
        "subject looks important, body is empty of substance - the case a "
        "subject-only judge gets wrong; the body must win over the subject "
        "line.",
    ),
]


def build(out_dir: Path) -> tuple[Path, Path]:
    """Write the corpus through the REAL writer (``append_thread``/
    ``close_run``), then copy the resulting file into ``out_dir``.

    A scratch vault + a scratch ``$BRAIN_INDEX_DIR`` (both under a tempdir,
    proven off each other so ``proven_off_mount`` passes) stand in for a real
    vault for exactly as long as the build takes. The corpus never lives
    under the real per-user app-data dir, and the committed fixture is a
    plain copy of what the writer produced, not a hand-built lookalike.
    """
    import importlib
    import os
    import shutil
    import tempfile

    corpus_path = out_dir / "cos-corpus-synthetic.jsonl"
    verdicts_path = out_dir / "cos-corpus-synthetic-verdicts.json"

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault = tmp / "vault"
        (vault / "brain" / "resources").mkdir(parents=True)
        (vault / "raw").mkdir(parents=True)
        old_vault = os.environ.get("BRAIN_VAULT")
        old_index = os.environ.get("BRAIN_INDEX_DIR")
        old_role = os.environ.get("BRAIN_ROLE")
        os.environ["BRAIN_VAULT"] = str(vault)
        os.environ["BRAIN_INDEX_DIR"] = str(tmp / "hostidx")
        os.environ.pop("BRAIN_ROLE", None)
        try:
            # Freshly imported AFTER the env vars are set: config/cos_corpus
            # cache nothing module-level that would survive a reimport, but
            # importing late keeps this builder honest about depending on
            # the env, not on import order.
            cos_corpus = importlib.import_module("brain.cos_corpus")
            importlib.reload(cos_corpus)

            for cid, kwargs, _verdict, _why in ROWS:
                cos_corpus.append_thread(
                    vault, RUN_ID, conversation_id=cid, now=NOW, **kwargs)
            cos_corpus.close_run(vault, RUN_ID, now=NOW)

            written = cos_corpus.corpus_path(vault, RUN_ID)
            out_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(written, corpus_path)
        finally:
            for key, old in (("BRAIN_VAULT", old_vault),
                             ("BRAIN_INDEX_DIR", old_index),
                             ("BRAIN_ROLE", old_role)):
                if old is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old

    verdicts = {
        cid: {"expected_verdict": verdict, "why": why}
        for cid, _kwargs, verdict, why in ROWS
    }
    with open(verdicts_path, "w", encoding="utf-8") as fh:
        json.dump(verdicts, fh, indent=2, sort_keys=True)
        fh.write("\n")

    return corpus_path, verdicts_path


if __name__ == "__main__":
    corpus_path, verdicts_path = build(HERE)
    print(f"wrote {len(ROWS)} rows -> {corpus_path}")
    print(f"wrote verdicts -> {verdicts_path}")
