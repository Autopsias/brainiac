"""The written record of a COS night: evidence skeleton, ledger/corpus/
contract artifacts, replay digest.

Sub-steps of ``cos_driver._run_night`` that produce what a night leaves on
disk. ``_run_night`` keeps its name and module (the tests, the replay check
and the nightly's expectations name it there); every parent callable or
constant this needs (``write_jsonl``, ``write_corpus``,
``build_contract_inputs``, ``write_report``, ``READ_LANE``,
``DIFF_EXCLUDED``) arrives as a parameter — this module never imports
``cos_driver``, so a monkeypatched parent attribute keeps working. The
metrics row deliberately stays in the parent: a test pins the literal
``"read_lane": READ_LANE`` to the driver's own source.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_driver_accounting import build_accounting  # noqa: E402
from cos_driver_draw import CHIP_TIER  # noqa: E402

REPLAY_TIMEOUT_S = 900


def night_evidence_skeleton(run_id: str, manifest_lane: str, vault: Path,
                            ops: Path, raw_sources: int, started_at: str,
                            read_lane: str,
                            diff_excluded: dict[str, str]) -> dict[str, Any]:
    """The evidence record declared UP FRONT, zeros visible before the night
    earns its numbers."""
    return {
        "session": "s02", "item": "REST-02",
        "run_id": run_id,
        "run_id_source": "host-stamped MAN-01 sheet (brain cos-run-begin)",
        "manifest_lane": manifest_lane,
        "manifest_lane_accepted": True,
        "driver_read_lane": read_lane,
        "vault_root_asserted": {
            "BRAIN_VAULT": str(vault),
            "raw_source_count_at_preflight": raw_sources,
            "cos_ops_exists": ops.is_dir(),
            "asserted_before_any_browser_action": True,
        },
        "started_at": started_at,
        # Declared UP FRONT and overwritten as the night earns them. A stop then
        # leaves a complete-shaped record whose zeros are visible, instead of an
        # absence a reader has to interpret (WAT-01: ship the failure mode with
        # the number that reveals it).
        "bodies_attempted": 0,
        "bodies_succeeded": 0,
        "bodies_error": 0,
        "seed_kind": None,
        "contract": {"exit_code": None, "render": "not reached"},
        "host_checks_executed": [],
        "second_process_diff": None,
        "excluded_fields": diff_excluded,
        "fixture_ref": None,
    }


def write_night_artifacts(
        vault: Path, ops: Path, run_id: str, capture: dict[str, Any],
        accounting: dict[str, Any], report: dict[str, Any],
        enumerated_at: str, reported_at: str, *,
        write_jsonl: Callable[[Path, list[dict[str, Any]]], None],
        write_corpus: Callable[..., dict[str, Any]],
        build_contract_inputs: Callable[..., tuple[dict, dict]],
        write_report: Callable[..., None]) -> dict[str, Any]:
    """Ledger, corpus, contract PRE/POST snapshots, sent baseline, nightly
    report — everything the metrics row (kept in the parent) is written
    beside."""
    ledger = ops / f"_cos_ingestion_ledger_{run_id}.jsonl"
    write_jsonl(ledger, accounting["rows"])
    corpus = write_corpus(vault, run_id, accounting, capture)
    pre, post = build_contract_inputs(capture, accounting, run_id=run_id,
                                      enumerated_at=enumerated_at,
                                      reported_at=reported_at)
    pre_path = ops / f"cos_contract_pre_{run_id}.json"
    post_path = ops / f"cos_contract_post_{run_id}.json"
    pre_path.write_text(json.dumps(pre, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    post_path.write_text(json.dumps(post, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    (ops / f"_cos_sent_baseline_{run_id}.json").write_text(
        json.dumps(pre["sent_zero_send"], indent=2) + "\n", encoding="utf-8")
    write_report(ops / f"_cos_nightly_{run_id}.md", run_id, accounting, report)
    return {"ledger": ledger, "corpus": corpus, "pre_path": pre_path,
            "post_path": post_path}


def replay_determinism(vault: Path, run_id: str,
                       replay_script: Path) -> tuple[Any, dict[str, Any]]:
    """The second-process determinism replay: (second_process_diff,
    determinism) exactly as the evidence record carries them."""
    replay = subprocess.run(
        [sys.executable, str(replay_script),
         "--vault", str(vault), "--run-id", run_id],
        capture_output=True, text=True, timeout=REPLAY_TIMEOUT_S)
    try:
        rep = json.loads(replay.stdout)
        return rep["second_process_diff"], {k: rep[k] for k in
                                            ("method", "rows_live", "rows_replayed",
                                             "excluded_fields",
                                             "enumerated_at_compared_as_one_value")}
    except (ValueError, KeyError):
        return None, {"error": replay.stderr[-800:]}


def fixture_ref(corpus_appended: Any, run_id: str,
                bodies: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "kind": "host-only COS capture corpus (never in this repository)",
        "run_id": run_id,
        "rows": corpus_appended,
        "schema": "brain.cos_corpus CORPUS_SCHEMA + `extraction` census facts",
        "response_digests": sorted(
            b["body_sha256"] for b in bodies if b.get("body_sha256")),
        "why_not_here": ("the raw responses are real message bodies, classified "
                         "MNPI; `_evidence/` is inside the repository and no "
                         "retention or deletion guarantee reaches git history"),
    }


# ---------------------------------------------------------------------------
# the attachment lane's per-run FACTS (FIX-02: the count is real)
# ---------------------------------------------------------------------------
def attachment_fetch_report_path(ops: Path, run_id: str) -> Path:
    """Where `cos_attachment_fetch.py` leaves its report for the stamp."""
    return ops / f"_cos_attachment_fetch_{run_id}.json"


def attachment_lane_facts(vault: Path, run_id: str,
                          report: dict[str, Any] | None = None
                          ) -> dict[str, Any]:
    """The per-run attachment counts, read from the run's own artifacts.

    MEASURED 2026-08-25: run188's metrics row said `attachment_lane:
    "not-exercised"` while its ingest manifest carried 32 lines for that run.
    The row is written by the READ pass, hours before the manifest or the
    fetched files exist, so a constant there can only ever be wrong.

    `attachments_dropped` counts the ingest-manifest lines whose `msg_key`
    names THIS run (one line per attachment the bridge dropped);
    `attachments_fetched` counts what the fetch report says it wrote. The lane
    word stays inside the closed vocabulary `cos_reconcile_append` checks; the
    two counts are new integer fields beside it.
    """
    from brain import cos                                        # noqa: PLC0415

    dropped = 0
    for mf in sorted(cos.ingest_manifest_dir(vault).glob("manifest-*.jsonl")):
        for line in mf.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue          # a torn line is not a dropped attachment
            if str(entry.get("msg_key") or "").startswith(f"{run_id}:"):
                dropped += 1
    if report is None:
        rpath = attachment_fetch_report_path(cos.run_ops_dir(vault), run_id)
        try:
            report = json.loads(rpath.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = {}
    fetched = int((report or {}).get("written") or 0)
    return {"attachment_lane": ("downloads-mounted" if dropped or fetched
                                else "not-exercised"),
            "attachments_dropped": dropped,
            "attachments_fetched": fetched}


def stamp_attachment_lane(vault: Path, run_id: str,
                          report: dict[str, Any] | None = None
                          ) -> dict[str, Any]:
    """SUPERSEDE the run's metrics row with the attachment lane's real counts.

    Append-only, through the same `append_metric` gate as every other row (so
    the closed vocabulary, the required-field check and the ingestion recount
    all still bind): a copy of the run's last row with the three lane fields
    corrected and `supersedes_run_ts` naming the row it replaces. No-op when
    the counts change nothing, and it never edits a line.
    """
    import datetime as _dt                                       # noqa: PLC0415
    import cos_reconcile_metrics as recon                        # noqa: PLC0415

    from brain import cos                                        # noqa: PLC0415

    ops = cos.run_ops_dir(vault)
    facts = attachment_lane_facts(vault, run_id, report=report)
    rows = [r for r in recon._rows(ops / "_cos_metrics.jsonl")
            if r.get("run_id") == run_id]
    if not rows:
        return {"appended": "no-driver-row", "facts": facts}
    prior = rows[-1]
    if all(prior.get(k) == v for k, v in facts.items()):
        return {"appended": "unchanged", "facts": facts}
    row = {**prior, **facts,
           "run_ts": _dt.datetime.now(_dt.timezone.utc).isoformat(
               timespec="milliseconds").replace("+00:00", "Z"),
           recon.SUPERSEDES: str(prior.get("run_ts"))}
    return {"appended": recon.append_metric(ops, row), "facts": facts,
            "supersedes": row[recon.SUPERSEDES]}


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
    caps: set[int] = set()
    read_never = False
    never_ids: set[str] = set()
    for r in cos_corpus.read_corpus(vault, run_id):
        ext = r.get("extraction") or {}
        cid = r["conversation_id"]
        items.append({
            "convId": cid,
            "itemId": ext.get("message_id"),
            "isRead": ext.get("read_state") == "read",
            # FIX-01's census fact, persisted by `corpus_extraction` above.
            "isDraft": ext.get("isDraft") is True,
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
        if ext.get("never_category") or ext.get("category_gate_excluded"):
            never_ids.add(cid)
        if ext.get("read_never_categories"):
            # PERSISTED, NOT RE-DERIVED, for the same reason the exclusion
            # above is: the replay holds no overlay and must not re-read the
            # owner's lever, or the determinism check becomes a test of two
            # reads agreeing instead of of the accounting being pure.
            read_never = True
        if ext.get("staging_cap"):
            caps.add(int(ext["staging_cap"]))
        if ext.get("body_opened"):
            bodies.append({"conv_id": cid, "ok": True,
                           "body_chars": int(ext.get("body_chars") or 0),
                           "text": r.get("text", ""),
                           "attachments": list(ext.get("attachments") or []),
                           # `item_keys` is only ever read as a BOOL here.
                           "item_keys": ext.get("attachments_withheld") or None,
                           "seq": ext.get("body_open_seq")})
    bodies.sort(key=lambda b: int(b.get("seq") or 0))
    draw = [{"convId": b["conv_id"], "itemId": None} for b in bodies]
    capture = {"enumeration": {"items": items}, "bodies": bodies, "draw": draw,
               "scan": {}, "sent": {}}
    # Older corpora carry no `staging_cap`; `build_accounting`'s default is
    # the constant those nights recorded anyway.
    return build_accounting(capture, run_id=run_id, bundle_version=bundle_version,
                            rules_version=rules_version, enumerated_at=enumerated_at,
                            gate_excluded=gate_excluded,
                            cap=max(caps) if caps else None,
                            read_never=read_never, never_ids=never_ids)
