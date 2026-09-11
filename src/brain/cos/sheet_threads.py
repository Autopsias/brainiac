"""ONE THREAD'S ROW on the morning sheet, and the words that explain it.

Split out of `sheet.py` on 2026-09-05 for a mechanical reason: that file was at
the 500-line ratchet bound exactly, so the three marks could not be added to a
row without something moving first. Nothing here changed meaning in the split —
`ACTION_MEANS`, `HELD_BECAUSE`, `NEEDS_OWNER_MEANS`, `_why`, `_thread_state`,
`DISPOSITION_MEANS` and `_category_legend` arrived verbatim from `sheet.py`,
and their docstrings still carry the measurements that shaped them.

What IS new is what a row now carries: the judge's `tier`, the draft's own
`draft_text`, three unset marks, and a `rules` list. The marks are UNSET at
build time and that is the invariant this module protects — see
`feedback_sheet._check_marks`.
"""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from .feedback import ACTION_FOR_VERB, subject_digest, thread_digest
from .sheet_render import UNAVAILABLE_SUBJECT as _UNAVAILABLE_SUBJECT

#: What each action word MEANS, in the owner's language rather than the
#: engine's. The sheet asked the owner to mark `chipped` right or wrong without
#: ever saying that `chipped` means "tagged in Outlook, mail not moved".
ACTION_MEANS = {
    "archived": "Moved out of the inbox into Archive.",
    "ingested": "A note was written into the vault. The mail was not moved.",
    "drafted": "A reply was drafted for you. Nothing was sent.",
    "chipped": "Tagged in Outlook. The mail was not moved.",
    "held": "Deliberately left alone.",
    "stale-archived": "Archived because the thread was judged already finished.",
    "none": "Read and passed over. Nothing happened.",
}

#: Why a thread was left alone, in plain words. Keys are the run ledger's own
#: `held_reason` vocabulary; an unknown one is shown verbatim rather than
#: dropped, so a new reason reaches the owner as itself instead of vanishing.
HELD_BECAUSE = {
    "never-category": "its category is on the never-ingest list",
    "no-substance": "there was nothing in it worth recording",
    "over-cap": "the night's budget for opening message bodies ran out",
    "no-body-access-on-lane": "the read lane could not open the body",
    "rest-read-returned-shell": "the server returned an empty message",
    "server-returned-no-body": "the server says this item has no message body",
    "unread-read-state-invariant": "it is still unread, and never touched",
    "rights-protected-message": "it is encrypted, and only you can open it",
}

#: THE THIRD OUTCOME (owner ruling 2026-08-28), in plain words. Every thread the
#: porter judges must produce SOMETHING - an archive, a draft, or this word
#: saying why it could do neither. The keys are the drafting leg's closed
#: vocabulary (`tools/cos_model_answer_schema.NEEDS_OWNER_VOCAB`); an unknown
#: one is shown verbatim, because a word the owner cannot read still tells him
#: more than a silence.
NEEDS_OWNER_MEANS = {
    "owner-decision": "the next move is a judgment only you can make",
    "facts-missing": "a reply needs facts the vault does not carry",
    "not-his-move": "someone else holds the next move",
    "unreadable": "the body never opened, so nothing could be judged",
    "unclear": "the porter could not tell what is being asked",
}


def _why(row: dict[str, Any], action: str) -> str:
    """One plain sentence: what happened to this thread, and why.

    The owner cannot say whether an action was right without the grounds for
    it. Both halves come from the run's own ledger row — nothing here is
    inferred, and an unrecognised code is quoted rather than swallowed.

    `held_reason` is printed ONLY on a hold, and that is a correction, not a
    style choice. The action is joined over the WHOLE night while the row is
    one run's attempt, so a thread held `over-cap` in run190 and ingested in
    run193 keeps run190's hold reason on its row. Printing both produced *"A
    note was written into the vault. Left alone because its category is on the
    never-ingest list"* — two contradictory claims about the same thread, on
    a page whose entire job is to let the owner judge the claim. No ledger row
    is ever both (measured over all four of 2026-08-27's runs, 621 rows: zero
    carry `held_reason` with `ingest.relevant`), so on any other action the
    reason belongs to a superseded attempt and is not this action's grounds.
    """
    parts = [ACTION_MEANS.get(action, action)]
    reason = str(row.get("held_reason") or "")
    because = HELD_BECAUSE.get(reason, reason)
    if reason and action == "held":
        # The chip already says HELD and the lead sentence already says it was
        # left alone. One sentence carrying the reason, not two saying it.
        parts = [f"Left alone because {because}."]
    elif reason and action == "none":
        parts.append(f"Nothing was recorded because {because}.")
    kind = str(row.get("substance_kind") or "")
    tier = str(row.get("classification") or "")
    if action == "ingested" and kind:
        parts.append(
            f"What was recorded: a {kind}"
            + (f", classified {tier}." if tier else ".")
        )
    # THE WORD, ON THE ONLY ACTIONS IT CAN STILL BE TRUE OF. The action is
    # joined over the WHOLE night while the word belongs to ONE run's drafting
    # leg, so the same superseded-attempt hazard the `held_reason` paragraph
    # above describes applies here: a thread the porter could not decide in
    # run195 and archived in run196 must not still read as needing him.
    word = str(row.get("needs_owner") or "")
    if word and action in ("held", "none"):
        parts.append(
            "NEEDS YOU: " + NEEDS_OWNER_MEANS.get(word, word) + ".")
    return " ".join(parts)


