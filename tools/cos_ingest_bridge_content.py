"""The BYTES the bridge writes: drop content, manifest-line shape, anomaly doc.

Moved verbatim out of `cos_ingest_bridge` (file-size ratchet split); every
comment travels with its code and no behaviour changes. Nothing here touches
disk or decides anything — these are pure shape functions the facade and its
siblings re-import, so `tools.cos_ingest_bridge` keeps exporting every name.
Covered by the `tests/test_cos_ingest_bridge_*.py` slices.
"""
from __future__ import annotations

import datetime as _dt
import json

from brain import cos
from brain.notes import safe_slug, sha256_text


CONTENT_TEXT = "text"
CONTENT_ATTACHMENTS = "attachments"
CONTENT_BOTH = "both"

#: taxonomy rule `lane` -> content choice (owner rule 2026-08-17 #3).
_LANE_TO_CHOICE = {"text": CONTENT_TEXT, "attachment": CONTENT_ATTACHMENTS,
                   "both": CONTENT_BOTH}

#: Taxonomy absent/off or category unknown -> `text`: the corpus-backed lane,
#: the one whose missing input quarantines loudly instead of routing around.
DEFAULT_CONTENT_CHOICE = CONTENT_TEXT


#: Bridge anomaly-evidence docs in the engine's claim-quarantine store carry
#: this prefix. They are EVIDENCE, never content: every custody/identity scan
#: must exclude them, or a parked anomaly would read as a delivery in custody.
ANOMALY_PREFIX = "cosbridge-anomaly-"


def content_choice_for(category: str, taxonomy: dict) -> str:
    """The category's content choice, from its taxonomy rule's lane. With the
    taxonomy unapproved/absent there is no lane fact, so all take the
    default."""
    if taxonomy.get("mode") != "active":
        return DEFAULT_CONTENT_CHOICE
    rule = (taxonomy.get("rules") or {}).get(str(category or "").strip())
    if not isinstance(rule, dict):
        return DEFAULT_CONTENT_CHOICE
    return _LANE_TO_CHOICE.get(str(rule.get("lane") or "both"),
                               DEFAULT_CONTENT_CHOICE)


def choice_for_candidate(category: str, taxonomy: dict, row: dict) -> str:
    """The content choice for ONE accepted candidate: its category's lane,
    widened to `both` when the row names a real file.

    A LANE IS A FLOOR ON WHAT A CANDIDATE INGESTS, NEVER A CEILING (owner
    ruling 2026-08-23). Choosing the file lane from the category alone
    answered a question nobody asked. Measured on run 2026-08-23-run171 (221
    threads): six threads carried a real non-inline document and only ONE sat
    in a file-carrying category. An IBM technical proposal and its SAP
    requirements annex sat on `counterparty-position`; a CAQ tender evaluation
    pair sat on `commitment`. Both lanes are text-only, so the threads that
    MENTIONED those documents were ingested and the documents were skipped.

    Two rules bound the widening. A `never` category never reaches here
    (`_never_category` returns first), so this can only widen what an
    ALREADY-accepted candidate carries — it can never manufacture one. And
    inline parts are not files (`_attachment_names`), so a signature logo
    never triggers it.
    """
    choice = content_choice_for(category, taxonomy)
    if choice == CONTENT_TEXT and _attachment_names(row):
        return CONTENT_BOTH
    return choice


def _is_attached_message(a: dict) -> bool:
    """True for an attached EMAIL rather than a file, on TWO agreeing signals.

    Either alone is too sharp. `content_type` is the faithful one — OWA sets a
    type on every real file and none on a nested message — but a partial or
    older capture can omit it for a genuine file, and skipping that file would
    be a silent loss. An absent EXTENSION agrees independently: an attached
    message is named by its subject. Requiring BOTH means this skips only what
    looks like neither a typed file nor a named one, and a real file missing
    just one signal is still claimed.
    """
    if a.get("content_type"):
        return False
    name = str(a.get("filename") or "")
    stem, dot, ext = name.rpartition(".")
    return not (dot and stem and 1 <= len(ext) <= 8 and ext.isalnum())


