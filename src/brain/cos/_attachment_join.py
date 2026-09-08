"""The BYTES lane's own join: manifest line -> released payload -> signed note.

WHY THIS IS A SEPARATE MODULE AND A SEPARATE CLAIM (ATT-03, 2026-09-05).
``Brainiac · Ingested`` says the vault signed a note for a thread's candidate.
It says nothing about WHICH lane produced that note, and a reader of the
mailbox cannot tell a thread whose TEXT was captured from one whose PDF was.
Measured on the reference host as of run ``2026-09-05-run260``: 156 threads
carry the chip, 51 of them carry a non-inline attachment, and 33 of those 51
ever reached the sweep's claim — the other 18 were chipped on the text lane
alone while their files sat in a staging directory nothing read.

So the chip is NOT widened. This module adds a SECOND, separately recorded
claim — ``cos_attachment_join/v1`` rows under the host-private attachments
directory — that names the whole chain for one file: the manifest line the
bridge wrote, the CONTENT HASH of the payload the host released into
``vault/inbox/``, and the note id the ingest drain minted for exactly those
bytes.

EVERY JOIN IN HERE IS ON A CONTENT HASH. Not on a filename: a name comparison
over this vault's own history matched 13 files that turned out to be earlier
ingests by other paths on other dates. Not on a per-run ident either: an ident
join on this project once fabricated 55 losses of which exactly 1 was real. The
manifest-line key participates only as the sweep's idempotency key — it says
WHICH LINE was claimed, never which bytes are which.
"""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._io import _append_jsonl, _read_jsonl
from ._layout import _ts

#: The schema of one bytes-join claim row. A reader that finds this name knows
#: it is looking at the FILE lane's evidence, never the text chip's.
ATTACHMENT_JOIN_SCHEMA = "cos_attachment_join/v1"


