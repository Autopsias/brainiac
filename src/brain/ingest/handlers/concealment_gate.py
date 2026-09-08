"""The concealment walk's RUNTIME GATE: kill switch, tolerance, COVERAGE.

Split out of :mod:`concealment` for the 500-LOC file bound, exactly as
``concealment_html``/``concealment_office`` were. Everything here answers one
question — *was every text this note admitted actually searched?* — while
``concealment`` keeps the detection vocabulary. Every name is re-exported from
``concealment``, so no handler imports this module directly.

THE COVERAGE RULE (round 5, 2026-09-03) — read this before adding a text
source anywhere in ``ingest/handlers``.

``injection_assessment.concealment_scan: full`` means *every text admitted
into this note was searched for concealment*. Four rounds of review each
closed the lanes it was asked about and left the next instance of the same
defect, because ``full`` was what a lane got by DEFAULT along any path its
author had not thought about:

* round 1 asked for a kill switch;
* round 2 shipped one that missed a lane;
* round 3 added the key, and it over-claimed on five lanes that never walk;
* round 4 made ``full`` a handler ATTESTATION — and two lanes that DO attest
  turned out to admit OCR text no walker reads (V13), so they attested over
  it.

* round 5 made ``full`` an EARNED ledger — and the table that said which
  walker covered which source was PROSE. ``docx:body``'s entry claimed
  ``concealment_office.docx_runs`` walks every run ``docx.py`` renders; the
  renderer reads ``paragraph.text`` and the walker iterates
  ``paragraph.runs``, which are not the same set. A ``w:vanish`` run inside a
  ``w:hyperlink`` is in the first and not the second, so Word's own
  hidden-text attribute carried an injection into the corpus stamped
  ``full`` with ``concealed runs: 0`` (V15). PPTX ``a:fld`` field text
  reproduced the identical shape.

So ``full`` is no longer something a handler can SAY. It is something a
handler has to EARN, three times over:

1. every chunk of text it admits is tagged with a SOURCE, and every source
   must appear in :data:`COVERED_SOURCES` — a source that is not in that
   table has no walker, and the note reads ``unknown``;
2. the ledger must ACCOUNT for the finished body: the note's text, whitespace
   normalised, must be exactly the chunks it was told about. Text appended
   without going through :class:`Admitted` leaves a residue, and the note
   reads ``unknown``;
3. the WALKER must report the text it actually read, that report must COVER
   every chunk admitted under its source OCCURRENCE BY OCCURRENCE, and the
   report must have come from the walker MODULE this table declares. This is
   the round-5 residue check applied one level in, and it is what turns the
   coverage table from a claim into a measurement: a renderer and a walker
   that read the document through different APIs disagree about SOME text,
   and the disagreement is the shortfall. Neither the DOCX hyperlink nor the
   PPTX field is named anywhere in this file — they fall out, and so does the
   next divergence nobody has found yet.

* round 6 measured that table and shipped with the GRANT withheld, because
  the comparison was CONTAINMENT: one inspected occurrence of a text
  satisfied every identical occurrence, so a document carrying its payload
  once visibly and once inside a ``w:hyperlink`` read ``full`` with zero
  concealed runs. Round 7 (2026-09-04) made the accounting occurrence-aware
  (``concealment_ledger.Admitted.uninspected`` walks the report with a
  cursor, and a renderer DECLARES a deliberate repeat), fixed the two DOCX/
  PPTX divergences generically, and put the grant back. The switch that
  withheld it is gone.

Both defaults point the same way. Add an OCR call, a new MIME part, a header
block, a sidecar file — anything that puts text in the note — and one of two
things happens with no further action from you: the text never reached the
ledger (residue, ``unknown``), or it reached it under a name nobody has
covered (``unknown``). Neither requires the next author to have read this
docstring. That is the point of it.

CEILING, stated so it is not mistaken for more than it is. Three parts:

* the comparison unit is the ASCII letter and digit
  (``concealment_ledger._key``): whitespace, punctuation and the handler's own
  Markdown scaffolding reduce to nothing, so a renderer's layout choices
  cannot make a walker look blind, and a renderer/walker pair that disagrees
  only about a dash or a ligature does not read as a missing payload. A
  payload written entirely in characters outside that set is therefore
  invisible to the COVERAGE check — the same ceiling gap 6 already records for
  the detection rules, which read English and Portuguese;
* the walker's report is compared as ONE CONCATENATED BLOB per source, not
  chunk against chunk (round-6 review, LOW — this paragraph omitted it). The
  walker reports per run, per text node, per operator; the renderer admits per
  paragraph, per shape, per page. Concatenating is what lets those two
  granularities be compared at all. What it costs is BOUNDARY precision: a
  chunk is covered if its letters run consecutively somewhere at or after the
  cursor, even if the walker read them as the tail of one run and the head of
  the next. The cursor bounds this to document order and to one sighting per
  occurrence; it does not make the comparison chunk-exact;
* a DECLARED repeat (``Admitted.repeat``) is held to containment, not to the
  cursor, and a handler that declares one falsely is not checkable from here —
  the same layer ``Admitted.chrome`` already sits at.
"""
from __future__ import annotations

