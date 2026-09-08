"""Concealment detection AT THE HANDLER BOUNDARY (M-3, 2026-09-02).

``brain.injection_scan`` classifies the Markdown a handler has already
produced. By that point the styling is gone: ``handlers/html.py`` calls
``doc.text_content()``, ``handlers/pptx.py`` reads ``shape.text_frame.text``,
``handlers/docx.py`` reads ``paragraph.text``, ``handlers/pdf.py`` reads
``page.extract_text()``. White-on-white text, one-pixel text, text parked
10,000 px off the page and a Word run carrying ``w:vanish`` all arrive as
ordinary sentences. Measured 2026-09-01: every one of those fixtures ingested
clean.

So the detection lives HERE, in the handlers, where colour, size and position
still exist, and travels forward as a structured signal in
``ExtractResult.metadata["concealed"]`` — a list of RUN records. The scanner
(``injection_scan.fold_concealed``) does the deciding; a handler only ever
reports *"this text was hidden"*.

**A handler never quarantines on technique alone.** Measured over 538 real
archived originals, re-measured 2026-09-02 after the round-2 detectors landed:
technique fires on 26.6% of documents (up from 10.4%, because the render-state
rules below now see hiding that was invisible — a print-only ``display:none``
block, a theme-white run on a white master), and the two-stage rule (hidden AND
injection-shaped once revealed) still convicts 0.00%. That gap is the whole
safety margin, it WIDENED rather than narrowed, and it lives in
``injection_scan``, not here.

THE COVERAGE RULE, and why this gap list is not decoration (round 5,
2026-09-03). ``injection_assessment.concealment_scan: full`` means *every text
admitted into the note was searched*, and it is now unreachable unless that is
positively established: each handler tags every chunk of text it admits with a
SOURCE, ``concealment_gate.COVERED_SOURCES`` says which sources a walker
covers, and the ledger must account for the finished body exactly. A source
with no walker, or text that never reached the ledger, gives ``unknown``
without anyone having to notice. The mechanism and its ceiling are in
``concealment_gate``'s module docstring; the list below is what the covered
walkers still cannot see WITHIN the text they do search.

ADMITTED TEXT SOURCES WITH NO WALKER — text that really does reach the note
body and that nothing here examines. Each is declared in
``concealment_gate.UNCOVERED_SOURCES``, and its presence alone stops the note
reading ``full``:

* **``pptx:picture_ocr``** — OCR of a slide picture (``pptx._ocr_pictures``).
  The walkers read python-pptx shape runs; nothing reads a raster. A
  picture-only deck is the ordinary shape of an exported or scanned deck, and
  until 2026-09-03 such a deck carried its whole OCR'd payload under a stamped
  ``full`` (review finding V13).
* **``pdf:page_ocr``** — OCR of a page raster on a page with no text layer
  (``pdf._ocr_page``). The operand visitor watches content-stream operators; a
  scan has none. Same finding, same date.
* **``email:body_plain``** — a ``text/plain`` part. It is the rendered
  alternative to a ``text/html`` sibling this lane does not walk.
* **``xlsx``, ``text``, ``image``, ``zip``, ``tables``** — whole lanes that
  pass no ledger at all, so they stamp nothing and read ``unknown`` (V9,
  2026-09-02). Building detectors for them is NOT this session's work.

WHAT THIS DOES NOT DETECT — the list ships HERE, in the module, because the
design note it used to cite lives under ``_plans/``, which ``.gitignore``
excludes, so a reader of the merged code had a dangling reference to the one
document naming the gaps (review finding V3, 2026-09-02):

1. **Encoded payloads.** A base64 or hex blob in plain sight defeats every
   pattern. Deliberately not attempted: the false-positive surface is every
   certificate and signature block in the corpus.
2. **Separator-split and homoglyph verbs.** ``I.g.n.o.r.e`` is not rejoined.
   (Splitting across hidden RUNS *is* covered — the fold joins them.)
3. **Compound CSS selectors.** ``<style>`` rules are resolved only for a bare
   tag, ``.class`` or ``#id``; ``.a .b{display:none}`` is missed.
4. **PDF text inside a form XObject.** pypdf's operand visitor does not
   descend into one.
5. **PDF fonts with no ToUnicode CMap.** A ``/Encoding /Differences`` or CID
   font makes a show operator's operand glyph CODES. Since round 7 the
   visitor decodes it through the page's own font resources — the same route
   ``extract_text()`` takes — so this is no longer the wide gap round 6
   measured (131 of 144 reference-corpus PDFs had a page whose report did not
   cover what the reader saw; three of those measured 2, 8 and 10 short pages
   before the decode and 0 after). What is LEFT is a font offering no
   ToUnicode map at all, or one that does not cover the code: those runs stay
   ``hidden_undecoded_text``, and the page they sit on still reads short
   rather than claiming a scan it did not perform. The three ceilings on the
   decode are in ``concealment_pdf``'s module docstring; each fails CLOSED.
   Detection of ``hidden_render_mode`` is unaffected either way: the render
   state is visible whether or not the operand decodes.
6. **Any concealed language but English and Portuguese.**
7. **Style-chain depth in Word.** A run's own font, its character style and
   its paragraph style are read; a ``basedOn`` ancestor style is not.
8. **PPTX theme resolution.** ``BACKGROUND_1``/``LIGHT_1`` are treated as
   white by NAME (they are white in every stock theme); a custom theme that
   redefines them, or a slide whose own background is dark, is not resolved.

REPRODUCED BYPASSES, OPEN — the round-2 adversarial gate (2026-09-02) ran each
of these as a real document through the real handler and watched the payload
reach the note body while the verdict stayed ``instruction_only``. They share
ONE root cause: ``concealment_html`` approximates the CSS cascade rather than
implementing it, so ordinary standards-valid CSS defeats it. Closing that is
session **s05d / item med-11**, deliberately NOT a fourth ad-hoc special case
here (owner ruling, 2026-09-02):

9. **``!important``.** REPRODUCED. ``style="display:none!important"`` yields
   zero concealed runs — the literal value match never sees past the token.
   → s05d.
10. **Class source order.** REPRODUCED. ``class="hide show"`` with
    ``.show{display:block}.hide{display:none}`` is admitted: declarations are
    concatenated in ATTRIBUTE order, so the last class named wins instead of
    the last RULE declared, and no specificity is computed. → s05d.
11. **Presentational colour attributes.** REPRODUCED. ``<font color="#ffffff">``
    is admitted; ``bgcolor`` is likewise never read as a ground. Neither
    attribute is parsed at all today, and ``<font color>`` is the commonest way
    HTML email hides text — mail being the primary ingest lane. → s05d.
12. **A malformed declaration BESIDE a hiding property.** REPRODUCED.
    ``style="color:rgb(1.2.3,0,0);display:none"`` is admitted, while the same
    malformed colour ALONE on its own element still convicts: the per-element
    tolerance added in rework 2 discards that element's whole resolved style,
    hiding property included. This is the attacker-reachable trigger for the
    fail-open :func:`collect` documents below. → s05d.

13. **FIVE INGEST LANES ARE UNSCANNED, and they always were.** (Also listed
    above as uncovered SOURCES — the two lists meet here.) Detection
    lives in exactly five handlers — ``html``, ``email`` (html-only bodies),
    ``docx``, ``pptx``, ``pdf``. ``xlsx``, ``text``, ``image``, ``zip`` and
    ``tables`` never call a detector, and neither does an ``email`` with a
    ``text/plain`` part. REPRODUCED for xlsx (review finding V9,
    2026-09-02): a sheet whose A2 cell carries ``Font(color="FFFFFFFF")``
    and an exfiltration instruction extracts with the payload present, no
    warnings, and — until that finding — a stamped ``concealment_scan:
    full``. Those lanes now attest NOTHING, so their notes read
    ``concealment_scan: unknown``; ``full`` is a handler attestation
    (:func:`attest`) and can no longer be inferred from silence. Building
    the xlsx / text / image / zip / tables detectors is NOT done here.
    → s05d.

Also true, and NOT what the ``concealment_html`` docstring said until
2026-09-02: an ``@media`` block does not suppress the rules inside it. The
at-rule wrapper is stepped over and its inner rules are resolved as if
unconditional, so ``@media print{.p{display:none}}`` convicts. That direction
is an OVER-fire, not a bypass, and it is measured in the 0.00% conviction rate
below rather than assumed harmless.

There is a kill switch: ``BRAIN_CONCEALMENT_SCAN=off`` disables every walk in
all five lanes — HTML, email, DOCX, PPTX and PDF — so a production over-fire
does not need a code revert to stop. Four of the five reach it through
:func:`collect`; the PDF lane checks it in :func:`pdf_operand_visitor`, which
is the only line pypdf's callback shares with the switch. A note ingested
while it is off records ``injection_assessment.concealment_scan: off``, so an
unscanned note is never mistaken for a scanned-clean one
(``brain.injection_fold.concealment_scan_state``).

**Every tolerance layer now reports, including the inner ones** (review
finding V12, 2026-09-02, extended 2026-09-03). ``collect``'s outer catch used
to be the only one that warned, so a document whose every element, run or
operator raised inside
``concealment_html``/``concealment_office``/:func:`pdf_operand_visitor` was
stamped ``full`` — ``incomplete`` was far narrower than it read. Those catches
call :func:`note_degraded`, and the count becomes a
``concealment_scan_warning:`` on the record. The fail-open contract is
unchanged: a detector still never fails an extraction.

V12 fixed the catches it was shown; a SIXTH sat in ``_shape_offscreen`` and
reported nothing, because nobody had ENUMERATED them (review finding V14,
2026-09-03). They are enumerated now, by a test rather than by hand:
``test_concealment_round5`` walks the AST of all four concealment modules and
requires every ``except`` either to call :func:`note_degraded` or to carry a
``# coverage-audit:`` comment saying why it is not a degradation. A seventh
cannot arrive silently.
"""
from __future__ import annotations

