"""DD-01 — what this vault has ALREADY SIGNED for a bridge conversation.

One join, in the direction the ingest bridge needs, kept out of
:mod:`brain.cos._learning_ledger` so neither file has to grow for the other.
"""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._attachment_join import _note_fields
from ._io import _read_jsonl
from ._learning_ledger import _claims_path

#: The signed-note frontmatter the bridge's cross-run rule reads, AFTER the
#: note-bytes join has vouched for the note. `cos.source_sha256` is the sha
#: of the CAPTURED MESSAGE TEXT, not of the note.
_BRIDGE_NOTE_KEYS = ("cos.source_sha256", "cos.run",
                     "provenance.conversation_id")


def _run_order(run_id: str) -> tuple[str, int]:
    """(day, run number) — ``run9`` precedes ``run10``, which a plain string
    compare gets backwards on a day with ten passes."""
    m = _RUN_NUMBER_RE.search(str(run_id))
    n = int(m.group(1)) if m and len(m.group(1)) <= MAX_RUN_DIGITS else 0
    return (str(run_id)[:10], n)


def signed_bridge_notes(vault) -> dict[str, list[dict[str, str]]]:
    """Bridge CONVERSATION KEY -> the SIGNED notes this vault holds for it.

    The cross-run half of :func:`ingest_signed_row`: not "did THIS run's
    candidate get signed" but "what has this thread already had signed,
    whichever night dropped it" — what stops the bridge re-filing an
    unchanged thread nightly (1571 notes for 190 conversations, reference
    host 2026-09-10). ONE FULL VAULT WALK, ~1.2s for 3,993 notes: build it
    once per pass and pass it down; per row it would be minutes.

    THE SAME TWO-KEY JOIN, AND THE KEYS ARE NOT INTERCHANGEABLE. SIGNED is
    proved exactly as :func:`signed_ingest_notes` proves it: sha256 OF THE
    NOTE FILE'S OWN BYTES against a sha the host-private claims ledger
    recorded. Only THEN is the note's own frontmatter read — and that is what
    makes reading it legitimate, because frontmatter alone is a claim
    (STA-01) and the first attempt at this index keyed on `cos.source_sha256`
    and matched nothing at all. A signed note carrying no `cos.source_sha256`
    appears with that field empty: it can never match a candidate's text, so
    it falls through to a fresh drop, never a silent skip — and stays
    available as a PREDECESSOR."""
    from ._proposal_state import bridge_conversation_key   # noqa: PLC0415

    claimed = {str(e.get("sha256") or "")
               for e in _read_jsonl(_claims_path(vault))}
    claimed.discard("")
    out: dict[str, list[dict[str, str]]] = {}
    if not claimed:
        return out
    root = config.vault_root(vault)
    for sub in ("brain", "raw"):
        for note in (root / sub).rglob("*.md"):
            try:
                body = note.read_bytes()
            except OSError:
                continue
            if hashlib.sha256(body).hexdigest() not in claimed:
                continue
            fields = _note_fields(body[:3000].decode("utf-8", "replace"),
                                  _BRIDGE_NOTE_KEYS)
            cid = fields.get("provenance.conversation_id", "")
            if not cid:
                continue
            out.setdefault(bridge_conversation_key(cid), []).append(
                {"id": note.stem,
                 "source_sha256": fields.get("cos.source_sha256", ""),
                 "run": fields.get("cos.run", "")})
    for notes in out.values():
        notes.sort(key=lambda n: (_run_order(n["run"]), n["id"]))
    return out


__all__ = ['signed_bridge_notes']
