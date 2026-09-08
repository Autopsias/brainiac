#!/usr/bin/env python3
"""Backfill the attachment bytes a chip certified but no signed note carries.

WHY THIS EXISTS. `Brainiac · Ingested` chips a thread once its TEXT is signed.
For a thread that also carried a real file, the chip says nothing about the
BYTES — and until ATT-01 was fixed (s05) the join test credited a join that had
never happened, so the shortfall read smaller than it was. The result is a
standing set of (conversation, filename) pairs the vault claims to hold and
does not: no file of that name anywhere under `raw/originals`.

Fixing the lane stops the bleeding. It does not fetch what never arrived. This
does, and it is deliberately THIN — every irreversible step is an existing,
already-audited lane:

    owed_files()      the shortfall census (was a throwaway probe in s03)
    plan_batch()      join it against a FRESH enumeration; name every skip
    write_manifest()  `cos_ingest_bridge_content`'s own line shape
    apply_batch()     `cos_attachment_fetch.fetch` -> `cos.ingest_sweep`

THE MANIFEST LINE IS WRITTEN FRESH, never reused. The sweep bounds a line to
ONE download episode in both directions (`_manifest_candidate`: a stale
namesake and a fresh namesake are both refused), so the original run's line
CANNOT claim bytes fetched months later — by design, and correctly. A backfill
is its own episode and says so with its own run id.

A PLAN IS A PROPOSAL, NOT A PERMIT. The mailbox moves while the batch waits for
a human GO, so `apply_batch` re-reads state and drops any thread whose latest
message is no longer the one planned against. Every drop is a NAMED residual —
`plan_batch` and `apply_batch` both return `residual` rows carrying a reason,
because a count of successes without the named failures is not a report.
"""
from __future__ import annotations

import collections
import json
import sys
import datetime as _dt
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

#: A chip certifies the thread's text. These are the reasons a chipped thread's
#: FILE is nonetheless absent, and they are the vocabulary of every residual row
#: this tool emits. One name per cause, so a census and an apply-time drop can
#: never describe the same thing two ways.
AGED_OUT = "aged-out"                       # thread no longer in the enumeration
ATTACHMENT_GONE = "attachment-gone"         # thread live, that file not on it
ID_UNKNOWN = "attachment-id-unknown"        # file named, no id to fetch it by
MOVED_ON = "thread-moved-on"                # newer message than the plan judged
FETCH_FAILED = "fetch-failed"               # the bytes did not come back
SWEEP_UNCLAIMED = "sweep-unclaimed"         # bytes landed, no signed note
#: An email attached to another email. EWS returns it with an `Item` and never
#: a `Content`, so there are no bytes to fetch and there never will be — this
#: is TERMINAL, not a failure to retry. Measured 2026-09-06: both files this
#: tool called "still fetchable" were attached MESSAGES whose filename is a
#: subject line, and the run reported them `written: false, reason: "NoError"`
#: — a success code standing in for a permanent refusal. Without its own name
#: every future backfill re-offers them and fails identically, which is the
#: standing-refusal loop the discard lane already had to close once.
ITEM_ATTACHMENT = "item-attachment"         # an attached email, never bytes

#: The page's own words for the two ways a `NoError` call still carries nothing.
_NO_BYTES_DETAILS = frozenset({"item-attachment-has-no-bytes"})


