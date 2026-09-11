"""The accounting and artifact writers of `cos_driver` — corpus rows, contract inputs, reports

Moved verbatim out of `cos_driver` (batch-2 drain) and re-imported by it, so
every name keeps its `cos_driver` module path; the parent's night orchestration
calls these through its own globals exactly as before, so a test that
monkeypatches one on `cos_driver` still steers the callers.
"""
from __future__ import annotations

import datetime as _dt  # noqa: F401
import hashlib as hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from brain import cos_chips  # noqa: E402
from cos_driver_draw import (  # noqa: E402
    CHIP_TIER as CHIP_TIER, _tier, _tier_source as _tier_source, conversations)
from cos_driver_transport import (  # noqa: E402
    BODY_BUDGET, BODY_OPEN_CAP, READ_LANE, short as short)

CONTRACT = Path(__file__).resolve().parent / "cos_contract.py"

#: `cos_contract.py` closed vocabulary. Nothing here is a judgment: a read-only
#: night archived nothing and drafted nothing, so every enumerated conversation
#: is still resident and undrafted.
BUCKET_RESIDENT = "held_non_drafted"


# ---------------------------------------------------------------------------
# accounting: a PURE function of the capture
# ---------------------------------------------------------------------------

def body_open_succeeded(b: dict[str, Any] | None) -> bool:
    """Did this body open actually LAND? ONE definition, read from the verifier.

    TWO SPELLINGS OF ONE CONCEPT is what this closes. The driver counted any
    extraction above zero characters as an open, while `cos_runverify` fails
    the run over any extraction at or below `_EMPTY_SHELL_CHARS` — the bare
    `<origin>/mail/` shell OWA drops a tab to when a conversation will not
    deep-link, folder and id gone. So a refused open was banked as a landed
    one, and the SAME thread invalidated two runs in a row: run162 and run164
    both failed `body_pass` on conversation `…grEtBSrkvbvcCz0ATswXo=`, 30
    characters, `body_opened: true`. An INVALID run never permits claiming, so
    one unopenable email was quarantining every ingestion candidate the night
    produced — 12 on each of those two runs.

    The threshold is IMPORTED, never restated. A second copy of the number is
    the same defect wearing the fix's clothes; the import is local because
    `tools/` reaches `brain` through the run's PYTHONPATH, exactly as
    `cos_corpus` is reached below.
    """
    from brain.cos_runverify_checks import (  # noqa: PLC0415
        _EMPTY_SHELL_CHARS)
    return bool(b and b.get("ok")
                and int(b.get("body_chars") or 0) > _EMPTY_SHELL_CHARS)


def carries_ingest_mark(categories: Any) -> bool:
    """Does this thread already carry `Brainiac · Ingested`?

    A BOOLEAN, NEVER THE CATEGORY LIST. The planner needs exactly one fact, so
    it does not re-write a mark that is already there and burn a cap slot on a
    no-op the page half refuses anyway. Recording the whole list instead would
    put the OWNER'S own category names into a ledger the model reads — a wider
    egress than the question asks for.
    """
    return cos_chips.CHIP_INGESTED in (categories or [])


def open_outcome(b: dict[str, Any] | None, opened: bool) -> str | None:
    """Why does this row carry no body? A DRIVER FACT, never a judgment.

    `mechanical_disposition` had exactly two states to work from — opened, or
    not opened — so every unopened row that was not category-excluded and not
    unread fell through to `over-cap`, the word for a row the cap NEVER DREW.
    Measured on run166: conversation `…grEtBSrkvbvcCz0ATswXo=` was drawn as
    P1, its open landed on the 30-character bare shell, and the ledger filed
    it `over-cap`. `body_order` then read a P1 as starved behind opened `other`
    rows and scored the run INVALID — a second wrong verdict from the same
    missing word. `body_open_succeeded` above made the row honest about the
    open; this makes it honest about the reason.

    `None` means the cap really is the reason: the row was never drawn, so
    there is no attempt to describe.
    """
    if opened or b is None:
        return None
    return "shell" if b.get("ok") else "error"


