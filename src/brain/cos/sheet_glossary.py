"""The words this sheet uses, explained on the sheet itself.

Owner request 2026-09-07: "a link to some kind of glossary could help too,
things like explaining the held-out sample or the chips."

DELIBERATELY NOT A LINK, and not a second file. A link is one more thing that
can rot, be moved, or point at a page built on a different night; a `<details>`
block ships with the sheet it explains and cannot disagree with it. It is
closed by default, so it costs a reader who already knows the words nothing.

Every entry answers the question the owner would actually ask — "what is this
and what should I do about it" — not what the code calls it.
"""
from __future__ import annotations

import html
from typing import Any

#: What each mark COLUMN asks. Lives here with the other owner-facing
#: wording, and because `sheet_render` sits against the 500-LOC bound.
COLUMN_MEANS = (
    ("judgment", "Judgment",
     "What should have happened to this thread — act on it, read it, or bin "
     "it as noise — and at what priority. Leave it alone if the porter got it "
     "right"),
    ("label", "Label",
     "Which category this thread belongs in. The category decides whether it "
     "is recorded in the vault at all"),
    ("draft", "Draft",
     "Whether the reply the porter wrote can be sent as it stands, and if not, "
     "what is wrong with it"),
)

#: (term, what it means to the owner). Ordered by when he meets it on the page.
TERMS: tuple[tuple[str, str], ...] = (
    ("act / read / noise",
     "The porter's three buckets. `act` means it thinks you owe someone "
     "something. `read` means it is information for you, with nothing owed. "
     "`noise` means it does not think you need it at all. Only `read` and "
     "`noise` can ever be archived automatically."),
    ("P0 · P1 · P2 · P3",
     "How urgent the porter judged the thread, P0 highest. Since your ruling "
     "of 2026-09-04, priority no longer protects a thread from being archived: "
     "a thread with no action for you is archived whatever its priority."),
    ("chip",
     "A coloured category tag the porter puts on the thread in Outlook itself, "
     "so the judgment is visible in your mailbox and not only here. An "
     "`Ingested` chip means the thread's TEXT reached a signed note in the "
     "vault. It says nothing about attachments — those are a separate lane."),
    ("held / hold",
     "The porter decided not to act and said why. A hold is a decision, not a "
     "failure: `Held · chip`, `Held · uncertain`, `Held · ask`, "
     "`Held · protected`."),
    ("held-out sample",
     "A few threads the sheet deliberately shows you WITHOUT the porter's own "
     "judgment attached, drawn at random each night. They are the only rows "
     "where your answer cannot be influenced by having read its answer first, "
     "which is what makes the rest of the sheet measurable rather than a "
     "popularity contest."),
    ("draft",
     "A reply the porter wrote and left in your Outlook Drafts. It is never "
     "sent. If you rewrite it before sending, the porter notices and records "
     "how far your version drifted from its own."),
    ("standing rule",
     "Your correction, generalised: quoted into every future night's "
     "instructions, applied to all mail rather than one thread, and expired "
     "after 90 days unless it keeps being confirmed."),
    ("note",
     "Your correction in your own words, about this one thread. Since "
     "2026-09-07 the judge reads it on every later night alongside your ruling "
     "and is told to follow it where it applies."),
    ("ingested",
     "The thread's text was written into the vault as a signed note, so it is "
     "searchable and can be cited later. Archiving is a separate step."),
    ("the door check",
     "The porter's test, immediately before it changes anything in your "
     "mailbox, that it is still signed in with enough time left to finish. "
     "It stops rather than half-acting."),
)


def capture_html(cap: dict[str, Any]) -> str:
    """The two lanes as the owner reads them on the card.

    Rendered here rather than in `sheet_render` so the words and the facts that
    produced them live in one file — and because that module sits against the
    500-LOC bound.
    """
    if not cap:
        return ""
    why = str(cap.get("body_why") or "")
    count = int(cap.get("files_count") or 0)
    return (
        '<div class="capture"><span class="field">Into the vault</span>'
        f'<span class="cap-body">body: {html.escape(str(cap.get("body") or ""))}'
        + (f" ({html.escape(why)})" if why else "")
        + "</span>"
        f'<span class="cap-files">files: {html.escape(str(cap.get("files") or ""))}'
        + (f" ({count})" if count else "")
        + "</span></div>")


def glossary_block() -> str:
    """The glossary, closed by default, ready to drop into the sheet."""
    rows = "".join(
        f"<div><b>{html.escape(term)}</b><span>{html.escape(meaning)}</span></div>"
        for term, meaning in TERMS)
    return ('<details class="glossary"><summary>What do these words mean?</summary>'
            f'<div class="howto">{rows}</div></details>')


__all__ = ["COLUMN_MEANS", "TERMS", "capture_html", "glossary_block"]