def _rows(path: Path):
    """Ledger lines, tolerating the `+` prefix and half-written tails."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        stripped = line.strip().lstrip("+")
        if not stripped:
            continue
        try:
            yield json.loads(stripped)
        except ValueError:
            continue


def _ingestion_ledgers(vault: Path) -> list[Path]:
    return sorted((Path(vault) / "cos-ops").glob("_cos_ingestion_ledger_*.jsonl"))


def _run_of(path: Path) -> str:
    return path.name[len("_cos_ingestion_ledger_"):-len(".jsonl")]


def chipped_conversations(vault: Path) -> set[str]:
    """Threads carrying an `Ingested` chip in any undo ledger."""
    out: set[str] = set()
    for path in sorted((Path(vault) / "cos-ops").glob("_cos_undo_ledger_*.jsonl")):
        for row in _rows(path):
            if row.get("chip") == "Brainiac · Ingested" and row.get("conversation_id"):
                out.add(str(row["conversation_id"]))
    return out


def _stored_names(vault: Path) -> set[str]:
    """Every basename under `raw/originals`.

    BY NAME, deliberately, and it is the LOOSE end of this census: a name that
    matches is credited even if the bytes belong to another message. That makes
    the shortfall an UNDER-count and the batch conservative — the failure mode
    is refetching nothing rather than refetching something already held.
    """
    root = Path(vault) / "raw" / "originals"
    return {p.name for p in root.rglob("*") if p.is_file()}


def owed_files(vault: Path) -> list[dict[str, Any]]:
    """The shortfall: chipped threads whose non-inline files are nowhere.

    Promoted from s03's one-off probe so the number has a single definition
    that a test can pin. Each row carries the run it was last SEEN in, which is
    what makes a later residual able to say when a file was last fetchable.
    """
    from brain import cos  # noqa: PLC0415

    vault = Path(vault)
    chipped = chipped_conversations(vault)
    ledgers = _ingestion_ledgers(vault)

    # filename -> the newest ledger row naming it, per conversation
    seen: dict[str, dict[str, dict[str, Any]]] = collections.defaultdict(dict)
    for path in ledgers:
        run = _run_of(path)
        for row in _rows(path):
            cid = str(row.get("conversation_id") or "")
            if cid not in chipped:
                continue
            for att in row.get("attachments") or []:
                name = cos._safe_basename(str(att.get("filename") or ""))
                if not name or att.get("is_inline"):
                    continue
                seen[cid][name] = {
                    "conversation_id": cid,
                    "filename": name,
                    "last_seen_run": run,
                    "attachment_id": str(att.get("attachment_id") or ""),
                    "approx_size_bytes": int(att.get("approx_size_bytes") or 0),
                    "content_type": str(att.get("content_type") or ""),
                    "category": str(row.get("category") or ""),
                    "judged_tier": str(row.get("judged_tier") or ""),
                    "received": str(row.get("received") or ""),
                    "message_id": str(row.get("message_id") or ""),
                }

    # A thread whose bytes ARE joined to a signed note owes nothing.
    signed: set[str] = set()
    for path in ledgers:
        signed |= set(cos.signed_attachment_conversations(vault, _run_of(path)))

    stored = _stored_names(vault)
    return [entry
            for cid, byname in sorted(seen.items())
            if cid not in signed
            for name, entry in sorted(byname.items())
            if name not in stored]


def _fresh_index(vault: Path, run_id: str) -> dict[str, dict[str, Any]]:
    """conversation_id -> its row in ONE enumeration."""
    path = Path(vault) / "cos-ops" / f"_cos_ingestion_ledger_{run_id}.jsonl"
    return {str(r["conversation_id"]): r
            for r in _rows(path) if r.get("conversation_id")}


def _attachment_in(row: dict[str, Any], filename: str) -> dict[str, Any] | None:
    from brain import cos  # noqa: PLC0415
    for att in row.get("attachments") or []:
        if cos._safe_basename(str(att.get("filename") or "")) == filename:
            return att
    return None


def plan_batch(vault: Path, run_id: str,
               owed: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Join the shortfall against ONE enumeration. Everything not fetchable
    from it becomes a residual with a reason — never a silent drop."""
    owed = owed_files(vault) if owed is None else owed
    fresh = _fresh_index(Path(vault), run_id)
    batch: list[dict[str, Any]] = []
    residual: list[dict[str, Any]] = []

    for want in owed:
        row = fresh.get(want["conversation_id"])
        if row is None:
            residual.append({**want, "reason": AGED_OUT, "as_of_run": run_id})
            continue
        att = _attachment_in(row, want["filename"])
        if att is None:
            residual.append({**want, "reason": ATTACHMENT_GONE, "as_of_run": run_id})
            continue
        aid = str(att.get("attachment_id") or "")
        if not aid:
            residual.append({**want, "reason": ID_UNKNOWN, "as_of_run": run_id})
            continue
        batch.append({
            **want,
            "attachment_id": aid,
            "approx_size_bytes": int(att.get("approx_size_bytes") or 0)
                                 or want["approx_size_bytes"],
            "content_type": str(att.get("content_type") or "") or want["content_type"],
            # THE STATE THIS PLAN WAS BUILT ON. `apply_batch` compares against a
            # re-read and refuses anything that moved; without these two fields
            # it would have nothing to compare to.
            "planned_against_run": run_id,
            "planned_received": str(row.get("received") or ""),
            "planned_message_id": str(row.get("message_id") or ""),
            "category": str(row.get("category") or "") or want["category"],
            "sender": str(((row.get("ingest") or {}).get("sender")) or ""),
            "subject": str(((row.get("ingest") or {}).get("subject")) or ""),
        })

    return {"as_of_run": run_id, "owed": len(owed), "batch": batch,
            "residual": residual, "counts": _counts(batch, residual)}