import os
import re
import threading
from typing import Any, Callable

# Re-exported, not re-implemented: the LEDGER is bookkeeping and this file is
# policy, and they are two files only because of the 500-LOC bound.
from .concealment_ledger import CHROME, Admitted, _key  # noqa: F401


#: The kill switch. ``BRAIN_CONCEALMENT_SCAN=off`` (also ``0``/``false``/``no``)
#: turns every handler-boundary walk off, so an over-fire in production is
#: stopped by an environment variable rather than by a code revert and a
#: release. It disables DETECTION only: the existing ``injection_scan`` rules
#: on the extracted Markdown are untouched.
DISABLE_ENV = "BRAIN_CONCEALMENT_SCAN"


def enabled() -> bool:
    return os.environ.get(DISABLE_ENV, "on").strip().lower() not in (
        "0", "off", "false", "no")


#: The metadata key a WALKING lane stamps onto ``ExtractResult.metadata``.
#: Its PRESENCE is the attestation; its value is one of the scan states.
STATE_KEY = "concealment_scan"

#: Per-thread count of failures swallowed by an INNER tolerance layer since
#: the last :func:`take_degraded`. Thread-local because two extractions may
#: share a process and a global would attribute one document's damage to
#: another's scan state.
_LOCAL = threading.local()


def note_degraded() -> None:
    """An inner tolerance layer just swallowed one failure (V12, 2026-09-02).

    Until this existed, only the OUTER :func:`collect` catch warned, so a
    document whose every run/element/operator raised inside was stamped
    ``concealment_scan: full`` — an assurance stronger than the walk that
    produced it. Counting here keeps the fail-open contract intact (a
    detector still never fails an extraction) while making the damage
    reach the document's scan state as ``incomplete``.
    """
    _LOCAL.degraded = getattr(_LOCAL, "degraded", 0) + 1


def take_degraded() -> int:
    """How many inner failures since the last call. Resets the counter."""
    count = getattr(_LOCAL, "degraded", 0)
    _LOCAL.degraded = 0
    return count


#: The three ways a source can be covered. The KIND is what :func:`attest`
#: enforces; the reason beside it is documentation and is never the check.
#: There is no fourth kind and no "trust me" — that was V15.
#: WALKED — a walker reads this source and reports what it read
#: (:meth:`Admitted.watch`). ``full`` requires that report to cover every
#: chunk admitted under the name, occurrence by occurrence, AND to have come
#: from one of the walker modules the row names.
WALKED = "walked"

#: PLAIN — text with no presentational channel to hide in. NOT taken on the
#: author's word: :data:`_MARKUP` checks each chunk, so a header value that
#: really does carry markup makes the claim fail for that document instead of
#: standing as a false universal (round-5 review, MEDIUM).
PLAIN = "plain"

#: AUTHORED — written by the handler itself: a heading, a label, a table pipe.
#: The one kind with nothing to measure, because no document text passes
#: through it: :meth:`Admitted.chrome` is the only door, and it is a literal in
#: handler source. Routing document text through it would be a handler lying to
#: its own ledger, which is a layer below anything here can see.
AUTHORED = "authored"

#: The package every walker module lives under. A WALKED row names its
#: walkers by MODULE, and this prefix is what makes the names short.
_PKG = "brain.ingest.handlers."