def _note_conversation(text: str) -> str:
    """The `provenance.conversation_id` a note's FRONTMATTER names, if any.

    THE SCAN STOPS AT THE CLOSING ``---``, and that is the whole point of this
    function (review pass 10, 2026-09-05). The body of a ``raw/`` attachment
    note is the emailed FILE'S OWN CONTENT — a spreadsheet, a PDF's text, an
    email chain someone sent in — so a body line reading
    ``provenance.conversation_id: <thread>`` is written by the sender, not by
    the drain. While this value only ever REFUSED a contradiction that was
    survivable; `_joined_row` now also reads it as a host WITNESS granting a
    join, and an attacker-supplied line that grants authority is the confused
    deputy again, one clause over. Probe: thread A holds a provenance-less
    note whose BODY names thread B, thread B appends one manifest line and one
    claims row copying A's ``dest`` -> B read ``joined`` and was chipped
    ``Ingested`` with its own bytes never fetched.

    A note with no frontmatter at all answers "" — the same as one whose
    frontmatter is silent, which is the fail-closed direction.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        if line.strip() == "---":
            return ""
        if line.startswith("provenance.conversation_id:"):
            return line.split(":", 1)[1].strip()
    # No closing `---` inside the bytes we read: the frontmatter is either
    # unterminated or longer than the slice, and neither is a witness.
    return ""


#: What a conflicting pair of claims rows for ONE manifest line collapses to.
#: Never a state a real sweep writes, and `_line_state` reads it as UNCLAIMED.
CLAIM_CONFLICT = "conflict"


def _sweep_claim_dests(vault) -> dict[str, dict[str, str]]:
    """Manifest-line key -> what the SWEEP recorded about that line.

    ``ingest_sweep`` writes one claims row per manifest line it consumed, keyed
    by ``_manifest_line_key`` (the same idempotency key), and only a line whose
    file was really found, validated and moved carries a ``dest``.

    FIRST ROW WINS, AND A CONFLICTING SECOND ROW POISONS THE KEY (review
    2026-09-05, Codex finding on this file). The claims ledger is
    MOUNT-RESIDENT — ``drop_dir(vault)/ingest-manifest/claims.jsonl``, in the
    tree the untrusted leg writes — and last-row-wins let an appended line
    silently replace the host sweep's own record of a key. It is append-only by
    construction and the sweep writes each key once, so a second row that
    DISAGREES is damage or forgery either way; both readings are answered by
    refusing the key rather than picking a winner. An identical repeat (the
    crash-retry shape ``_write_manifest_lines`` documents) is not a conflict.

    The DISPOSITION STRING ON THESE ROWS IS NOT READ FOR ANY DECISION — see
    :func:`_line_state`. It is returned so a report can show it, and that is
    all it is good for.
    """
    from ._attachment_store import _sweep_claims_path            # noqa: PLC0415

    out: dict[str, dict[str, str]] = {}
    for entry in _read_jsonl(_sweep_claims_path(vault)):
        key = str(entry.get("key") or "")
        if not key:
            continue
        row = {"dest": str(entry.get("dest") or ""),
               "disposition": str(entry.get("disposition") or "")}
        prior = out.get(key)
        if prior is None:
            out[key] = row
        elif prior != row and prior.get("disposition") != CLAIM_CONFLICT:
            out[key] = {"dest": "", "disposition": CLAIM_CONFLICT}
    return out


def _line_state(vault, claim: dict[str, str] | None, *, key: str, aid: str,
                joined: bool, settled: dict[str, str]) -> str:
    """What became of ONE manifest line's file.

    TWO THINGS SETTLE A LINE AND BOTH DESIGNATE IT. ``joined`` is the
    content-hash chain, computed here and written down by nobody. Everything
    else the host ever RECORDS settling — the sweep's decline, a claimed
    payload leaving the funnel — arrives as one row in one ledger keyed by
    THIS LINE'S OWN KEY (:func:`brain.cos.line_settlements_path`), which the
    host derives from the bridge's manifest entry.

    NOTHING THE MOUNT SUPPLIES SETTLES ANYTHING (redesign 2026-09-05). The
    claim's ``dest`` still reaches this function, and it still decides between
    two UNSETTLED answers — a payload sitting in quarantine reads
    ``in-funnel``, everything else reads ``unclaimed``. A forged ``dest`` can
    therefore make a line look like it is waiting for a different reason. It
    cannot make a line look settled, which is the only direction that matters:
    both answers keep the thread out of the chip.
    """
    if joined:
        return LINE_JOINED
    recorded = settled.get(key)
    if recorded:
        return recorded
    if claim is None or not claim.get("dest") or not aid:
        # Unclaimed, or claimed with no destination, or the key is poisoned by
        # a conflicting row. Still owed — the fail-CLOSED direction.
        return LINE_UNCLAIMED
    from ._attachment_store import (                             # noqa: PLC0415
        _attachment_lifecycle, _attachment_meta_path)
    if _attachment_lifecycle(vault, aid) or _attachment_meta_path(
            vault, aid).exists():
        return LINE_IN_FUNNEL
    return LINE_UNCLAIMED


def attachment_lane_context(vault, run_id: str) -> dict[str, Any]:
    """ONE walk of this run's manifest lines -> the whole file-lane picture.

    Returns ``{"joins": [...], "lines": {conversation_id: [state, ...]},
    "covered": {conversation_id: [filename, ...]}}``. ``covered`` names the
    file each SETTLED line was written for, because a settlement pays for the
    attachment it NAMES and never for a different one — see
    :func:`attachment_lane_pending`.
    Both halves come out of the same pass because both callers ask about the
    same lines and the walk is the expensive part: `signed_ingest_notes` is a
    full-vault scan and the ingest drain's manifest is re-read per sha without
    this, which is four minutes per row rather than one read per run.

    ``joins`` rows carry the chain end to end — the manifest line's key and
    ``msg_key``, the filename the bridge named, the quarantine id, the
    CONTENT HASH of the released payload, and the note the drain minted for
    exactly those bytes.
    """
    from ._attachment_store import (                             # noqa: PLC0415
        _attachment_lifecycle, _ingested_raw_id, _manifest_line_key,
        ingest_manifest_dir, line_settlements)
    from ._guards import _safe_basename                          # noqa: PLC0415

    root = config.vault_root(vault)
    claims = _sweep_claim_dests(vault)
    settled = line_settlements(vault)
    notes: dict[str, str | None] = {}
    joins: list[dict[str, Any]] = []
    lines: dict[str, list[str]] = {}
    covered: dict[str, list[str]] = {}
    # ONE STATE PER DISTINCT LINE, NOT PER ENTRY (2026-09-05): coverage compares
    # len(files) to len(states), so a repeated line hides an unoffered file.
    seen: dict[str, set[str]] = {}
    paid: dict[str, set[str]] = {}
    for mf in sorted(ingest_manifest_dir(vault).glob("manifest-*.jsonl")):
        for entry in _read_jsonl(mf):
            msg_key = str(entry.get("msg_key") or "")
            if not msg_key.startswith(f"{run_id}:"):
                continue
            cid = str((entry.get("provenance") or {}).get(
                "conversation_id") or "")
            if not cid:
                continue
            key = _manifest_line_key(entry)
            if key in seen.setdefault(cid, set()):
                continue
            seen[cid].add(key)
            claim = claims.get(key)
            # INT-05: the sweep's `dest` is mount-resident, so it is reduced to
            # a bare filename that cannot name anything outside the lifecycle
            # directory the HOST derives — never resolved and compared.
            leaf = _safe_basename(Path((claim or {}).get("dest") or "").name)
            aid = Path(leaf).stem if leaf else ""
            life = _attachment_lifecycle(vault, aid) if aid else {}
            sha = str(life.get("sha256") or "")
            # THE HOST'S OWN ANSWER TO "WHICH LINE IS THIS PAYLOAD FOR",
            # written at claim time into a 0700 record under `host/`. SAY WHAT
            # THAT IS AND IS NOT (adversarial review pass 2, 2026-09-05):
            # `attachment_joins_path` is honest that `host/**` is still ON the
            # mount — "a convention and a mode bit, not a boundary" — and this
            # comment used to call the same directory host-private without the
            # qualifier. What the record binds is PAYLOAD to LINE. It does not
            # bind LINE to THREAD: the host writes it from the manifest entry,
            # and the untrusted leg writes the manifest. The thread binding
            # comes from `attachment_lane_pending`'s name match instead.
            bound = str(life.get("manifest_line_key") or "")
            if sha and sha not in notes:
                notes[sha] = _ingested_raw_id(vault, sha)
            nid = notes.get(sha) if sha else None
            row = _joined_row(root, entry, cid=cid, msg_key=msg_key, key=key,
                              aid=aid, sha=sha, nid=nid, run_id=run_id,
                              bound=bound)
            if row is not None:
                joins.append(row)
            state = _line_state(vault, claim, key=key, aid=aid,
                                joined=row is not None, settled=settled)
            # ONE PAYLOAD JOINS ONE LINE (review 2026-09-05). `joined` is the
            # one settled state the mount still helps designate — the claim's
            # `dest` picks the payload whose hash is looked up — so a SECOND
            # line naming the same `dest` used to read `joined` too, and one
            # file's bytes covered two documents. The other settled states no
            # longer need this: they are keyed by the line itself.
            if state == LINE_JOINED and sha in paid.setdefault(cid, set()):
                state = LINE_UNCLAIMED
            elif state == LINE_JOINED:
                paid[cid].add(sha)
            lines.setdefault(cid, []).append(state)
            if state in LINE_SETTLED:
                covered.setdefault(cid, []).append(
                    _safe_basename(str(entry.get("filename") or "")))
    return {"joins": joins, "lines": lines, "covered": covered}


def _joined_row(root: Path, entry: dict[str, Any], *, cid: str, msg_key: str,
                key: str, aid: str, sha: str, nid: str | None,
                run_id: str, bound: str = "") -> dict[str, Any] | None:
    """One join row, or ``None`` when the chain does not reach a signed note.

    SOMETHING THE HOST WROTE MUST NAME THIS LINE OR THIS THREAD. The chain up
    to here runs payload -> content hash -> note, and the mount picks the
    payload: the sweep's claim supplies ``dest``, so an appended claims row
    copying ANOTHER thread's ``dest`` reaches that thread's note. Two host
    witnesses are accepted, and silence is not:

    1. ``bound`` — the lifecycle record's ``manifest_line_key``, written at
       claim time from the bridge's own manifest entry into a 0700 host
       record. It must equal THIS line's key. Every payload claimed from
       2026-09-05 on carries it.
    2. the note's ``provenance.conversation_id``, which must name THIS thread.
       The drain stamps it from the same host-side claim.

    This docstring used to say the opposite — that step 5 refuses only a
    CONTRADICTION, never a silence — and reasoned that "provenance is already
    established by the manifest line, the sweep's claim of it and the payload's
    content hash". THE SWEEP'S CLAIM IS ON THE MOUNT, so that sentence made the
    untrusted leg a source of provenance and this function a confused deputy
    (Hardy 1988; Miller, erights.org: "don't separate designation from
    authority"). The probe: thread A joins through a provenance-less note,
    thread B appends one manifest line and one claims row copying A's ``dest``,
    and B read ``joined`` too.

    MEASURED COST, 2026-09-05, on the reference host over all 42 runs: of 431
    join rows, 384 have a note that names their own thread and keep joining,
    0 have a note naming a different thread, and 47 — spread over 6 threads —
    have a note with no provenance at all and now stop joining until their
    payload is re-ingested. Those threads go back to WAITING, which is the
    direction that costs the owner attention rather than bytes. (The old
    docstring's "13" was measured on 2026-08-25 over a smaller corpus and was
    stale; this number was re-run at the time of writing.)
    """
    if not nid:
        return None
    note = next((p for p in (root / "raw" / f"{nid}.md",
                             root / "brain" / f"{nid}.md") if p.is_file()),
                None)
    if note is None:
        return None
    named = _note_conversation(note.read_bytes()[:3000].decode("utf-8",
                                                              "replace"))
    if named and named != cid:
        return None
    if bound != key and named != cid:
        return None
    return {"schema": ATTACHMENT_JOIN_SCHEMA, "run": str(run_id),
            "conversation_id": cid, "msg_key": msg_key,
            "manifest_line_key": key, "lane": LANE_ATTACHMENT,
            "filename": provenance.sanitize_value(entry.get("filename")),
            "attachment_id": aid, "sha256": sha, "note_id": nid,
            "note_path": str(note.relative_to(root))}


__all__ = ['ATTACHMENT_JOIN_SCHEMA', 'LINE_JOINED', 'LINE_DECLINED',
           'LINE_WITHDRAWN', 'LINE_IN_FUNNEL', 'LINE_UNCLAIMED',
           'LINE_SETTLED', 'CLAIM_CONFLICT', '_note_conversation',
           '_sweep_claim_dests', '_line_state',
           'attachment_lane_context', '_joined_row']
