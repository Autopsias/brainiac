"""The FILE GATE: is this thread's attachment lane finished, and if not, why.

SPLIT OUT OF ``_attachment_join.py`` on 2026-09-05, when the second adversarial
review pass took that file past the 500-line production limit. The two halves
answer different questions and share only one call:

* ``_attachment_join`` builds the PICTURE — one walk of a run's manifest lines
  producing each line's state and the bytes-join chain behind it;
* this module asks the QUESTION ATT-03 exists for — given that picture and a
  thread's own ledger row, may this thread be called finished?

The split follows the dependency, which runs one way: the gate calls
:func:`~brain.cos._attachment_join.attachment_lane_context` and the context
never calls back.
"""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._attachment_join import attachment_lane_context

def signed_attachment_conversations(vault, run_id: str) -> set[str]:
    """Conversations whose ATTACHMENT, dropped by THIS RUN, the vault SIGNED.

    The file lane's whole authority, and it walks the production chain end to
    end rather than asking a question any old note could answer:

    1. THIS RUN's ingest-manifest line (``<run_id>:<message key>``) names the
       conversation — the bridge's own record of offering that file;
    2. the sweep CLAIMED that exact line (a ``dest``): the file was fetched,
       validated and moved into attachment quarantine;
    3. the attachment's lifecycle record carries the content sha of those
       bytes — written when the payload was RELEASED into ``vault/inbox/``;
    4. the ingest drain minted a note for exactly that sha
       (``_ingested_raw_id``, the drain's own original-sha -> note-id map);
    5. that note is on disk and its own provenance does not name a DIFFERENT
       conversation.

    A LINE ALONE IS AN OFFER, WHICH IS THE BUG. Until 2026-08-25 this lane
    asked only (1) plus "does any vault note name this conversation" — and
    ``index.values()`` is every note in the vault, so a note from an earlier
    night chipped a thread whose PDF tonight merely offered. Caught in review
    with that exact probe: one manifest line, nothing fetched, nothing signed,
    an older note naming the thread, and the ``Brainiac · Ingested`` mark was
    planned. Steps 2-4 are what make it this run's own signature.
    """
    return {row["conversation_id"]
            for row in attachment_lane_context(vault, run_id)["joins"]}


def _is_attached_message(a: dict[str, Any]) -> bool:
    """An attached EMAIL rather than a file — TWO agreeing signals.

    THE SAME RULE THE BRIDGE APPLIES, restated here on purpose rather than
    imported: ``tools/cos_ingest_bridge_content._is_attached_message`` is the
    original, it lives in a `tools/` script the engine may not import, and the
    two must agree or this gate demands a manifest line the bridge deliberately
    never writes. Kept in sync by
    ``test_attached_message_rule_matches_the_bridge``, which reads the bridge's
    source and re-runs both over the same fixtures.

    Either signal alone is too sharp — a partial capture can omit
    ``content_type`` for a genuine file, and an attached message is named by
    its subject, so it has no extension either. Requiring BOTH skips only what
    looks like neither a typed file nor a named one.
    """
    if a.get("content_type"):
        return False
    stem, dot, ext = str(a.get("filename") or "").rpartition(".")
    return not (dot and stem and 1 <= len(ext) <= 8 and ext.isalnum())


def _owed_attachment_names(row: dict[str, Any]) -> list[str]:
    """The DISTINCT filenames the FILE LANE is expected to carry.

    THE EXPECTED SET HAS TO BE THE BRIDGE'S SET, not "everything the mail
    held". The ledger row keeps every part — inline signature logos included —
    and `_attachment_names` offers a strict subset: non-inline, not an attached
    message, and passed through `_safe_basename`, which DROPS what it empties.
    Counting the rest as owed leaves the thread pending on a manifest line
    nothing was ever going to write, which is a permanent hold, not a gate.

    DISTINCT, FOR THE SAME REASON (review 2026-09-05). `_write_manifest_lines`
    dedups on the whole line, so two attachments sharing ONE filename produce
    one line; owing two of them is the same permanent hold by another route.
    """
    from ._guards import _safe_basename                          # noqa: PLC0415

    return list(dict.fromkeys(
        n for n in (_safe_basename(str(a.get("filename") or ""))
                    for a in (row.get("attachments") or [])
                    if isinstance(a, dict) and not a.get("is_inline")
                    and not _is_attached_message(a)) if n))