def draft_texts(vault: Any, run_ids: list[str]) -> dict[str, str]:
    """THE DRAFT ITSELF, per conversation, from the drafting leg's own file.

    FB-03 asks for the full draft text shown and editable, and the sheet had
    no access to it: the undo ledger records that a draft LANDED, never what it
    said, so the owner was asked to judge a reply he could not read. The text
    lives in `_cos_drafts_pending_<run>.jsonl`, which the drafting leg writes
    before the mutation lane saves anything to the mailbox.

    Later runs win, same as every other join here: the newest attempt for a
    conversation is the draft that is sitting in the mailbox this morning.
    """
    from .. import cos  # noqa: PLC0415  (the facade; avoids an import cycle)
    from .sheet import _read_jsonl_strict  # noqa: PLC0415

    out: dict[str, str] = {}
    ops = cos.run_ops_dir(vault)
    for run_id in run_ids:
        run_id = cos.checked_run_id(run_id)
        path = ops / f"_cos_drafts_pending_{run_id}.jsonl"
        for row in _read_jsonl_strict(path, required=False):
            cid = str(row.get("conversation_id") or "")
            text = str(row.get("text") or "")
            if cid and text:
                out[cid] = text
    return out


#: What reached the vault, as two SEPARATE answers. The owner asked for this on
#: 2026-09-07: "important to differentiate email body ingestion and file
#: attachments ingestion too". They are genuinely different lanes with
#: different authorities — `signed_ingested_catching_up` for the text (the
#: sheet's own, which counts a note signed by a later catch-up run),
#: `signed_attachment_conversations` for the bytes. On `2026-09-07-run269` the sheet's own producers answered
#: differently for 23 of 109 threads: 22 carried files that reached the vault
#: with their body, and 1 was judged worth keeping and did not land at all.
#: Merged into a single word, none of that is visible on the only page the
#: owner reads.
BODY_IN_VAULT = "in the vault"
BODY_NOT_NEEDED = "not needed"
BODY_MISSING = "NOT captured"
FILES_NONE = "none attached"
FILES_IN_VAULT = "in the vault"
FILES_MISSING = "NOT captured"


def _capture(row: dict[str, Any], text_signed: bool,
             files_signed: bool) -> dict[str, Any]:
    """The two lanes, answered separately and never merged into one word."""
    ingest = row.get("ingest") or {}
    files = [str(n) for n in (row.get("attachment_manifest") or [])]
    if text_signed:
        body = BODY_IN_VAULT
    elif not ingest.get("relevant"):
        body = BODY_NOT_NEEDED
    else:
        body = BODY_MISSING
    if not files:
        state = FILES_NONE
    elif files_signed:
        state = FILES_IN_VAULT
    else:
        state = FILES_MISSING
    return {"body": body, "body_why": str(row.get("held_reason") or ""),
            "files": state, "files_count": len(files)}