def open_error(b: dict[str, Any] | None,
               opened: bool) -> dict[str, Any] | None:
    """WHAT the refused open answered — not merely THAT it was refused.

    `open_outcome` above records the WORD (`error` or `shell`), and the page
    has carried the cause beside it all along: the Exchange `ResponseCode`, the
    HTTP `status`, and the caught exception text (`fetchBody`'s reject path in
    `cos_driver_page.js`). Nothing ever read them. So eleven nights of ledgers
    say `error` and not one of them says why, and the ledgers cannot tell a
    transient refusal from a permanent one — which is exactly what a retry
    needs to know. Measured 2026-09-03: seven threads held
    `no-body-access-on-lane`, five of them for eleven consecutive nights, with
    no recorded cause on any of the rows.

    `None` when the open landed, so a healthy row grows no field.
    """
    if opened or not b:
        return None
    # `item_class`/`retry_item_class` say WHAT the row is (2026-09-04). The
    # eight fields above describe the fetch; none of them distinguishes a
    # message whose body the lane cannot reach from an item that has no message
    # body to reach. Without it the seven standing rows can only be guessed at
    # from their subjects.
    out = {k: b.get(k) for k in ("code", "status", "error", "item_class",
                                 "retry_status", "retry_code", "retry_error",
                                 "retry_shape", "retry_chars",
                                 "retry_item_class")
           if b.get(k) not in (None, "")}
    return out or None


def body_open_fields(b: dict[str, Any] | None, opened: bool,
                     seq: int | None) -> dict[str, Any]:
    """The four facts about THIS row's body open, as one group.

    Grouped when `body_open_error` joined them (2026-09-03): they are read
    together, they are meaningless apart, and `_ledger_row` is a flat literal
    that had no room for a fourth. `body_chars` is what landed, `seq` the order
    it landed in, `outcome` the word for a refusal, `error` what the server
    actually answered.
    """
    return {
        "body_chars": int(b.get("body_chars") or 0) if b else 0,
        "body_open_seq": seq,
        "body_open_outcome": open_outcome(b, opened),
        "body_open_error": open_error(b, opened),
    }


def _opened_sequence(capture: dict[str, Any],
                     bodies: dict[str, Any]) -> dict[str, int]:
    """Draw order of the bodies that actually landed (see `body_open_succeeded`)."""
    opened_seq: dict[str, int] = {}
    seq = 0
    for d in capture.get("draw", []):
        b = bodies.get(d["convId"])
        if body_open_succeeded(b):
            seq += 1
            opened_seq[d["convId"]] = seq
    return opened_seq


#: The row builder, in its own module for the parent's 500-LOC bound.
#: Re-imported so callers and tests keep reaching it at this module path.
from cos_driver_ledger_row import _ledger_row  # noqa: E402,F401


def build_accounting(capture: dict[str, Any], *, run_id: str,
                     bundle_version: str, rules_version: str,
                     enumerated_at: str,
                     gate_excluded: set[str] | frozenset[str] = frozenset(),
                     cap: int | None = None, read_never: bool = False,
                     never_ids: set[str] | frozenset[str] = frozenset()
                     ) -> dict[str, Any]:
    """Ledger rows + counters, computed from the capture and nothing else.

    JUDGMENT SLOTS ARE `None`, DELIBERATELY AND VISIBLY. `disposition`,
    `held_reason`, `category`, `verdict` and `dedup_check` are the judge's
    (s03). The driver writing a plausible value into any of them is the exact
    defect this rebuild exists to remove: run 106 coined `no-new-substance` and
    15 rows fell out of every total; run 108 coined
    `no-substance-or-already-represented` and the one check written to score
    substance verdicts passed reporting there were none.
    """
    convs = conversations(capture["enumeration"].get("items", []))
    bodies = {b["conv_id"]: b for b in capture.get("bodies", [])}
    opened_seq = _opened_sequence(capture, bodies)

    rows = [_ledger_row(c, bodies=bodies, opened_seq=opened_seq, run_id=run_id,
                        bundle_version=bundle_version,
                        rules_version=rules_version,
                        enumerated_at=enumerated_at,
                        gate_excluded=gate_excluded, cap=cap,
                        read_never=read_never, never_ids=never_ids)
           for c in convs]

    in_scope = len(rows)
    return {
        "rows": rows,
        "counters": {
            "ingestion_in_scope": in_scope,
            "ingestion_candidates": 0,
            "ingestion_held": in_scope,
        },
        "body_open_actual": len(opened_seq),
        # The run's OWN cap, for `write_report` and the metrics row (FIX-02).
        "body_open_cap": BODY_OPEN_CAP if cap is None else int(cap),
    }