def _attachment_names(row: dict) -> list[str]:
    """Attachment filenames the FILE LANE should carry, basename-guarded
    (`_safe_basename`, the one bare-name rule, INT-05), or [].

    AN ATTACHED MESSAGE IS NOT A FILE, and a missing `content_type` is how
    this build says so. OWA returns an attached email as an attachment like
    any other — a name, a size, an id — but no content type and no extension,
    and the file-download route cannot render it, so the fetch failed it with
    the unhelpful string `NoError`. One such item among 13 real files stopped
    the whole night at exit 20 (run173, 2026-08-23): 12 documents were already
    on disk and none could be archived, because a night must not archive mail
    whose attachment it failed to preserve. Measured on that run, the split is
    exact — all 13 files carried a content type (pdf, xlsx, docx, zip, csv,
    png) and the sole attached message carried none. STATED CEILING: this is
    a proxy. OWA's own discriminator is the attachment's `__type`
    (`FileAttachment` vs `ItemAttachment`), which `cos_driver_page.js` does
    not capture; if a real file ever arrives with no content type it will be
    skipped silently, and capturing `__type` is the upgrade. The thread's own
    TEXT lane still runs, so the conversation is not lost — only the nested
    message is, and it is mail, not a document.

    INLINE PARTS ARE EXCLUDED, and that is the difference between a working
    lane and a vault full of `image001.png`. OWA marks a signature logo and an
    embedded screenshot `IsInline: true`; measured on the live mailbox
    2026-08-22, 5 of the 6 opened threads carried nothing but inline PNGs of
    481-12,805 bytes, and the sixth carried a real 19 KB CSV alongside them.
    The LEDGER keeps every part, inline included — that is the record of what
    the mail held; this is only which of them the manifest claims.
    """
    return [n for n in (cos._safe_basename(str(a.get("filename") or ""))
                        for a in row.get("attachments") or []
                        if isinstance(a, dict) and not a.get("is_inline")
                        and not _is_attached_message(a)) if n]


def _claims(row: dict, corpus_row: dict | None) -> dict:
    """Provenance CLAIMS. The conversation id comes from the LEDGER ROW — the
    corpus row's `provenance` has sender/sent/subject only, and reading the id
    there sent every drop and manifest line out without one. Never
    `provenance.verified`: only a host parse of an original earns that."""
    prov = (corpus_row or {}).get("provenance") or {}
    cid = str(row.get("conversation_id") or "").strip()
    return {k: prov[k] for k in ("sender", "sent", "subject") if prov.get(k)
            } | ({"conversation_id": cid} if cid else {})


def _manifest_line(run_id: str, row: dict, *, filename: str, tier: str,
                   prov: dict, now: _dt.datetime) -> dict:
    """One manifest line in the sweep's OWN vocabulary. The sweep and the
    INT-04 anchors are untouched — this is only a new WRITER of that shape."""
    cid = str(row.get("conversation_id") or "")
    entry: dict = {
        "msg_key": f"{run_id}:{sha256_text(cid)[:12]}",
        "filename": filename,
        "expected_filename": filename,
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "category": str(row.get("category") or "").strip(),
        "classification": tier,
        "provenance": dict(prov),
    }
    size = next((a.get("approx_size_bytes") for a in row.get("attachments") or []
                 if isinstance(a, dict)
                 and cos._safe_basename(str(a.get("filename") or "")) == filename),
                None)
    if isinstance(size, int) and size > 0:
        entry["approx_size_bytes"] = size
    return entry


def _fm(key, value):
    # ALWAYS quoted — a JSON string IS a YAML double-quoted scalar, escapes
    # and all. `capture.enforce` RE-PARSES this block before staging it, and
    # unquoted (measured, not assumed) it reads `Q3 update #4` as a comment,
    # `yes`/`on` as bools, `~` as null, `0x1f` as 31 and an ISO date as a
    # date object, while a leading `*`/`&` fails strict YAML outright and
    # drops the whole block to the fallback parser — corrupting exactly the
    # two free-text fields (`title`, `provenance.subject`) an email fills.
    return f"{key}: {json.dumps(str(value), ensure_ascii=False)}"


def _run_day(run_id: str, now: _dt.datetime) -> str:
    """The DROP-CONTENT date: the run id's own date, falling back to the wall
    clock only when the run id carries none. Deliberate (attempt 14): the drop
    bytes are a pure function of (run, candidate, corpus, taxonomy, tier), so
    an idempotent re-run — even across a midnight — produces byte-identical
    content and the ENGINE's replay guard is what suppresses the duplicate."""
    try:
        return _dt.date.fromisoformat(run_id[:10]).isoformat()
    except ValueError:
        return now.date().isoformat()


