"""The accounting and artifact writers of `cos_driver` — corpus rows, contract inputs, reports

Moved verbatim out of `cos_driver` (batch-2 drain) and re-imported by it, so
every name keeps its `cos_driver` module path; the parent's night orchestration
calls these through its own globals exactly as before, so a test that
monkeypatches one on `cos_driver` still steers the callers.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_driver_draw import (  # noqa: E402
    CHIP_TIER, _tier, _tier_source, conversations)
from cos_driver_transport import (  # noqa: E402
    BODY_BUDGET, BODY_OPEN_CAP, READ_LANE, short)

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


def build_accounting(capture: dict[str, Any], *, run_id: str,
                     bundle_version: str, rules_version: str,
                     enumerated_at: str,
                     gate_excluded: set[str] | frozenset[str] = frozenset()
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
    opened_seq: dict[str, int] = {}
    seq = 0
    for d in capture.get("draw", []):
        b = bodies.get(d["convId"])
        if body_open_succeeded(b):
            seq += 1
            opened_seq[d["convId"]] = seq

    rows: list[dict[str, Any]] = []
    for c in convs:
        cid = c["convId"]
        b = bodies.get(cid)
        opened = cid in opened_seq
        row: dict[str, Any] = {
            "run": run_id,
            "run_profile": "full",
            "conversation_id": cid,
            "message_id": c.get("itemId"),
            "received": c.get("received"),
            "read_state": "read" if c.get("isRead") else "unread",
            "read_lane": READ_LANE,
            "tier": _tier(c.get("categories")),
            "tier_source": _tier_source(c.get("categories")),
            "body_opened": opened,
            # A FACT ABOUT THE PASS, not a judgment about the mail: this row was
            # held out of the rule-1½ draw because the category batch stamped it
            # with an id the owner's taxonomy dispositions `never`. The judgment
            # is the CATEGORY, and it is the model's; what the driver records is
            # that the body was consequently never opened. `cos_judge`'s
            # `mechanical_disposition` reads this to write rule 1¾'s pairing
            # (`no-substance` / `never-category`) without asking the model to
            # re-decide something already on disk.
            "category_gate_excluded": cid in gate_excluded,
            "body_chars": int(b.get("body_chars") or 0) if b else 0,
            "body_open_seq": opened_seq.get(cid),
            "body_budget": BODY_BUDGET,
            "staging_cap": BODY_OPEN_CAP,
            "attachment_lane": "not-exercised",
            "send_attempted": False,
            "extraction_rules_version": rules_version,
            "bundle_version": bundle_version,
            "ts": enumerated_at,
            # --- judgment slots, owned by s03 and left EMPTY on purpose -------
            "verdict": None,
            "category": None,
            "disposition": None,
            "held_reason": None,
            "dedup_check": None,
            "candidate_count": 0,
            "proposal_id": None,
            "content_sha256": None,
            "judgment_pending": True,
        }
        rows.append(row)

    in_scope = len(rows)
    return {
        "rows": rows,
        "counters": {
            "ingestion_in_scope": in_scope,
            "ingestion_candidates": 0,
            "ingestion_held": in_scope,
        },
        "body_open_actual": len(opened_seq),
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
    # `.get`, and only for this one: every row THIS builder emits carries it,
    # but a row from a night that predates the category gate does not, and a
    # replay of one must rebuild rather than crash.
    out["category_gate_excluded"] = bool(row.get("category_gate_excluded"))
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
    for row in accounting["rows"]:
        cid = row["conversation_id"]
        b = bodies.get(cid) if row["body_opened"] else None
        cos_corpus.append_thread(
            vault, run_id,
            conversation_id=cid,
            text=(b or {}).get("text", "") if b else "",
            sender=((b or {}).get("sender")
                    or (enumerated.get(cid) or {}).get("sender") or None),
            sent=(b or {}).get("sent"),
            subject=((b or {}).get("subject")
                     or (enumerated.get(cid) or {}).get("subject") or None),
            read_lane=READ_LANE,
            body_opened=bool(row["body_opened"]),
            extraction=corpus_extraction(row))
        appended += 1
    cos_corpus.close_run(vault, run_id)
    return {"appended": appended, "run": run_id}


def accounting_from_corpus(vault: Path, run_id: str, *, bundle_version: str,
                           rules_version: str, enumerated_at: str) -> dict[str, Any]:
    """Rebuild the ledger rows from the CORPUS alone — the replay path.

    Deliberately a different entry point over the same builder: re-hashing an
    output file proves the file did not change, which is not what "byte-identical
    from the same captured inputs" means.
    """
    from brain import cos_corpus                                 # noqa: PLC0415

    items = []
    bodies = []
    draw: list[dict[str, str]] = []
    gate_excluded: set[str] = set()
    for r in cos_corpus.read_corpus(vault, run_id):
        ext = r.get("extraction") or {}
        cid = r["conversation_id"]
        items.append({
            "convId": cid,
            "itemId": ext.get("message_id"),
            "isRead": ext.get("read_state") == "read",
            "categories": [k for k, v in CHIP_TIER.items() if v == ext.get("tier")],
            "received": ext.get("received"),
            "subject": (r.get("provenance") or {}).get("subject") or "",
        })
        if ext.get("category_gate_excluded"):
            # PERSISTED, NOT RE-DERIVED. The replay has no taxonomy lookup and
            # no category batch; re-deciding the exclusion here would make the
            # determinism check a test of two lookups agreeing rather than of
            # the accounting being a pure function of the capture.
            gate_excluded.add(cid)
        if ext.get("body_opened"):
            bodies.append({"conv_id": cid, "ok": True,
                           "body_chars": int(ext.get("body_chars") or 0),
                           "text": r.get("text", ""),
                           "seq": ext.get("body_open_seq")})
    bodies.sort(key=lambda b: int(b.get("seq") or 0))
    draw = [{"convId": b["conv_id"], "itemId": None} for b in bodies]
    capture = {"enumeration": {"items": items}, "bodies": bodies, "draw": draw,
               "scan": {}, "sent": {}}
    return build_accounting(capture, run_id=run_id, bundle_version=bundle_version,
                            rules_version=rules_version, enumerated_at=enumerated_at,
                            gate_excluded=gate_excluded)


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
        f"{BODY_OPEN_CAP}, budget {BODY_BUDGET}\n"
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
    made by the writer itself, after its last write. It does NOT pass
    `--record` — scoring for the evidence file is not claiming the run.
    """
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent / "cos_run_verify.py"),
         str(vault), "--run-id", run_id, "--quiesce-seconds", "0", "--json"],
        capture_output=True, text=True, timeout=1800)
    try:
        report = json.loads(proc.stdout)[0]
    except (ValueError, IndexError, KeyError):
        return {"verdict": "not-scored", "returncode": proc.returncode,
                "stderr": proc.stderr[-800:]}
    checks = report.get("checks") or []
    return {
        "verdict": report.get("verdict"),
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