def build_contract_inputs(capture: dict[str, Any], accounting: dict[str, Any], *,
                          run_id: str, enumerated_at: str, reported_at: str
                          ) -> tuple[dict[str, Any], dict[str, Any]]:
    """PRE and POST for `tools/cos_contract.py`.

    Nothing here is a judgment either. A read-only night archived nothing and
    drafted nothing, so every enumerated conversation is still resident and
    undrafted — `held_non_drafted` — and every archive candidate is INELIGIBLE
    for one mechanical reason: this driver has no mutation lane at all.
    """
    convs = conversations(capture["enumeration"].get("items", []))
    ids = [c["convId"] for c in convs]
    scan = capture["scan"]
    sent = capture["sent"]
    evidence = {
        "unique_ids": len(ids),
        "list_declared_size": len(ids),
        "stagnant_scans": int(scan.get("stagnant_scans") or 0),
        "scroll_at_end": bool(scan.get("at_end")),
        "dom_scanner_ids": len(scan.get("ids") or []),
        "dom_declared_size": scan.get("declared"),
        "rest_pages": capture["enumeration"].get("page_count"),
        "rest_terminated": bool(capture["enumeration"].get("terminated")),
    }
    provenance = {
        "run_id": run_id,
        "toolset": "chrome-plugin",
        "folder": "Inbox",
        "identity_field": "conversation_id",
        "read_lane": READ_LANE,
    }
    sent_block = {
        "identity_field": "item_id",
        "identity_source": "service.svc FindItem ItemId",
        "window_start": capture["window_start"],
        "captured_at": sent.get("captured_at") or enumerated_at,
        "sort": "newest-first",
        "complete": True,
        "boundary": "list-end",
        "boundary_timestamp": None,
        "items": sent.get("items") or [],
    }
    pre = {
        "run_profile": "full",
        "run_id": run_id,
        "enumerated_at": enumerated_at,
        "enumerated": ids,
        "pre_run_holds": {c["convId"]: "Held · chip"
                          for c in convs if _tier(c.get("categories"))},
        "inbox_conversation_count_before": len(ids),
        "owa_folder_item_count_before": len(capture["enumeration"].get("items", [])),
        "enumeration_complete": True,
        "enumeration_evidence": evidence,
        "scan_provenance": provenance,
        "browser_election": {
            "attempted": ["chrome-plugin"],
            "elected": "chrome-plugin",
            "chrome_plugin_result": ("owner-pinned lane; run-owned tab; read-only "
                                     "service.svc FindItem/GetItem, no click dispatch"),
        },
        "sent_zero_send": sent_block,
    }
    post = {
        "run_profile": "full",
        "run_id": run_id,
        "enumerated_at": reported_at,
        "post_run": {cid: BUCKET_RESIDENT for cid in ids},
        "inbox_conversation_count_after": len(ids),
        "owa_folder_item_count_after": len(capture["enumeration"].get("items", [])),
        "enumeration_complete": True,
        "enumeration_evidence": evidence,
        "scan_provenance": provenance,
        "sent_zero_send": dict(sent_block, captured_at=reported_at),
        "arrived_during_run": [],
        "candidates": [
            {"convid": cid, "capability": "archives", "eligible": False,
             "exclusion_reason": "read-only night: the driver has no mutation lane"}
            for cid in ids
        ],
        "capabilities": {
            "archives": {"in_scope": True, "exercised": False},
            "drafts": {"in_scope": True, "exercised": False},
            "chip_clears": {"in_scope": True, "exercised": False},
        },
    }
    return pre, post


# ---------------------------------------------------------------------------
# the corpus is the FIXTURE
# ---------------------------------------------------------------------------
def corpus_extraction(row: dict[str, Any]) -> dict[str, Any]:
    """The mechanical facts a replay needs to rebuild this row from the corpus.

    The corpus already holds the TEXT; these are the census facts around it.
    Together they make the corpus a complete fixture for the accounting path,
    which is what makes "same captured inputs => byte-identical ledgers" a
    statement anyone can check rather than a claim.
    """
    out = {k: row[k] for k in ("received", "read_state", "tier", "tier_source",
                               "body_opened", "body_chars", "body_open_seq",
                               "message_id")}
    # `.get`, and only for these: every row THIS builder emits carries them,
    # but a row from a night predating the category gate, the FIX-01 drafts
    # census or the FIX-02 cap/attachment fields does not, and a replay of one
    # must rebuild rather than crash.
    out["category_gate_excluded"] = bool(row.get("category_gate_excluded"))
    out["read_never_categories"] = bool(row.get("read_never_categories"))
    # Pre-ruling nights recorded no `never_category` because the two fields WERE
    # one, so fall back to the draw fact — False would replay an old ledger as
    # though its taxonomy had been empty.
    out["never_category"] = bool(row.get("never_category")
                                 or row.get("category_gate_excluded"))
    out["isDraft"] = bool(row.get("isDraft"))
    out["staging_cap"] = row.get("staging_cap", BODY_OPEN_CAP)
    # THE ATTACHMENT NAMES TRAVEL TOO, and they had to before `attachment_lane`
    # could stop being a constant: the replay rebuilds its `bodies` from this
    # corpus, so a field derived from the attachment parts is reproducible only
    # if the parts are here. They were not, and `attachments` itself has been
    # inside the determinism diff and absent from the replay since the file
    # lane shipped — a diff waiting for the first night that carried one.
    out["attachments"] = list(row.get("attachments") or [])
    out["attachments_withheld"] = bool(row.get("attachments_withheld"))
    return out