#: EVERY text source that may contribute to a note body, WHICH MODULES may
#: report for it, and why. A source that is not a key here is UNCOVERED — the
#: note reads ``uncovered``. That is the default, and it is what makes a text
#: source nobody thought to cover tell the truth about itself.
#:
#: The middle field is the round-7 answer to a round-6 MEDIUM: the reason
#: string named a walker in PROSE, and nothing checked that the walker was
#: what actually fed the sink. A handler wired from its RENDERER instead
#: satisfied its own coverage with its own output. The modules named here are
#: held against ``Admitted.reported_by`` — a renderer lives in a handler
#: module, a walker in a ``concealment*`` module, so the miswiring names
#: itself. It is empty for the two kinds that have no walker.
COVERED_SOURCES: dict[str, tuple[str, tuple[str, ...], str]] = {
    CHROME: (AUTHORED, (),
             "authored by the handler, never read from the document"),
    "html:text": (WALKED, (_PKG + "concealment_html",),
                  "concealment_html walks the same tree html.py flattens with "
                  "text_content(), and reports every text node it read"),
    "html:bundled": (WALKED, (_PKG + "concealment_html",),
                     "the same walker over the same reader, applied to the "
                     "INNER document of a bundled artifact page instead of "
                     "its loader (html.py's `_bundled_document`) — the loader "
                     "carries no text worth scanning and the inner document "
                     "is what the note holds"),
    "docx:body": (WALKED, (_PKG + "concealment_office",),
                  "concealment_office.docx_runs walks every w:r in the "
                  "renderer's body order — not paragraph.runs, which stops at "
                  "a paragraph's DIRECT children and so misses the runs Word "
                  "nests one level down (V15). The report is still a "
                  "measurement, not a claim of equality"),
    "pptx:native": (WALKED, (_PKG + "concealment_office",),
                    "concealment_office.pptx_runs walks every text-bearing "
                    "element of every shape text frame and table cell pptx.py "
                    "extracts — every element that owns an a:t, whatever holds "
                    "it — and reports what it read"),
    "pdf:text_layer": (WALKED, (_PKG + "concealment_pdf",),
                       "concealment.pdf_operand_visitor watches the text-render "
                       "mode of every show operator and reports the operand it "
                       "decoded, through the current font's ToUnicode CMap "
                       "where the font supplies one. A font that supplies none "
                       "and encodes glyph CODES (gap 5) gives a report that "
                       "does not cover the rendered page, which is exactly "
                       "what it means to be unable to search that page"),
    "email:body_html": (WALKED, (_PKG + "concealment_html",),
                        "concealment_html.html_runs_stdlib walks the same html "
                        "bytes email.py strips to text — mail is the primary "
                        "ingest lane and this branch was a bypass until V1"),
    "email:headers": (PLAIN, (),
                      "RFC 5322 header values are delivered as text, with no "
                      "colour, size, position or render state around them — "
                      "unless the value itself carries markup, which is "
                      "checked rather than assumed"),
    "email:attachment_manifest": (PLAIN, (),
                                  "attachment filenames and MIME types, same, "
                                  "and checked the same way"),
}

#: A presentational channel inside text that claimed to have none: an HTML
#: comment, a well-formed HTML tag, an inline style attribute, a Markdown link
#: title/target, or a Markdown reference definition. Deliberately narrow about
#: what counts as a tag — ``Alice <alice@example.com>`` is an ordinary ``From:``
#: header and must not read as markup, so a name must be followed by whitespace
#: or ``>``, never by ``@``.
#:
#: The comment and Markdown forms were added on 2026-09-03 after a round-6
#: review reproduced the gap: an HTML comment in a ``Subject:`` reached the
#: note's Markdown, invisible in every renderer a human reads and fully visible
#: to a model, while the note still attested ``concealment_scan: full``. A
#: PLAIN source has no walker, so this predicate IS its whole check, and a
#: predicate narrower than the claim it stands for is the defect this round
#: exists to end.
#:
#: CEILING, stated because this is a predicate and not a walker: it enumerates
#: the constructs known to hide text in a rendered note. A construct nobody has
#: written down here is not caught. If a PLAIN source ever needs to carry
#: arbitrary rich text, demote it out of :data:`COVERED_SOURCES` and let it read
#: ``unknown`` rather than widen this further.
_MARKUP = re.compile(r"<!--|-->"
                     r"|<\s*/?[a-z][a-z0-9]*(?:\s[^<>]*)?>|style\s*=\s*[\"']"
                     r"|\]\([^)]*\)|^\s*\[[^\]]+\]:\s",
                     re.I | re.M)