def _counts(batch: list[dict[str, Any]],
            residual: list[dict[str, Any]]) -> dict[str, Any]:
    by_reason = collections.Counter(r["reason"] for r in residual)
    return {"batch": len(batch), "residual": len(residual),
            "residual_by_reason": dict(sorted(by_reason.items())),
            "conversations": len({r["conversation_id"] for r in batch}),
            "approx_bytes": sum(int(r.get("approx_size_bytes") or 0) for r in batch)}


def manifest_lines(run_id: str, batch: list[dict[str, Any]], *,
                   now: _dt.datetime) -> list[dict[str, Any]]:
    """The sweep's own line shape, one per file — `_manifest_line`'s fields.

    `classification` is MNPI: the owner's standing approval covers ingesting
    all content including MNPI, and the conservative tier is the one that keeps
    the egress gate closed by default rather than the one that opens it.
    """
    from brain.notes import sha256_text  # noqa: PLC0415

    lines: list[dict[str, Any]] = []
    for row in batch:
        cid = row["conversation_id"]
        prov = {"conversation_id": cid}
        if row.get("sender"):
            prov["sender"] = row["sender"]
        if row.get("subject"):
            prov["subject"] = row["subject"]
        if row.get("planned_received"):
            prov["sent"] = row["planned_received"]
        entry: dict[str, Any] = {
            "msg_key": f"{run_id}:{sha256_text(cid)[:12]}",
            "filename": row["filename"],
            "expected_filename": row["filename"],
            "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "category": row.get("category") or "",
            "classification": "MNPI",
            "provenance": prov,
        }
        size = int(row.get("approx_size_bytes") or 0)
        if size > 0:
            entry["approx_size_bytes"] = size
        lines.append(entry)
    return lines


def write_manifest(vault: Path, run_id: str, batch: list[dict[str, Any]], *,
                   now: _dt.datetime) -> Path:
    """Append this backfill's OWN manifest. Never edits another run's."""
    from brain import cos  # noqa: PLC0415
    path = cos.ingest_manifest_dir(vault) / f"manifest-{run_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(line, sort_keys=True) + "\n"
                   for line in manifest_lines(run_id, batch, now=now))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(body)
    return path