def write_corpus(vault: Path, run_id: str, accounting: dict[str, Any],
                 capture: dict[str, Any]) -> dict[str, Any]:
    """One corpus row per in-scope thread, carrying the TYPED FIELDS.

    THE SUBJECT AND THE SENDER ARE PERSISTED FOR EVERY ROW, not only for opened
    ones (JDG-01, 2026-08-10; the sender 2026-08-11 for the same reason, measured
    on run 117: 283 of 303 rows judged with `sender: null`, which disarms the
    priority map and every recurring-sender count).
    Phase 1.5 triages from typed fields and nothing else (INJ-03),
    so a row whose subject this run captured and then discarded cannot be
    triaged at all — measured on run 115, where 290 of 310 enumerated rows
    reached the judgment layer with a timestamp and a read-state and no way to
    tell what they were about. `FindItem` already returns the subject on every
    enumerated item; the only defect was throwing it away.
    """
    from brain import cos_corpus                                 # noqa: PLC0415

    bodies = {b["conv_id"]: b for b in capture.get("bodies", [])}
    enumerated = {i.get("convId"): i
                  for i in capture.get("enumeration", {}).get("items", [])}
    appended = 0
    bounded = 0
    for row in accounting["rows"]:
        cid = row["conversation_id"]
        b = bodies.get(cid) if row["body_opened"] else None
        kw = dict(
            conversation_id=cid,
            text=(b or {}).get("text", "") if b else "",
            sender=((b or {}).get("sender")
                    or (enumerated.get(cid) or {}).get("sender") or None),
            sent=(b or {}).get("sent"),
            subject=((b or {}).get("subject")
                     or (enumerated.get(cid) or {}).get("subject") or None),
            read_lane=READ_LANE,
            body_opened=bool(row["body_opened"]),
            raw_chars=(b or {}).get("raw_chars") if b else None)
        ext = corpus_extraction(row)
        try:
            cos_corpus.append_thread(vault, run_id, extraction=ext, **kw)
        except cos_corpus.CorpusRefused:
            # ONE oversized thread must never crash the whole read night
            # (measured run207: a ~20-attachment thread's extraction hit 5221
            # bytes and the uncaught refusal killed the leg with nothing
            # written). The only unbounded field is the `attachments` LIST, so
            # this only recovers a refusal we can actually fix by collapsing it:
            # with no attachments to drop the row is malformed for some OTHER
            # reason (a bad join key, oversized text) and MUST still fail loudly,
            # not be silently dropped from a corpus the ledger will be joined to.
            if not ext.get("attachments"):
                raise
            ext["attachments_dropped"] = len(ext["attachments"])
            ext["attachments"] = []
            cos_corpus.append_thread(vault, run_id, extraction=ext, **kw)
            bounded += 1
        appended += 1
    cos_corpus.close_run(vault, run_id)
    return {"appended": appended, "bounded": bounded, "run": run_id}
# ---------------------------------------------------------------------------
# artifacts
# ---------------------------------------------------------------------------
def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    payload = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                      for r in rows)
    path.write_text(payload, encoding="utf-8")