#: Sources KNOWN to be uncovered. Listing one changes NOTHING mechanically —
#: absence from :data:`COVERED_SOURCES` is what decides — but it is how an
#: operator reading a warning learns whether the gap is understood or new.
UNCOVERED_SOURCES: dict[str, str] = {
    "pptx:picture_ocr": "OCR of a slide picture. The concealment walkers read "
                        "python-pptx shape runs; nothing reads a raster, and a "
                        "picture-only deck is the ordinary shape of an exported "
                        "or scanned deck (V13, 2026-09-03)",
    "pdf:page_ocr": "OCR of a page raster on a page with no text layer. The "
                    "operand visitor sees content-stream operators; a scan has "
                    "none (V13, 2026-09-03)",
    "email:body_plain": "a text/plain part. It is the rendered alternative to a "
                        "text/html sibling this lane does not walk, and when "
                        "that sibling exists the styled original is unexamined "
                        "(V9, 2026-09-02)",
}


#: The three ways coverage can FAIL to be established, and they are three
#: separate scan states rather than one ``unknown`` because they mean three
#: different things (round-6 review, MEDIUM — ``unknown`` was one unalarmed
#: bucket with three causes, no per-note reason, and nothing to list):
#:
#: * ``uncovered`` — a source with no walker at all (OCR text, a text/plain
#:   part). A POPULATION: known, enumerated in :data:`UNCOVERED_SOURCES`, and
#:   not a fault;
#: * ``uninspected`` — a COVERED source whose walker did not reach its text,
#:   or was not the walker this table declares. A FAULT;
#: * ``unaccounted`` — text reached the note body without passing the ledger.
#:   A FAULT.
#:
#: The last two are in ``injection_fold.REGRESSION_BUCKETS``: they can only
#: mean a lane that IS supposed to be covered stopped being. The state is per
#: note and in its frontmatter, so listing them is one grep over the vault.
UNCOVERED, UNINSPECTED, UNACCOUNTED = "uncovered", "uninspected", "unaccounted"


def attest(warnings: list[str] | None = None, *,
           body: str | None = None,
           admitted: "Admitted | None" = None) -> dict[str, str]:
    """The scan state this note has EARNED, merged into its metadata.

    Returns ``{}`` — an ABSENT key, which ``concealment_scan_state`` reads as
    ``unknown`` — only for a lane that passes no ledger at all (``xlsx``,
    ``text``, ``image``, ``zip``, ``tables``). A lane that DOES pass one always
    gets a word back, and when coverage fails the word says which of the three
    ways it failed (:data:`UNCOVERED` / :data:`UNINSPECTED` /
    :data:`UNACCOUNTED`), with the detail on a warning beside it.

    ``full`` requires all of: the switch on, every admitted source in
    :data:`COVERED_SOURCES`, every WALKED source's DECLARED walker reporting
    text that covers what it admitted occurrence by occurrence, and a residue
    of zero. A ``concealment_scan_warning:`` from a degraded walk downgrades it
    to ``incomplete``.
    """
    notes = warnings if warnings is not None else []
    if admitted is None:
        return {}
    if not enabled():
        return {STATE_KEY: "off"}
    fault = _fault(admitted, notes)
    if fault:
        return {STATE_KEY: fault}
    left = admitted.residue(body or "")
    if left:
        notes.append(
            f"concealment_scan_unaccounted: {left} character(s) of the note body "
            "did not come through the coverage ledger, so no claim about what "
            "was searched can cover them")
        return {STATE_KEY: UNACCOUNTED}
    warned = any(str(w).startswith("concealment_scan_warning:") for w in notes)
    return {STATE_KEY: "incomplete" if warned else "full"}


def _fault(admitted: "Admitted", notes: list[str]) -> str | None:
    """Why coverage is not established for this ledger, or ``None``.

    One pass, every source reported rather than the first: an operator fixing
    a note wants the whole shortfall, not one name at a time.

    :data:`UNINSPECTED` OUTRANKS :data:`UNCOVERED` when a document hits both.
    An uncovered source is a known population and an uninspected one is a
    regression, and a regression that hides inside a population is exactly the
    defect this split exists to end.
    """
    fault = None
    for source in sorted(admitted.sources()):
        entry = COVERED_SOURCES.get(source)
        if entry is None:
            notes.append(
                f"concealment_scan_uncovered: {source} "
                f"({UNCOVERED_SOURCES.get(source, 'no walker is declared for this source')})")
            fault = fault or UNCOVERED
            continue
        short = _shortfall(admitted, source, entry[0], entry[1])
        if short:
            notes.append(f"concealment_scan_uninspected: {source} — {short}")
            fault = UNINSPECTED
    return fault