def _thread_state(
    rows: list[dict[str, Any]],
    subjects: dict[str, str],
    landed: dict[tuple[str, str], dict[str, Any]],
    signed: set[str],
    stale: set[str],
    held: set[str],
    held_out: list[str],
    drafts: dict[str, str] | None = None,
    files_signed: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Project the authoritative run facts into owner-facing thread rows."""
    drafts = drafts or {}
    files_signed = files_signed or set()
    out = []
    for row in rows:
        cid = str(row["conversation_id"])
        subject = subjects.get(cid) or _UNAVAILABLE_SUBJECT
        action = "none"
        for verb in ("archive", "draft", "categorize"):
            if (cid, verb) in landed:
                action = ACTION_FOR_VERB[verb]
                break
        if cid in stale:
            action = "stale-archived"
        elif action == "none" and cid in signed:
            action = "ingested"
        elif action == "none" and cid in held:
            action = "held"
        # A missing subject gets a per-thread sentinel digest. Hashing one
        # shared empty string would make unrelated threads confirm one rule.
        digest_input = (
            subject if subject != _UNAVAILABLE_SUBJECT else f"{subject} · {cid}"
        )
        out.append(
            {
                "conversation_id": cid,
                "conversation_id_digest": thread_digest(cid),
                "subject_sha256": subject_digest(digest_input),
                "subject": subject,
                "action_taken": action,
                # The JUDGE's tier, not the mail's own `tier` field: the
                # question the sheet asks is whether the porter judged this
                # right, and `judged_tier` is what it judged.
                "tier": str(row.get("judged_tier") or ""),
                # UNSET, all four. `verdict` is DERIVED from the marks at
                # read-back; the three columns start empty because a
                # pre-selected control cannot be told apart from a mark.
                "verdict": None,
                "judgment": "",
                "label": "",
                "draft_mark": "",
                "draft_text": drafts.get(cid, ""),
                "rules": [],
                "note": "",
                "held_out": cid in held_out,
                # Set by `sheet_select.apply_selection`, which is the one
                # place the four selection rules live.
                "shown": False,
                "stale_archived": cid in stale,
                # Display-only context, straight off this row. `received` is
                # the mail's own date, not the run's. `files` reads
                # `attachment_manifest` and NOT `attachments`: the latter
                # counts inline images, so it would list `image.png` three
                # times and call them the files that came with the thread.
                "received": str(row.get("received") or ""),
                "category": str(row.get("category") or ""),
                # A re-draft is named as one: the owner read the sixth draft
                # on one thread as a repeat ("wasn't this the same reply?",
                # marks 2026-09-08). The plan discards the superseded draft
                # in the same run, so the discard row is the host's own fact.
                "reason": _why(row, action) + (
                    " This replaces the draft the porter wrote on an earlier "
                    "night; that one was discarded."
                    if action == "drafted" and (cid, "discard-draft") in landed
                    else ""),
                "files": [str(name) for name in
                          (row.get("attachment_manifest") or [])],
                "capture": _capture(row, cid in signed, cid in files_signed),
            }
        )
    return out


#: What a DISPOSITION does, in the owner's terms. The category WORDS are the
#: owner's own — the engine holds no definition of `decision-record` beyond the
#: line in `overlay/cos/ingest.md` that gives it a disposition — so the legend
#: states what the category CAUSES and never invents what it MEANS.
DISPOSITION_MEANS = {
    "always": "always recorded in the vault when there is something to quote",
    "propose": "offered for a decision rather than recorded automatically",
    "never": "never recorded in the vault",
}


def _category_legend(vault, threads: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The categories ON THIS SHEET, each with what its rule does.

    Only the ones present: a legend of 30 categories for a page showing 4 is a
    reference document, not an explanation. Returns EMPTY when the taxonomy is
    off or unparseable — an empty legend and an absent key must never read the
    same, which is why the validator requires the key either way.
    """
    from ._taxonomy import ingest_taxonomy  # noqa: PLC0415

    present = sorted({str(row.get("category") or "") for row in threads} - {""})
    if not present:
        return []
    tax = ingest_taxonomy(vault)
    if tax.get("mode") != "active":
        return []
    legend = []
    for category in present:
        rule = tax["rules"].get(category)
        disposition = str((rule or {}).get("disposition") or "propose")
        legend.append({
            "category": category,
            "disposition": disposition,
            "means": DISPOSITION_MEANS.get(
                disposition, f"disposition {disposition}"),
        })
    return legend


def label_vocabulary(vault, threads: list[dict[str, Any]]) -> list[str]:
    """EVERY category the owner may re-label a thread TO.

    Wider than `_category_legend`, and deliberately: the legend explains the
    categories ON this sheet, while the LABEL column has to offer the ones that
    are not — a thread mislabelled `scheduling-logistics` is corrected by
    naming the category it should have had, which by definition is not the one
    it got. The owner's full taxonomy is that list, plus whatever categories
    tonight's rows actually carry (a category the run emitted but the taxonomy
    no longer names is still a real answer).

    Empty when the taxonomy is off or unparseable, exactly like the legend: the
    LABEL column then offers `right` alone, which is honest — there is no
    vocabulary to correct toward.
    """
    from ._taxonomy import ingest_taxonomy  # noqa: PLC0415

    tax = ingest_taxonomy(vault)
    if tax.get("mode") != "active":
        return []
    present = {str(row.get("category") or "") for row in threads} - {""}
    return sorted(set(tax["rules"]) | present)


__all__ = ['ACTION_MEANS', 'HELD_BECAUSE', 'NEEDS_OWNER_MEANS',
           'DISPOSITION_MEANS', 'draft_texts', 'label_vocabulary']