def write_report(path: Path, run_id: str, accounting: dict[str, Any],
                 completeness_report: dict[str, Any]) -> None:
    """The run report. It states 0 repair rounds because the driver repairs
    NOTHING in flight: a counter is computed from the ledger once, and there is
    no second pass that could disagree with the first."""
    c = accounting["counters"]
    path.write_text(
        f"# COS run {run_id} — driver night (read-only)\n\n"
        f"Produced by `tools/cos_driver.py`. Mechanics only: this run made no "
        f"judgment and staged no candidate.\n\n"
        f"## Census\n\n"
        f"- conversations enumerated: {completeness_report['enumerated_count']} "
        f"(DOM scanner {completeness_report['scanner_count']}, unexplained set "
        f"difference {completeness_report['unexplained_set_difference']})\n"
        f"- messages enumerated: {completeness_report['messages_enumerated']} "
        f"against a server folder total of "
        f"{completeness_report['folder_total_reported']}\n"
        f"- bodies opened: {accounting['body_open_actual']} of a cap of "
        f"{accounting.get('body_open_cap', BODY_OPEN_CAP)}, budget {BODY_BUDGET}\n"
        f"- ingestion in scope {c['ingestion_in_scope']}, candidates "
        f"{c['ingestion_candidates']}, held {c['ingestion_held']}\n\n"
        f"## Judgment\n\n"
        f"Every judgment slot in `_cos_ingestion_ledger_{run_id}.jsonl` is "
        f"`null` (`judgment_pending: true`): `verdict`, `category`, "
        f"`disposition`, `held_reason`, `dedup_check`. The driver does not own "
        f"them and does not guess them.\n\n"
        f"## 🧪 Run-integrity — E-checks (0 repair rounds)\n\n"
        f"The bundle's self-eval is a JUDGMENT pass over this night's artifacts "
        f"and is not the driver's to report. It is left unexecuted rather than "
        f"asserted.\n\n"
        f"## 🔧 Repairs\n\n"
        f"None.\n",
        encoding="utf-8")


def run_host_checks(vault: Path, run_id: str) -> dict[str, Any]:
    """Every host check, EXECUTED — never "would have passed".

    The verdict is reported as it comes back. A read-only night leaves the
    judgment slots empty by design, and the checks that score judgment are
    therefore expected to FAIL; naming which ones is the honest form of that,
    and suppressing them would be the dishonest one.

    `--quiesce-seconds 0` is safe HERE and only here: the quiesce window exists
    so a validator does not score a run that is still writing, and this call is
    made by the writer itself, after ITS last write. It does NOT pass
    `--record` — scoring for the evidence file is not claiming the run.

    THE WRITER IS ONE LANE, NOT THE NIGHT (2026-09-04). This runs at the end of
    the READ lane; judgment, the ingest bridge, attachment fetch, the mutation
    plan and apply, the e-checks and the sheet all write run-named artifacts
    afterwards. `completion()` cannot see that, because the launch-frozen
    `expected_artifacts` names only the four files the read lane writes, so it
    declares the night finished at step one and every later-lane check scores
    "not yet". Measured on run 258: `verdict: INVALID`, 5 of 19 checks failed
    (`self_eval`, `ledger_vocabulary`, `category_stamp`, `ingestion_ledger`,
    `ingest_independence`) on a night that ended 10/10 PASS.

    Widening the manifest is NOT the fix: it is immutable, the VM reads it, and
    a read-only night legitimately writes no mutation artifacts — it would then
    never complete at all. So the block STATES ITS SCOPE instead of claiming
    the run: `scope: "read-lane"` and `read_lane_verdict`, never `verdict`.
    """
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent / "cos_run_verify.py"),
         str(vault), "--run-id", run_id, "--quiesce-seconds", "0", "--json"],
        capture_output=True, text=True, timeout=1800)
    try:
        report = json.loads(proc.stdout)[0]
    except (ValueError, IndexError, KeyError):
        return {"scope": "read-lane", "read_lane_verdict": "not-scored",
                "returncode": proc.returncode, "stderr": proc.stderr[-800:]}
    checks = report.get("checks") or []
    return {
        "scope": "read-lane",
        "read_lane_verdict": report.get("verdict"),
        "executed": [c["check"] for c in checks],
        "executed_count": len(checks),
        "passed": [c["check"] for c in checks if c.get("status") == "pass"],
        "failed": [{"check": c["check"], "detail": c.get("detail", "")[:400]}
                   for c in checks if c.get("status") != "pass"],
        "inputs_digest": report.get("inputs_digest"),
    }


def run_contract(ops: Path, run_id: str, pre: Path, post: Path,
                 out: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(CONTRACT), "--pre", str(pre), "--post", str(post),
         "--ledgers", str(ops), "--run-id", run_id, "--profile", "full",
         "--out", str(out)],
        capture_output=True, text=True, timeout=300)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


# ---------------------------------------------------------------------------
# the night
# ---------------------------------------------------------------------------
def _persist(evidence_path: Path | None, evidence: dict[str, Any]) -> None:
    if not evidence_path:
        return
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")