import re

# Re-exported, not re-implemented: handlers and `injection_fold` import these
# from HERE, and the split is a file-size measure, not a second module in the
# contract (see `concealment_gate`).
from .concealment_gate import (AUTHORED, CHROME, COVERED_SOURCES,  # noqa: F401
                               DISABLE_ENV, PLAIN, STATE_KEY, UNACCOUNTED,
                               UNCOVERED, UNCOVERED_SOURCES, UNINSPECTED,
                               WALKED, Admitted, attest, collect, enabled,
                               note_degraded, run_record, take_degraded)

#: Every marker this module can emit. A run record carries exactly one.
MARKERS = (
    "hidden_white_text",       # text the same colour as the ground it sits on
    "hidden_transparent_text",  # zero-alpha colour: invisible on any ground
    "hidden_tiny_text",        # resolved font size below 2pt
    "hidden_offscreen_text",   # parked far off the page / outside the slide
    "hidden_vanish_run",       # Word w:vanish / w:webHidden
    "hidden_render_mode",      # PDF text render mode 3 or 7 (invisible)
    "hidden_undecoded_text",   # invisible PDF text whose operand is glyph codes
    "hidden_display_none",     # CSS display:none / opacity:0 / `hidden` attribute
    "hidden_visibility",       # CSS visibility:hidden / collapse
)