def attachment_lane_pending(vault, run_id: str, row: dict[str, Any], *,
                            ctx: dict[str, Any] | None = None) -> str | None:
    """Why this thread's FILE lane is not finished yet, or ``None``.

    THE RULE ATT-03 EXISTS FOR: a thread that carries a real attachment is not
    "taken" while the attachment is still outside the vault. The porter used to
    read such a thread as complete on the TEXT lane alone — the text note was
    signed, the chip went on, and the aged-read lane then archived the mail
    while its PDF sat in a staging directory nothing swept.

    It is NOT a demand that every file be kept. Three outcomes end the wait,
    and all three are things the vault RECORDED doing:

    * the bytes joined a signed note (the content-hash chain above);
    * the sweep declined the line — a never-ingest category, an unsafe name, a
      file over the cap, or a duplicate of bytes another line already carries;
    * the payload left the funnel after being claimed: the owner rejected it in
      his batch, or its TTL expired.

    What does NOT end the wait is silence. A thread whose attachment the bridge
    never offered at all stays pending unless the bridge RECORDED settling it
    (``record_bridge_settlement``, kinds ``never-category``/``duplicate``) —
    absence of a record is the exact shape that made 596 lost candidates
    invisible for 15 runs (ATT-02), and it is not evidence here either.
    """
    # AN UNOPENED BODY IS NOT A WITNESS OF ITS OWN FILE SET (2026-09-05): the
    # row carries `attachments: []` when the body pass was capped, and the
    # planner UNIONS its answer across the window. Measured over 146 runs:
    # 17,284 unopened rows, NOT ONE listing a file.
    if row.get("body_opened") is False:
        return (f"the body of this thread was not opened in {run_id}: its row "
                "lists no attachments because nothing looked, and silence "
                "from an unopened row is not evidence of no files (ATT-03)")
    # NAMES WITHHELD IS NOT "NO FILES" (2026-09-05), the same class as the
    # guard above: the page sets this only when the item reported attachments
    # and returned no list, so `attachments: []` here means files whose names
    # nobody knows. Latent — 0 of 12,455 live rows carrying the field say true.
    if row.get("attachments_withheld"):
        return ("this thread reports attachments whose names the page never "
                "returned, so no manifest line can account for them (ATT-03)")
    files = _owed_attachment_names(row)
    if not files:
        return None
    cid = str(row.get("conversation_id") or "")
    if not cid:
        return None
    lane = ctx or attachment_lane_context(vault, run_id)
    states = (lane["lines"].get(cid) or [])
    waiting = [s for s in states if s not in LINE_SETTLED]
    # A SETTLEMENT PAYS FOR THE FILE IT NAMES (adversarial review pass 2,
    # 2026-09-05). This compared `len(files)` with `len(states)` and matched
    # nothing, so ANY settled line on this thread covered ANY owed file. The
    # untrusted leg picks both the `conversation_id` and the `filename` on a
    # manifest line, so it could append one line attaching a DIFFERENT thread's
    # real download to this thread; the host then declined those bytes on their
    # own merits — over the cap, a duplicate, a never-ingest category — and that
    # honest host decline settled a line for a file this thread never owed.
    # Reproduced: a thread owing `contoso.pdf`, whose bytes were never fetched,
    # went from `1 of 1 attachment(s) ... never offered` to `marks ['victim']`
    # on one appended line naming an unrelated `decoy.bin`. It is the FIFTH
    # instance of the confused deputy in this lane, and the last one that could
    # reach a settled state. Matching by name costs nothing live: of 191 threads
    # that pass this gate on the reference vault, 0 stop passing.
    pool = list(lane.get("covered", {}).get(cid) or [])
    unpaid = []
    for name in files:
        if name in pool:
            pool.remove(name)      # one settled line pays for one owed file
        else:
            unpaid.append(name)
    if not waiting and not unpaid:
        return None
    # The bridge can decide not to offer a candidate at all (never-category, a
    # same-pass duplicate) and it records that HOST-side, per candidate — so it
    # answers for the lines that were never written, and never for one already
    # in the funnel.
    if not waiting and _bridge_settled(vault, run_id, cid):
        return None
    if waiting:
        return (f"{len(waiting)} of {len(states)} manifest line(s) for this "
                f"thread in {run_id} have not reached a signed note: "
                + ", ".join(sorted(set(waiting)))
                + " — the text of this thread may be in the vault, its files "
                  "are not (ATT-03)")
    return (f"{len(unpaid)} of {len(files)} attachment(s) on this thread have "
            f"no settled ingest manifest line of their own in {run_id} ("
            + ", ".join(sorted(unpaid))
            + "): the bridge never offered those bytes and recorded no "
              "settlement, so nothing says whether the vault wanted them "
              "(ATT-03)")


def attachment_lane_withheld(vault, run_id: str,
                             rows: list[dict[str, Any]]
                             ) -> dict[str, str]:
    """conversation id -> WHY its file lane is not finished, for this run.

    THE REASON WAS COMPUTED AND THROWN AWAY (review 2026-09-05). The gate in
    :func:`ingest_signed_row` collapses a full sentence — which lines, in which
    state, how many attachments unaccounted for — into ``False``, and a reader
    of the morning sheet or a maintain report then sees a thread that simply is
    not chipped, with nothing saying it is the FILES that are missing rather
    than the text. This is the same walk the gate does, kept as prose.

    One context for the whole run (the walk is the expensive part), so a caller
    can afford to ask about every row.
    """
    ctx = attachment_lane_context(vault, run_id)
    out: dict[str, str] = {}
    for row in rows:
        cid = str(row.get("conversation_id") or "")
        why = attachment_lane_pending(vault, run_id, row, ctx=ctx)
        if cid and why:
            out[cid] = why
    return out


def _bridge_settled(vault, run_id: str, cid: str) -> bool:
    """Did the BRIDGE record deciding not to offer this thread's files?

    Host-side record (``bridge_receipts_root`` is proven off every VM-visible
    root), so unlike the ledger row's own fields this cannot be claimed by the
    untrusted leg. ``quarantined`` is deliberately not accepted: it means the
    bridge is still holding the candidate, which is a wait, not a decision.

    The store answers COUNTS bounded to the latest bridge pass, and this reads
    them as a presence test rather than spending one per row — E16's cardinality
    bound exists to stop ONE record exempting many FORGED rows, and there is no
    equivalent here: the thing being exempted is this thread's own attachment
    list, off the same row, and a second row for the same conversation asks the
    same question about the same files.
    """
    from ._proposal_state import (                               # noqa: PLC0415
        bridge_conversation_key, bridge_settlements)
    kinds = bridge_settlements(vault, run_id).get(
        bridge_conversation_key(cid)) or {}
    return any(int(kinds.get(k) or 0) > 0
               for k in ("never-category", "duplicate"))


__all__ = ['signed_attachment_conversations', '_is_attached_message',
           '_owed_attachment_names', 'attachment_lane_pending',
           'attachment_lane_withheld', '_bridge_settled']