def enumeration_index(path: Path | str) -> dict[str, dict[str, Any]]:
    """Read `cos_driver.py --enumerate-only --out` as a re-read source.

    THE CHEAP RE-READ, and the one to prefer. `enumerate_only` is pass 1 alone
    and explicitly "writes NO ledger, NO contract and NO corpus", so it opens no
    bodies and costs a fraction of a read pass — but its rows carry only the
    typed fields (`conversation_id`, `subject`, `sender`, `received`,
    `read_state`, `chip`). That answers two of the three guards: is the thread
    still in the Inbox, and did it take a newer message.

    It CANNOT answer the third — whether the attachment id still resolves —
    because an enumeration row carries no attachments. That guard is not lost,
    it MOVES: the fetch asks the server by id, and a dead id comes back as this
    file's `fetch-failed` residual carrying the server's own reason. A skip is
    still named either way, which is the property that matters.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {str(r["conversation_id"]): r
            for r in data.get("rows") or [] if r.get("conversation_id")}


def recheck(vault: Path, batch: list[dict[str, Any]],
            verify_run: str | None = None, *,
            fresh: dict[str, dict[str, Any]] | None = None,
            source: str = "") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`(still_good, moved_on)` against a re-read taken AFTER the GO.

    The whole point of the human checkpoint is that time passes across it. A
    thread that took a new message in that window is not the thread the plan
    judged, and its attachment id may name a different part.

    `fresh` may come from a full ingestion ledger (`verify_run`) or from an
    enumeration (`enumeration_index`). A ledger row carries attachments and a
    `message_id`; an enumeration row carries neither, so those two checks are
    applied ONLY when the source actually has the field — a missing field must
    never read as a passed check.
    """
    if fresh is None:
        fresh = _fresh_index(Path(vault), str(verify_run))
        source = source or str(verify_run)
    good: list[dict[str, Any]] = []
    moved: list[dict[str, Any]] = []
    for row in batch:
        cur = fresh.get(row["conversation_id"])
        if cur is None:
            moved.append({**row, "reason": AGED_OUT, "as_of_run": source})
            continue
        if str(cur.get("received") or "") > row.get("planned_received", ""):
            moved.append({**row, "reason": MOVED_ON, "as_of_run": source,
                          "now_received": str(cur.get("received") or "")})
            continue
        if cur.get("message_id") and \
                str(cur["message_id"]) != row.get("planned_message_id", ""):
            moved.append({**row, "reason": MOVED_ON, "as_of_run": source,
                          "now_received": str(cur.get("received") or "")})
            continue
        if "attachments" not in cur:
            good.append(row)          # the fetch will name a dead id for us
            continue
        att = _attachment_in(cur, row["filename"])
        if att is None or not str(att.get("attachment_id") or ""):
            moved.append({**row,
                          "reason": ATTACHMENT_GONE if att is None else ID_UNKNOWN,
                          "as_of_run": source})
            continue
        # The re-read's id wins: it is the one the server will answer to now.
        good.append({**row, "attachment_id": str(att["attachment_id"])})
    return good, moved