def _proposal_content(*, run_id: str, row: dict, choice: str, tier: str,
                      text: str | None, corpus_row: dict | None,
                      attachments: list[str], now: _dt.datetime) -> str:
    """The drop for one candidate: provenance CLAIMS, the judged category, the
    content choice, and the SOURCE MAP. Every field is a claim — the host join
    reads the LEDGER row, not this frontmatter (STA-01). DETERMINISTIC per
    run (see `_run_day`): a re-run stages byte-identical content, which is
    exactly what lets the engine's replay guard own duplicate suppression."""
    corpus_row = corpus_row or {}
    prov, cid = _claims(row, corpus_row), str(row.get("conversation_id") or "")
    source_digest = str(corpus_row.get("text_sha256") or "") or None
    # `updated` is set EXPLICITLY (attempt 16, finding 3): `capture.enforce`
    # fills any missing `updated` from the WALL-CLOCK day, so omitting it
    # made the staged bytes differ across midnight — the replay guard binds
    # on byte-identity, and a next-day re-run's unbindable bytes re-asked
    # the owner a question the guard exists to suppress.
    fm: dict[str, object] = {
        "title": prov.get("subject") or f"COS ingestion candidate {run_id}",
        "type": "note", "classification": tier,
        "created": _run_day(run_id, now), "updated": _run_day(run_id, now),
        "content_choice": choice,
        "category": str(row.get("category") or "").strip(), "cos.run": run_id}
    if source_digest:
        fm["cos.source_sha256"] = source_digest
    fm.update({f"provenance.{k}": prov[k] for k in
               ("sender", "sent", "conversation_id", "subject") if prov.get(k)})

    lines = ["---", *[_fm(k, v) for k, v in fm.items()], "---", "",
             "## Source map (cos_ingest_bridge)", "",
             f"- run: {run_id}", f"- conversation_id: {cid}",
             f"- content_choice: {choice}"]
    if source_digest:
        lines.append(f"- corpus text_sha256: {source_digest}")
    lines += [f"- expected artifact: {n} (ingest-manifest lane; owner "
              "acceptance anchor INT-04)" for n in attachments] + [""]
    if text is not None and text.strip():
        lines += ["## Captured message text", "", text.rstrip(), ""]
    elif attachments:
        lines += ["The value of this candidate is its attachment(s); the "
                  "covering message carried no captured text.", ""]
    return "\n".join(lines)


def _anomaly_nid(key: str) -> str:
    """Run-independent, so a standing anomaly is ONE parked doc refreshed per
    (id, code) — never one per night."""
    return safe_slug(f"{ANOMALY_PREFIX}{key}")


def _anomaly_doc(nid: str, *, run_id: str, row: dict, reason: str,
                 detail: str, now: _dt.datetime) -> str:
    """The parked EVIDENCE for one quarantined candidate: the reason, the
    full ledger row, and how to replay. Shaped as a valid capture note
    (id/type/classification/created) ON PURPOSE: the engine's hourly release
    path re-binds everything in claim-quarantine/, and a doc that fails
    `capture.validate` would be flushed to rejected/ within the hour instead
    of HELD — probed end to end through the real `cos.claim_drops` in the
    suite. Its nid joins no ingestion-ledger row, so the release path holds
    it (no-ledger-row) until a human resolves the anomaly or the bridge
    clears it when the conversation settles."""
    fm_lines = [_fm("id", nid), _fm("title", f"COS ingest bridge anomaly ({reason})"),
                _fm("type", "note"), _fm("classification", "MNPI"),
                _fm("created", now.date().isoformat())]
    return "\n".join([
        "---", *fm_lines, "---", "",
        "## Bridge quarantine evidence", "",
        f"- reason: {reason}", f"- detail: {detail}", f"- run: {run_id}",
        f"- conversation_id: {row.get('conversation_id') or ''}", "",
        "Ledger row at quarantine time:", "", "```json",
        json.dumps(row, ensure_ascii=False, sort_keys=True), "```", "",
        "Replay: fix the recorded cause, then re-run "
        f"`tools/cos_ingest_bridge.py --run-id {run_id}` — a settled "
        "conversation clears this doc automatically.", ""])


def _line_content_key(entry: dict) -> str:
    """FULL-CONTENT identity of one manifest line, ``ts`` aside — the WRITE
    dedup's key (attempt 11, Codex finding: forged manifest collision). The
    manifest file is VM-writable, so a planted line sharing only the
    (msg_key, filename) name key must never stop the bridge's real line from
    landing — the attacker-authored line would then be the only durable
    attachment intent the sweep consumes. Untrusted bytes may CONFIRM the
    exact line the bridge is about to write, never substitute a different
    one: dedup fires only on a line identical in every field except `ts`."""
    return json.dumps({k: v for k, v in entry.items() if k != "ts"},
                      ensure_ascii=False, sort_keys=True)