def _shortfall(admitted: "Admitted", source: str, kind: str,
               walkers: tuple[str, ...] = ()) -> str | None:
    """Why this source's cover does not reach its admitted text, or ``None``.

    The one function that turns each :data:`COVERED_SOURCES` kind into a
    measurement. ``AUTHORED`` is the only kind with nothing to measure, and it
    is the only kind no document text passes through.

    For a WALKED source there are TWO measurements, and the provenance one
    runs first because it decides whether the other one means anything: a sink
    fed by the handler's own renderer is trivially covered by its own output
    (round-6 review, MEDIUM). ``walkers`` is the module list the source's
    :data:`COVERED_SOURCES` row declares; ``Admitted.reported_by`` is who
    actually called the sink.
    """
    if kind == AUTHORED:
        return None
    if kind == WALKED and walkers:
        wrong = sorted(admitted.reported_by(source) - set(walkers))
        if wrong:
            return ("the coverage sink was fed by " + ", ".join(wrong)
                    + ", not by the declared walker module(s) "
                    + ", ".join(walkers) + " — a sink fed by the half that "
                    "RENDERS the text is covered by its own output and "
                    "measures nothing")
    if kind == PLAIN:
        marked = sum(1 for name, text, _ in admitted.chunks
                     if name == source and _MARKUP.search(text))
        return (f"{marked} chunk(s) carry markup, so the 'no presentational "
                "channel to hide in' that covers this source is not true of "
                "this document") if marked else None
    missing = admitted.uninspected(source)
    if not missing:
        return None
    return (f"{len(missing)} chunk(s), {sum(len(_key(t)) for t in missing)} "
            "comparable character(s), of admitted text are not in what the "
            "walker reported reading, so nothing searched them")


def collect(gather: Callable[[], list[dict[str, Any]]],
            warnings: list[str]) -> list[dict[str, Any]]:
    """Run one collector; a DETECTOR MUST NEVER FAIL AN EXTRACTION.

    Every handler wraps its concealment walk in this. A failure is reported
    as a warning on the record — visible, never silent — and the document is
    extracted exactly as it would have been before this module existed. The
    alternative is a detector that turns a readable deck into a quarantine,
    which is how a security control becomes the outage.

    **The decision the round-1 review asked for, stated (2026-09-02).** A
    collector that cannot complete does NOT quarantine. Two reasons, and the
    first is the load-bearing one: this catch is the LAST resort, not the
    first — tolerance now lives per element (``concealment_html``), per run
    (``docx_runs``/``pptx_runs``), per operator (``pdf_operand_visitor``) and
    per page (``pdf._page_text``), so reaching here means the walk failed
    whole, not that one declaration was malformed. Second, the trigger is
    unproven: eight malformed declarations were tried against the shipped
    rules and every one still convicted with no warning. Quarantining on an
    unproven trigger trades a hypothetical bypass for a certain outage, and
    the two-stage rule (hidden AND injection-shaped) is what carries the
    safety margin. The failure is not silent: the warning rides on the record
    and ``brain integrity --injection`` reports the note.
    """
    if not enabled():
        return []
    take_degraded()  # discard any count the previous document left behind
    try:
        return gather()
    except Exception as exc:  # coverage-audit: reports directly as a warning
        warnings.append(f"concealment_scan_warning: {type(exc).__name__}: {exc}")
        return []
    finally:
        # The INNER tolerance layers this docstring points at are silent by
        # design — they skip one element and carry on. Silent to the walk is
        # not silent to the RECORD: their count warns here, which is what
        # turns the document's scan state `incomplete` (V12, 2026-09-02).
        skipped = take_degraded()
        if skipped:
            warnings.append(
                f"concealment_scan_warning: {skipped} element(s) skipped "
                "after an internal error; some hiding may be unseen")


def run_record(marker: str, container: str, where: str, text: str) -> dict[str, Any]:
    """One hidden run, as the handlers emit it into ``metadata["concealed"]``.

    ``text`` is the run's FULL text, never truncated: the scanner classifies
    the joined text and truncates only the excerpt it keeps for display. A
    400-character truncation here would let 400 benign characters ahead of a
    payload defeat conviction outright.
    """
    return {"marker": marker, "container": container, "where": where, "text": text}