def apply_batch(vault: Path, run_id: str, plan: dict[str, Any], *,
                verify_run: str | None = None,
                verify_enumeration: Path | str | None = None,
                now: _dt.datetime | None = None,
                fetch=None, sweep=None) -> dict[str, Any]:
    """THE LIVE STEP. Re-read, write the manifest, fetch, sweep, reconcile.

    `fetch`/`sweep` are injectable so the whole join can be tested without a
    browser or a mailbox; the defaults are the real, audited lanes.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    batch = list(plan.get("batch") or [])
    residual = list(plan.get("residual") or [])

    if verify_enumeration:
        batch, moved = recheck(vault, batch,
                               fresh=enumeration_index(verify_enumeration),
                               source=str(verify_enumeration))
        residual += moved
    elif verify_run:
        batch, moved = recheck(vault, batch, verify_run)
        residual += moved

    if not batch:
        return {"run_id": run_id, "fetched": 0, "recovered": [],
                "residual": residual, "status": "nothing-to-fetch"}

    manifest = write_manifest(vault, run_id, batch, now=now)

    if fetch is None:
        from cos_attachment_fetch import fetch as fetch  # noqa: PLC0415
    report = fetch([{"attachment_id": r["attachment_id"],
                     "filename": r["filename"],
                     "approx_size_bytes": r["approx_size_bytes"]}
                    for r in batch])

    landed = {f["filename"]: f for f in report.get("files") or [] if f.get("written")}
    for row in batch:
        if row["filename"] not in landed:
            bad = next((f for f in report.get("files") or []
                        if f.get("filename") == row["filename"]), {})
            detail = str(bad.get("reason") or "no-content")
            # TERMINAL, NOT FAILED: an attached email has no bytes to fetch, so
            # calling it `fetch-failed` would keep it in the fetchable pool and
            # re-offer it on every future run.
            reason = (ITEM_ATTACHMENT if detail in _NO_BYTES_DETAILS
                      else FETCH_FAILED)
            residual.append({**row, "reason": reason, "detail": detail})

    if sweep is None:
        from brain.cos import ingest_sweep as sweep  # noqa: PLC0415
    swept = sweep(vault)

    # A CLAIM IS THE RECEIPT, not the write. `written: True` only says bytes
    # reached the staging dir; the sweep's `moved` row is the audited claim of
    # a manifest line, and it is what puts the file under the acceptance lane.
    #
    # THIS IS WHERE THE TOOL STOPS, deliberately. The sweep quarantines at
    # `state: pending`; `_accept_attachment` -> `vault/inbox/` -> `run_ingest`
    # is what finally mints the signed `raw/` note, and that leg is the owner's
    # verdict/approval machinery. A backfill that drove it itself would be
    # ingesting on the owner's behalf around his own gate. `verify_recovered`
    # below reads the note ids back out once the ordinary lane has run.
    moved = {str(m.get("filename") or ""): m for m in swept.get("moved") or []}
    recovered: list[dict[str, Any]] = []
    for row in batch:
        got = landed.get(row["filename"])
        if not got:
            continue
        claim = moved.get(row["filename"])
        if claim is None:
            residual.append({**row, "reason": SWEEP_UNCLAIMED,
                             "sha256": got.get("sha256", ""),
                             "detail": _why_unclaimed(swept, row["filename"])})
            continue
        recovered.append({**row, "sha256": got.get("sha256", ""),
                          "bytes": got.get("bytes", 0),
                          "attachment_meta_id": str(claim.get("id") or ""),
                          "quarantine_dest": str(claim.get("dest") or "")})

    return {"run_id": run_id, "manifest": str(manifest),
            "requested": len(batch), "fetched": len(landed),
            "recovered": recovered, "residual": residual,
            "fetch_report": report, "sweep_report": swept}


def _why_unclaimed(swept: dict[str, Any], filename: str) -> str:
    """The sweep's OWN words for why this name was not claimed.

    It reports refusals and unmatched names separately and with reasons; a
    residual that just said `sweep-unclaimed` would throw away the one piece of
    information that says whether to retry or to stop.
    """
    for row in swept.get("refused") or []:
        if str(row.get("filename") or "") == filename:
            return str(row.get("reason") or "refused")
    for row in swept.get("unmatched_reasons") or []:
        if str(row.get("filename") or "") == filename:
            return str(row.get("reason") or "unmatched")
    for row in swept.get("duplicates") or []:
        if str(row.get("filename") or "") == filename:
            return f"duplicate: already quarantined as {row.get('id')}"
    return "not reported by the sweep"


def verify_recovered(vault: Path, recovered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve each recovered file's SIGNED NOTE ID from its content hash.

    The drain keeps an authoritative sha -> note-id map, so this reads the
    outcome rather than predicting it: a row with `note_id: None` has bytes on
    disk that no signed note carries YET, which is a true statement about the
    acceptance lane not having run, not a failure of the fetch.
    """
    from brain import cos  # noqa: PLC0415
    return [{**row, "note_id": cos._ingested_raw_id(vault, row.get("sha256", ""))}
            for row in recovered]


def main(argv: list[str] | None = None) -> int:
    """Kept here so `cos_attachment_backfill.py <verb>` stays the launch line
    the run script and the docs already name."""
    from cos_attachment_backfill_cli import main as _main  # noqa: PLC0415
    return _main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
