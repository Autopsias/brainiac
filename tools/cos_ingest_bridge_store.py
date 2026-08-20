"""Bridge IDENTITY keys, the host-private store, and the manifest write lane.

Moved verbatim out of `cos_ingest_bridge` (file-size ratchet split); every
comment travels with its code and no behaviour changes. The facade re-imports
every name, so `tools.cos_ingest_bridge` keeps exporting them.
Covered by the `tests/test_cos_ingest_bridge_*.py` slices.
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

from brain import config, cos
from brain.notes import safe_slug

from tools.cos_ingest_bridge_content import (
    _claims, _line_content_key, _manifest_line)


def bridge_ledger_path(vault, run_id: str) -> Path:
    """One row per candidate. Named `_cos_ingest_bridge_`, NOT
    `_cos_ingestion_`: the candidate ledger's six readers anchor on that exact
    prefix and would double-count a second file. Never indexed (INT-03)."""
    return cos.run_ops_dir(vault) / f"_cos_ingest_bridge_{run_id}.jsonl"


def _conv_key(cid: str) -> str:
    """The RUN-INDEPENDENT conversation key every bridge ident ends in —
    ONE definition, the engine's (E16 re-derives it to look settlement
    claims up in the host record, attempt 13)."""
    return cos.bridge_conversation_key(cid)


def _bridge_ident(run_id: str, cid: str) -> str:
    return safe_slug(f"cosbridge-{run_id}-{_conv_key(cid)}")


def _attachment_shape(row: dict) -> tuple:
    """(basename, claimed size) per attachment, sorted. The size rides the
    identity (attempt 16, finding 4): names alone collapsed two rows naming
    the same filenames with DIFFERENT content — exactly what a mail thread
    produces — and silently dropped the second's work. A size claim
    `_manifest_line` would not record (non-int or <= 0) normalizes to 0, so
    the shape and the manifest line agree on what a size claim IS."""
    out = []
    for a in row.get("attachments") or []:
        if not isinstance(a, dict):
            continue
        name = cos._safe_basename(str(a.get("filename") or ""))
        if not name:
            continue
        size = a.get("approx_size_bytes")
        out.append((name, size if isinstance(size, int) and size > 0 else 0))
    return tuple(sorted(out))


def _row_shape(row: dict) -> tuple:
    """What makes two candidate rows for ONE conversation the SAME work —
    THE one rule (attempt 15, Codex :686-690; sizes added attempt 16): the
    fields the drop content and the attachment lane are built from —
    category (which derives the content choice and the tier floor), the
    tier claim, and the attachment set as (basename, claimed size) pairs.
    Two rows agreeing on these are rule-1 idempotency; two rows diverging
    are DIFFERENT work, and collapsing them silently dropped the second."""
    return (str(row.get("category") or "").strip(),
            str(row.get("classification") or "").strip(),
            _attachment_shape(row))


#: The three ledger-row settlement CLAIMS E16 reads (`settlement_claim`).
_SETTLEMENT_CLAIM_KEYS = ("bridge_quarantined", "bridge_refused",
                          "bridge_duplicate_of")


def _claim_settlement(row: dict, field: str, value: str) -> None:
    """Stamp ONE settlement claim on the ledger row, clearing the others.

    E16 matches the row's claimed KIND against the latest pass's host
    record, and `settlement_claim` reads the fields in a fixed order — so a
    stale claim left over from a pass that settled this row differently
    (quarantined last pass, a duplicate this pass) would out-rank the current
    one, mismatch the recorded kind, and score a genuinely settled row as
    forged. One claim on the row, the one this pass recorded."""
    for k in _SETTLEMENT_CLAIM_KEYS:
        row.pop(k, None)
    row[field] = value


# -- the host-private store (attempt 14: no delivery receipts live here) ------
#
# Attempts 12-13 kept per-conversation DELIVERY RECEIPTS in this store; the
# collapse deleted the delivery verdict they fed, and the receipts with it.
# The store itself stays, because two things that are NOT delivery evidence
# still earn their place in it: the manifest write-dedup records below (a
# planted mount twin must not pre-empt the real manifest line, attempts
# 11/13), and the ENGINE's settlement records (`cos.record_bridge_settlement`,
# E16's stamp exemption). It lives under ``config.host_private_base()`` — the
# ONE definition (INT-05) the approved queue, the attachment acceptance
# anchors, the single-writer lock and the supersede crash journal share —
# resolved and PROVEN off every VM-visible root by ``config.proven_off_mount``.


def receipts_root(vault) -> Path:
    """The bridge's host-private dir for THIS vault, proven off-mount.

    ONE definition, and it lives in the ENGINE (``cos.bridge_receipts_root``):
    E16's settlement exemption reads the settlement records in this same
    store, and a second copy of the path rule is how the first ends up subtly
    weaker. Same construction as the approved queue: ``host_private_base() /
    <dirname> / vault_slug8`` — the per-vault identity is the hash of the
    RESOLVED VAULT PATH, never the mount-resident ``.brain/vault-id`` a VM
    can rewrite. Raises ``config.HostPathUnsafe`` when ``$BRAIN_INDEX_DIR``
    (or a symlink) resolves back onto the mount — the caller REFUSES rather
    than fall back to a VM-reachable location. The name is historical
    (attempt 12's delivery receipts, deleted in attempt 14); what it holds
    now is the manifest write-dedup records and the engine's settlements."""
    return cos.bridge_receipts_root(vault)


def _receipts_ensure(vault) -> Path:
    """Create the host-private dir (0700). Only WRITE paths call this — a read
    must not materialise host state as a side effect (the approved-queue
    precedent, `cos._approved_ensure`)."""
    d = receipts_root(vault)
    d.mkdir(parents=True, exist_ok=True)
    config.secure_file_permissions(d, 0o700)
    return d


def _manifest_record_path(vault, run_id: str) -> Path:
    """The host-private record of the manifest-line content keys THIS bridge
    wrote for one run — the WRITE-DEDUP authority (attempt 12, Codex finding
    :1027-1037). The mount manifest is VM-writable, so deduping against ITS
    lines let a planted twin — identical in every field except ``ts``, the
    field the sweep's stale-namesake rule reads — pre-empt the real write and
    become the only durable attachment intent. Dedup now consults only what
    the host itself recorded writing."""
    return receipts_root(vault) / f"manifest-{safe_slug(run_id)}.jsonl"


def _write_manifest_lines(vault, run_id: str, row: dict, crow: dict | None, *,
                          names: list[str], tier: str, path: Path,
                          known_keys: dict[str, set[str]], now: _dt.datetime,
                          outcome: dict | None = None) -> list[str]:
    """ING-02: one manifest line per attachment. The dedup key is still FULL
    content (`_line_content_key`, `ts` aside) so a re-run writes none twice —
    and the dedup SET (attempt 13) is the host-private record of lines the
    bridge itself wrote INTERSECTED with the mount manifest's current lines:
    no planted mount line can pre-empt the real write (attempt 12), and no
    host record can certify a line since deleted from the VM-writable
    manifest — a recorded-but-absent line is RE-WRITTEN, never presumed
    still there. ORDER per line: mount append first,
    host record second — a crash between them re-appends one identical line
    on retry (harmless: the sweep claims per line and the file arrives once);
    the reverse order would record a line that never landed. Given an
    ``outcome``,
    it also records exactly the lines THIS pass wrote on the row and the
    outcome — the recording is the write's own tail, not a second step a
    caller can forget."""
    written: list[str] = []
    for name in names:
        entry = _manifest_line(run_id, row, filename=name, tier=tier,
                               prov=_claims(row, crow), now=now)
        key = _line_content_key(entry)
        if key in known_keys["content"]:
            continue    # HOST-recorded AND still on the mount manifest
        path.parent.mkdir(parents=True, exist_ok=True)
        cos.append_jsonl(path, entry)
        if key not in known_keys["recorded"]:
            _receipts_ensure(vault)
            cos.append_jsonl(_manifest_record_path(vault, run_id),
                             {"key": key})
            known_keys["recorded"].add(key)
        known_keys["content"].add(key)
        written.append(name)
    if outcome is not None:
        if written:
            outcome["manifest_lines"] = written
        if names:
            # EVERY intended name is durably on the manifest now — written
            # this pass or deduped against an earlier pass's line. Recording
            # only `written` lost the crash-retry case: the retry's dedup
            # wrote nothing, the row stayed blank, and a later manifest loss
            # for this candidate could never be detected.
            row["attachment_manifest"] = sorted(
                set(row.get("attachment_manifest") or []) | set(names))
    return written


def _known_manifest_keys(vault, run_id: str, path: Path) -> dict[str, set[str]]:
    """The key sets for this run, each from the source that can be trusted
    for ITS job (attempt 12; dedup re-keyed attempt 13):

    - ``recorded`` is the HOST-PRIVATE record of lines the bridge itself
      wrote (`_manifest_record_path`).
    - ``content`` (WRITE DEDUP) is ``recorded`` INTERSECTED with the lines
      currently on the mount manifest. The host record is NECESSARY — a
      mount line alone must never fire the dedup (attempt 11: a planted twin
      identical in every field except ``ts`` pre-empted the real write,
      leaving the attacker's forged ``ts`` as the only durable one) — and
      mount presence is also necessary, because a record that a line was
      WRITTEN is not evidence the line IS THERE (attempt 13: a line deleted
      from the VM-writable manifest after the record was made was certified
      forever, and the attachment never travelled). Each side of the pair
      can only force a RE-WRITE, never suppress one.
    """
    out: dict[str, set[str]] = {"content": set(), "recorded": set()}
    rpath = _manifest_record_path(vault, run_id)
    if rpath.is_file():
        for x in rpath.read_text(encoding="utf-8").splitlines():
            if x.strip():
                try:
                    key = json.loads(x).get("key")
                except ValueError:
                    continue          # a torn record line is not a write
                if key:
                    out["recorded"].add(str(key))
    if not path.is_file():
        return out
    mount_content: set[str] = set()
    # THE MOUNT MANIFEST IS UNTRUSTED INPUT (attempt 16, finding 5): a torn
    # or hostile line here used to escape `_bridge_locked` as a raw
    # ValueError and kill the nightly as "the ingest bridge refused the run"
    # — false, and a VM-writable denial of service. A line that does not
    # parse to a dict is not a manifest line: it cannot CONFIRM a recorded
    # write, so the dedup fails toward RE-WRITING (the documented safe
    # direction — worst case one duplicate identical line the sweep claims
    # once). An unreadable file is the same: no confirmations, re-write all.
    try:
        mount_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for x in mount_lines:
        if not x.strip():
            continue
        try:
            entry = json.loads(x)
        except ValueError:
            continue
        if isinstance(entry, dict):
            mount_content.add(_line_content_key(entry))
    # THE DEDUP SET IS THE INTERSECTION (attempt 13, Codex :812-831). A host
    # record that a line was WRITTEN is not evidence the line IS THERE: the
    # mount manifest is VM-writable, so a line deleted after the record was
    # made would be certified forever and the attachment would never travel.
    # Host-recorded AND currently on the mount -> skip; recorded but absent
    # -> RE-WRITE (fail toward re-writing, worst case one duplicate identical
    # line the sweep claims once); present but never host-recorded -> still
    # write (attempt 11: a planted twin must not pre-empt the real write).
    out["content"] = out["recorded"] & mount_content
    return out


def _consumed_shas(vault) -> set[str]:
    """THE CLAIM PATH'S REPLAY AUTHORITY, read once per pass exactly as the
    sweep builds it (`_claim_text_drops`: every `sha256` in the claims
    ledger, whatever its disposition): a drop whose exact bytes are in this
    set is deleted unseen at sweep time ("replay-rejected"). Read only to
    turn that silent deletion into a loud quarantine after the drop is
    written (`_execute_fresh_drop`) — never to skip one."""
    consumed = {str(e.get("sha256") or "")
                for e in cos._read_jsonl(cos._claims_path(vault))}
    consumed.discard("")
    return consumed
