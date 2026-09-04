"""The coverage LEDGER: what a handler admitted, and what its walker saw.

Split out of :mod:`concealment_gate` for the 500-LOC file bound, exactly as
``concealment_html``/``concealment_office`` were split out of ``concealment``.
The gate keeps the POLICY — which sources are covered, and what ``full``
requires; this file keeps the BOOKKEEPING one handler does while it renders.
Every name is re-exported from ``concealment_gate`` and again from
``concealment``, so no handler imports this module directly.

Two properties here are the whole reason the file exists, and both were bought
by a round-6 review that reproduced a false ``full`` (2026-09-03):

**OCCURRENCE-AWARE (round 7, 2026-09-04).** Until now :meth:`Admitted.uninspected`
tested CONTAINMENT — an admitted chunk passed if its letters appeared ANYWHERE
in the walker's report. So one inspected occurrence of a text satisfied every
identical occurrence, and a DOCX carrying its payload once as a visible
paragraph and once as a ``w:vanish`` run inside a ``w:hyperlink`` read ``full``
with zero concealed runs. The accounting is now a CURSOR over the walker's
report: chunks are matched in the order the renderer admitted them, and each
match CONSUMES the text it matched, so a second occurrence needs a second
sighting. Both halves of every handler already emit in document order — that
is what ``_docx_paragraphs`` mirroring ``_document_parts`` is for — so the
cursor costs an ordinary document nothing.

Containment could not simply become equality, because a renderer legitimately
repeats text it was given: ``html.py`` writes the ``<title>`` again as a
heading. So a renderer DECLARES its repeat with :meth:`Admitted.repeat`, and a
declared repeat is checked for containment (the text must have been seen at
least once) without consuming a second sighting. A handler that declares a
repeat for text that is not one is lying to its own ledger, which is the same
layer :meth:`Admitted.chrome` already sits at and cannot be checked from here.

**PROVENANCE (round 7, 2026-09-04).** :meth:`Admitted.watch` used to return a
bare ``list.append``, so nothing recorded which HALF of a handler fed it. A
handler wired from its RENDERER instead of its walker satisfied its own
coverage check with the renderer's own output and read ``full`` with the
payload in the note — failing open, with a green suite. The sink now records
the MODULE each report came from, and ``concealment_gate`` holds that against
the walker module the source's :data:`~concealment_gate.COVERED_SOURCES` row
declares. A renderer lives in a handler module and a walker lives in a
``concealment*`` module, so the miswiring names itself.
"""
from __future__ import annotations

import sys
from typing import Callable

#: The one source name that is not document text at all: headings, labels and
#: separators the HANDLER itself wrote (``## Slide 3``, ``- **From:**``). It is
#: in the ledger so the body can be accounted for exactly, and it is covered
#: because no attacker authored it.
CHROME = "handler:chrome"


def _flat(text: str) -> str:
    """Whitespace-normalised, so a join separator cannot change the answer."""
    return " ".join((text or "").split())


def _key(text: str) -> str:
    """ASCII letters and digits, lowercased — the COVERAGE comparison unit.

    Whitespace, punctuation and the handler's own Markdown scaffolding
    (``##``, ``|``, ``---``) reduce to nothing, so a renderer's layout choices
    cannot make a walker look blind. So does a dash or a ligature the two
    sides map differently — measured on the reference corpus, a PDF text layer
    rendering ``–`` where the same operand decodes to ``Ð``. What survives is
    the letters an instruction is made of.
    """
    return "".join(c for c in text.lower() if c.isascii() and c.isalnum())


def _reporter() -> str:
    """The MODULE two frames up — i.e. the code that called a coverage sink.

    Read at report time rather than at :meth:`Admitted.watch` time on purpose:
    ``watch`` is always called by the handler, in the correctly wired case and
    the miswired one alike, so its own caller distinguishes nothing. The
    caller of the SINK is the walker, or the renderer that was handed the sink
    by mistake, and those two live in different modules.
    """
    try:
        return str(sys._getframe(2).f_globals.get("__name__") or "?")
    except (ValueError, AttributeError):  # coverage-audit: an unnameable frame
        # reads as an unknown reporter, which fails the provenance check
        # CLOSED — it never silently counts as the declared walker.
        return "?"