#: A channel this high counts as white. Exact ``FFFFFF`` and this threshold
#: select the identical set on the 538-document corpus; the threshold exists
#: to defeat ``#FEFEFE``.
NEAR = 0xF0
#: Per-channel distance at which text is "the same colour as its ground".
GROUND_DELTA = 0x10
#: Resolved sizes below this many CSS pixels are hidden (design: < 2pt).
TINY_PX = 2 * 4 / 3
#: An offset at least this far negative is off the page, not a layout nudge.
OFFSCREEN_PX = 1000.0

_HEX6 = re.compile(r"#([0-9a-f]{6})\b", re.I)
_HEX3 = re.compile(r"#([0-9a-f]{3})\b", re.I)
_RGB = re.compile(
    r"rgba?\(\s*([0-9.]+)\s*[, ]\s*([0-9.]+)\s*[, ]\s*([0-9.]+)"
    r"(?:\s*[,/]\s*([0-9.%]+))?", re.I)
_HSL = re.compile(
    r"hsla?\(\s*(-?[0-9.]+)\s*(?:deg)?\s*[, ]\s*([0-9.]+)%\s*[, ]\s*([0-9.]+)%"
    r"(?:\s*[,/]\s*([0-9.%]+))?", re.I)
_SIZE = re.compile(r"(-?[0-9.]+)\s*(px|pt|em|rem|pc|in|cm|mm)", re.I)
_DECL = re.compile(r"([a-z-]+)\s*:\s*([^;]+)", re.I)
_UNIT_PX = {"px": 1.0, "pt": 4 / 3, "em": 16.0, "rem": 16.0,
            "pc": 16.0, "in": 96.0, "cm": 96 / 2.54, "mm": 9.6 / 2.54}
_OFFSET_PROPS = ("text-indent", "left", "top", "margin-left", "margin-top")
WHITE = (255, 255, 255)