class Admitted:
    """The ledger: every chunk of text a handler puts in the note, by SOURCE.

    A handler threads one of these through its body assembly —
    ``lines.append(admitted.add("pptx:native", text))`` — and hands it to
    ``concealment_gate.attest`` with the finished body. :meth:`add` returns the
    text it was given, so wrapping a call site is one edit and never changes
    the output.
    """

    __slots__ = ("chunks", "seen", "sinks")

    def __init__(self) -> None:
        #: ``(source, text, is_declared_repeat)`` in RENDERED order. The order
        #: is load-bearing twice over: :meth:`residue` requires the chunks to
        #: tile the body exactly, and :meth:`uninspected` walks the walker's
        #: report with a cursor that only ever moves forward.
        self.chunks: list[tuple[str, str, bool]] = []
        #: What each WALKER reported reading, by source. Written only through
        #: :meth:`watch`, and empty for a source whose walker was never wired
        #: to one — which is a shortfall, not a pass.
        self.seen: dict[str, list[str]] = {}
        #: Which MODULES fed each source's sink. See :func:`_reporter`.
        self.sinks: dict[str, set[str]] = {}

    def add(self, source: str, text: str) -> str:
        if text and text.strip():
            self.chunks.append((source, text, False))
        return text

    def repeat(self, source: str, text: str) -> str:
        """Text this renderer is DELIBERATELY writing a second time.

        ``html.py`` renders the ``<title>`` as an ``# H1`` and the same text
        arrives again inside ``text_content()``'s body. The note carries it
        twice, so :meth:`residue` must see it twice; the walker read it once,
        so :meth:`uninspected` must not demand a second sighting. Declaring it
        is what keeps those two facts from contradicting each other — and what
        stops the occurrence-aware rule from having to soften back into the
        containment test a round-6 reviewer defeated.

        The declared text still has to have been seen at least ONCE. A repeat
        of something no walker ever reported is a shortfall like any other.
        """
        if text and text.strip():
            self.chunks.append((source, text, True))
        return text

    def chrome(self, text: str) -> str:
        """Text the HANDLER wrote — a heading, a label, a separator."""
        return self.add(CHROME, text)

    def sources(self) -> set[str]:
        return {source for source, _, _ in self.chunks}

    def watch(self, source: str) -> Callable[[str], None]:
        """The sink a WALKER reports into: every text it actually inspected.

        Handed to the walk, never to the renderer — ``docx_runs(document,
        admitted.watch("docx:body"))``. The two halves of a handler then read
        the document through their own APIs and the ledger compares the
        results, instead of a table asserting that they agree.

        A handler that forgets to wire one reports NOTHING for that source,
        which is a total shortfall and reads ``uninspected``. A handler that
        wires the WRONG half reports from its own module, and
        :meth:`reported_by` is what makes that visible rather than a green
        suite over a note that fails open.
        """
        texts = self.seen.setdefault(source, [])
        modules = self.sinks.setdefault(source, set())

        def sink(text: str) -> None:
            modules.add(_reporter())
            texts.append(text)

        return sink

    def reported_by(self, source: str) -> set[str]:
        """The modules that fed ``source``'s sink. Empty when nothing did."""
        return set(self.sinks.get(source, ()))

    def uninspected(self, source: str) -> list[str]:
        """Chunks admitted under ``source`` that its walker never reported.

        OCCURRENCE-AWARE, in rendered order. ``at`` is a cursor into the
        walker's concatenated report; each admitted chunk must appear at or
        after it, and a match moves the cursor past what it matched. So a
        second identical chunk needs a second sighting, which is the round-6
        HIGH: a payload carried once visibly and once inside a ``w:hyperlink``
        used to be satisfied by the visible copy alone.

        A chunk the walker did not report leaves the cursor where it was, so
        one shortfall never cascades into a false shortfall for every chunk
        behind it. A DECLARED repeat (:meth:`repeat`) is held to containment
        only — see that method for why equality is not available here.
        """
        seen = _key("".join(self.seen.get(source, ())))
        missing: list[str] = []
        at = 0
        for name, text, again in self.chunks:
            if name != source:
                continue
            key = _key(text)
            if not key:
                continue
            if again:
                if key not in seen:
                    missing.append(text)
                continue
            found = seen.find(key, at)
            if found < 0:
                missing.append(text)
            else:
                at = found + len(key)
        return missing

    def residue(self, body: str) -> int:
        """How many characters of ``body`` the ledger cannot account for.

        Zero when the body is exactly the admitted chunks joined by
        whitespace — which is what every handler here does. Non-zero means
        text entered the note without a source, and no claim about what was
        searched can be made about it.
        """
        expected = " ".join(flat for flat in (_flat(t) for _, t, _ in self.chunks)
                            if flat)
        actual = _flat(body)
        return 0 if expected == actual else abs(len(actual) - len(expected)) or 1