# --------------------------------------------------------------------------
# Shared vocabulary: colour, size, offset.
# --------------------------------------------------------------------------
def parse_colour(value: str) -> tuple[int, int, int, float] | None:
    """``(r, g, b, alpha)`` for a CSS colour, or ``None`` if it names none.

    Handles ``#rgb``/``#rrggbb``, ``rgb()``/``rgba()``, ``hsl()``/``hsla()``
    and the two keywords that matter here. A zero alpha is REPORTED, not
    silently dropped: ``rgba(0,0,0,0)`` is invisible text, and reading it as
    opaque black is the bug this function exists to avoid.
    """
    v = (value or "").strip().lower()
    if not v or v in ("inherit", "initial", "unset", "revert", "none", "currentcolor"):
        return None
    if v == "transparent":
        return (0, 0, 0, 0.0)
    if v == "white":
        return (255, 255, 255, 1.0)
    if v == "black":
        return (0, 0, 0, 1.0)
    m = _RGB.search(v)
    if m:
        rgb = tuple(min(255, max(0, int(float(g)))) for g in m.groups()[:3])
        return (rgb[0], rgb[1], rgb[2], _alpha(m.group(4)))
    m = _HSL.search(v)
    if m:
        r, g, b = _hsl_to_rgb(float(m.group(1)), float(m.group(2)), float(m.group(3)))
        return (r, g, b, _alpha(m.group(4)))
    m = _HEX6.search(v)
    if m:
        h = m.group(1)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    m = _HEX3.search(v)
    if m:
        h = m.group(1)
        return (int(h[0] * 2, 16), int(h[1] * 2, 16), int(h[2] * 2, 16), 1.0)
    return None


def _alpha(raw: str | None) -> float:
    """The alpha channel of a colour, defaulting to opaque.

    An unparseable alpha is REPORTED (V14 audit, 2026-09-03): a browser drops
    the whole declaration, so opaque matches the render, but the walk did not
    resolve this colour and a document whose colours it could not resolve is
    not one it searched completely.
    """
    if raw is None:
        return 1.0
    try:
        return float(raw[:-1]) / 100 if raw.endswith("%") else float(raw)
    except ValueError:
        note_degraded()
        return 1.0


def _hsl_to_rgb(h: float, s: float, ll: float) -> tuple[int, int, int]:
    h = (h % 360) / 360.0
    s, ll = max(0.0, min(1.0, s / 100)), max(0.0, min(1.0, ll / 100))
    if s == 0:
        v = round(ll * 255)
        return (v, v, v)
    q = ll * (1 + s) if ll < 0.5 else ll + s - ll * s
    p = 2 * ll - q

    def channel(t: float) -> int:
        t = t % 1.0
        if t < 1 / 6:
            val = p + (q - p) * 6 * t
        elif t < 1 / 2:
            val = q
        elif t < 2 / 3:
            val = p + (q - p) * (2 / 3 - t) * 6
        else:
            val = p
        return round(val * 255)

    return (channel(h + 1 / 3), channel(h), channel(h - 1 / 3))


def near_white(rgb: tuple[int, ...] | None) -> bool:
    """Every channel at or above ``NEAR``. ``None`` is not a colour."""
    return rgb is not None and all(c >= NEAR for c in rgb[:3])


def colour_marker(colour: tuple[int, int, int, float] | None,
                  ground: tuple[int, int, int, float] | None) -> str | None:
    """The marker a resolved text colour earns against its resolved ground.

    ``hidden_transparent_text`` for a zero alpha (invisible whatever is
    behind it) and ``hidden_white_text`` when the text is within
    ``GROUND_DELTA`` of its ground — white-on-white being the case this was
    measured on. Named for that case because that is what it catches in
    practice; the test is "the same colour as what is behind it".
    """
    if colour is None:
        return None
    if colour[3] <= 0.0:
        return "hidden_transparent_text"
    bg = ground if ground is not None and ground[3] > 0.0 else (*WHITE, 1.0)
    if all(abs(colour[i] - bg[i]) <= GROUND_DELTA for i in range(3)):
        return "hidden_white_text"
    return None


def to_px(value: str) -> float | None:
    """A CSS length in pixels, or ``None`` when the value carries no unit."""
    m = _SIZE.search(value or "")
    if not m:
        return None
    return float(m.group(1)) * _UNIT_PX[m.group(2).lower()]


def is_tiny(px: float | None) -> bool:
    return px is not None and 0 <= px < TINY_PX


def declarations(style: str | None) -> dict[str, str]:
    return {k.strip().lower(): v.strip() for k, v in _DECL.findall(style or "")}


def normalise(text: str) -> str:
    return " ".join((text or "").split())

# The per-format rules live in their own modules (the 500-LOC file bound; HTML
# is also the one format whose styling CASCADES). Re-exported so every caller —
# the handlers and the tests alike — keeps importing them from here.
from .concealment_html import (html_runs_lxml, html_runs_stdlib,  # noqa: E402,F401
                               stylesheet)
from .concealment_office import (_docx_paragraphs, _font_marker,  # noqa: E402,F401
                                 _office_rgb, _shape_offscreen, _theme_white,
                                 docx_runs, pptx_runs)
from .concealment_pdf import (PRINTABLE_FLOOR, _decode_operand,  # noqa: E402,F401
                              _pdf_marker, _shown_text, page_fonts,
                              pdf_operand_visitor)
