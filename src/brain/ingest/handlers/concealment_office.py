"""DOCX / PPTX concealment — run-level font inspection on the live object model.

Its own module for the same reason ``concealment_html`` is: ``concealment.py``
lives under a 500-LOC file bound. ``concealment`` re-exports everything here,
so callers keep the one import they had.

Two round-1 review findings are answered in this file, each REPRODUCED first
(2026-09-02, transcripts in the session's probe-after evidence):

* ``w:vanish`` supplied by a CHARACTER or PARAGRAPH STYLE reads
  ``run.font.hidden is None``. Measured: ``run.style.font.hidden is True`` for
  the same run. The marker now resolves down the run → character style →
  paragraph style chain, first specified value winning, like the renderer.
* ``_docx_paragraphs`` yielded every top-level paragraph and only then every
  table cell. ``docx.py:_document_parts`` renders them INTERLEAVED in body
  order, so a payload split between a paragraph and the table under it had a
  third element wedged between its halves and the join returned ``clean``.
  This module now walks ``document.element.body`` in the renderer's order.
"""
from __future__ import annotations

from typing import Any, Callable

from .concealment import colour_marker, normalise, note_degraded, run_record

#: Theme slots that are white in every stock Office theme. ``pptx.py`` reads no
#: theme part, so this is a NAME test, not a resolved colour — see the gap list
#: in ``concealment``'s docstring. It exists because a run painted
#: ``BACKGROUND_1`` over a default white slide was invisible AND undetected
#: (reproduced 2026-09-02: ``color.type`` is ``SCHEME``, ``.rgb`` raises).
WHITE_THEME_SLOTS = ("BACKGROUND_1", "LIGHT_1")


def _office_rgb(colour: Any, rgb_type: Any) -> tuple[int, int, int, float] | None:
    """``(r, g, b, 1.0)`` for an EXPLICIT RGB font colour, else ``None``.

    ``.rgb`` RAISES ``AttributeError`` on a python-pptx SCHEME colour
    (``pptx/dml/color.py``), and the handlers catch at document level — so
    reading it unguarded would turn an ordinary theme-coloured deck into
    ``pptx_extraction_error``. Theme colours are handled by :func:`_theme_white`.
    """
    if colour is None:
        return None
    try:
        kind = colour.type
    except Exception:
        note_degraded()
        return None
    if kind != rgb_type:
        # A THEME, AUTO or unset colour. The ORDINARY path, not a failure:
        # counting it would stamp every theme-coloured deck `incomplete`.
        return None
    try:
        value = colour.rgb
    except (AttributeError, ValueError, TypeError):
        # `.type` said RGB and `.rgb` still would not answer — an anomaly, and
        # this run's colour is now unknown to the walk (V14 audit, 2026-09-03).
        note_degraded()
        return None
    if value is None:
        return None
    hexed = str(value)
    if len(hexed) != 6:
        note_degraded()
        return None
    try:
        return (int(hexed[0:2], 16), int(hexed[2:4], 16), int(hexed[4:6], 16), 1.0)
    except ValueError:
        note_degraded()
        return None


def _theme_white(colour: Any) -> bool:
    """True for a font painted with the theme's white slot.

    ``getattr``'s default absorbs the ``AttributeError`` python-pptx raises for
    a colour that has no theme slot — the ORDINARY case, and the reason this
    catch does not count a degradation. Anything else IS one (V14 audit,
    2026-09-03): the previous shape caught the ordinary case in the same
    ``except`` as a real failure, so it could not have told them apart.
    """
    if colour is None:
        return False
    try:
        raw = getattr(colour, "theme_color", None)
    except Exception:
        note_degraded()
        return False
    parts = ("" if raw is None else str(raw)).split()
    return bool(parts) and parts[0] in WHITE_THEME_SLOTS


def _font_marker(font: Any, rgb_type: Any, tiny_limit: Any) -> str | None:
    why = colour_marker(_office_rgb(getattr(font, "color", None), rgb_type), None)
    if why:
        return why
    if _theme_white(getattr(font, "color", None)):
        return "hidden_white_text"
    size = getattr(font, "size", None)
    if size is not None and size < tiny_limit:
        return "hidden_tiny_text"
    return None


# --------------------------------------------------------------------------
# DOCX
# --------------------------------------------------------------------------
def _fonts(run: Any, paragraph: Any) -> list[Any]:
    """The run's font, then its character style's, then its paragraph's.

    Most specific first, so the FIRST specified value wins — the resolution
    order Word itself uses. A ``basedOn`` ancestor style is not followed
    (named gap 7 in ``concealment``'s docstring).
    """
    fonts = [run.font]
    for owner in (getattr(run, "style", None), getattr(paragraph, "style", None)):
        font = getattr(owner, "font", None)
        if font is not None:
            fonts.append(font)
    return fonts


def _first(fonts: list[Any], attr: str) -> Any:
    for font in fonts:
        value = getattr(font, attr, None)
        if value is not None:
            return value
    return None


def _docx_marker(run: Any, paragraph: Any, rgb_type: Any, tiny: Any) -> str | None:
    fonts = _fonts(run, paragraph)
    if _first(fonts, "hidden") or _first(fonts, "web_hidden"):
        return "hidden_vanish_run"
    for font in fonts:
        colour = _office_rgb(getattr(font, "color", None), rgb_type)
        if colour is not None:
            why = colour_marker(colour, None)
            return why or _tiny(fonts, tiny)
        if _theme_white(getattr(font, "color", None)):
            return "hidden_white_text"
    return _tiny(fonts, tiny)


def _tiny(fonts: list[Any], tiny: Any) -> str | None:
    size = _first(fonts, "size")
    return "hidden_tiny_text" if size is not None and size < tiny else None


def docx_runs(document: Any,
              seen: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
    """Hidden runs in a python-docx document, in the RENDERER'S body order.

    Headers, footers, footnotes and comments are NOT walked, because
    ``docx.py``'s ``_document_parts`` does not extract them — text that never
    reaches the Markdown cannot reach a model, so there is nothing to conceal
    into.

    ``seen`` is the coverage sink (``Admitted.watch``): every run's text is
    reported to it, whether hidden or not. That report is what the ledger
    compares against the text the handler admitted, and it is why this walk no
    longer has to CLAIM it reads what ``paragraph.text`` renders. It does not:
    ``paragraph.runs`` excludes the runs inside a ``w:hyperlink``, and a
    ``w:vanish`` run parked in one was invisible to this function while its
    text went into the note (V15, 2026-09-03). Nothing here special-cases a
    hyperlink; the shortfall does the work.
    """
    from docx.enum.dml import MSO_COLOR_TYPE
    from docx.oxml.ns import qn
    from docx.shared import Pt
    from docx.text.run import Run

    rgb_type, tiny, run_tag = MSO_COLOR_TYPE.RGB, Pt(2), qn("w:r")
    runs: list[dict[str, Any]] = []
    for index, paragraph in enumerate(_docx_paragraphs(document), start=1):
        container = f"docx:p{index}"
        # Every ``w:r`` this paragraph OWNS, at any depth — not
        # ``paragraph.runs``, which is its DIRECT ``w:r`` children only.
        # Word nests a run one level down whenever it wraps it (a link, a
        # bookmark, a tracked insertion, a smart tag), and `paragraph.text`
        # renders those runs while `paragraph.runs` does not: that gap is
        # what let a `w:vanish` run carry an injection into the corpus under
        # a stamped `full` (V15). This names none of the wrappers — it walks
        # the element that HOLDS text, so the next wrapper nobody has seen
        # falls in with them (round 7, 2026-09-04).
        for run in (Run(el, paragraph) for el in paragraph._p.iter(run_tag)):
            # Per-RUN tolerance: one unreadable run never costs the document's
            # other runs (the fail-open decision, `concealment.collect`).
            try:
                text = normalise(run.text)
                if seen is not None and text:
                    seen(text)
                why = _docx_marker(run, paragraph, rgb_type, tiny)
            except Exception:
                note_degraded()
                continue
            if why and text:
                runs.append(run_record(why, container, f"docx:para{index}", text))
    return runs


def _docx_paragraphs(document: Any) -> Any:
    """Body paragraphs and table-cell paragraphs INTERLEAVED in body order.

    Mirrors ``docx.py:_document_parts``, which walks ``element.body`` children
    and renders each ``w:p``/``w:tbl`` where it stands. Yielding all
    paragraphs and then all cells reordered the joined hidden text and
    defeated the split-payload case (reproduced 2026-09-02).
    """
    tables = {table._tbl: table for table in document.tables}
    paragraphs = {para._p: para for para in document.paragraphs}
    for child in document.element.body.iterchildren():
        tag = str(child.tag).rsplit("}", 1)[-1]
        if tag == "p":
            para = paragraphs.get(child)
            if para is not None:
                yield para
        elif tag == "tbl":
            table = tables.get(child)
            for row in (table.rows if table is not None else ()):
                for cell in row.cells:
                    yield from cell.paragraphs


# --------------------------------------------------------------------------
# PPTX
# --------------------------------------------------------------------------
def pptx_runs(presentation: Any,
              seen: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
    """Hidden runs in a python-pptx deck: table cells AND shape text frames.

    Both are text paths ``pptx.py`` really extracts (``c.text`` for tables,
    ``shape.text_frame.text`` for frames), and both are what the
    ``pptx:native`` source in ``COVERED_SOURCES`` names.

    Pictures read by OCR are NOT covered by this walk, and saying "out of
    scope — OCR output carries no styling" is exactly what let a picture-only
    deck carry its whole payload under a stamped ``full`` (V13, 2026-09-03):
    the styling is in the RASTER, which nothing here reads. ``pptx.py`` admits
    that text under ``pptx:picture_ocr``, which is uncovered, so such a note
    reads ``unknown``. Presenter notes are genuinely out of scope: ``pptx.py``
    never touches ``slide.notes_slide``, so that text never reaches the note
    at all and there is nothing to conceal into.

    ``seen`` is the coverage sink (``Admitted.watch``), and it carries the same
    correction ``docx_runs`` does: ``shape.text_frame.text`` includes field
    (``a:fld``) text that ``paragraph.runs`` does not hold, so this walk does
    not read everything ``pptx.py`` renders. Reporting what it DID read is what
    makes that gap visible instead of asserted away (V15, 2026-09-03).
    """
    from pptx.enum.dml import MSO_COLOR_TYPE
    from pptx.util import Pt

    runs: list[dict[str, Any]] = []
    width, height = presentation.slide_width, presentation.slide_height
    style = (MSO_COLOR_TYPE.RGB, Pt(2))
    for si, slide in enumerate(presentation.slides, start=1):
        for k, shape in enumerate(slide.shapes):
            offscreen = _shape_offscreen(shape, width, height)
            if getattr(shape, "has_table", False):
                for ri, row in enumerate(shape.table.rows):
                    for ci, cell in enumerate(row.cells):
                        _frame_runs(cell.text_frame, offscreen,
                                    f"pptx:s{si}t{k}r{ri}c{ci}", si, style,
                                    runs, seen)
            if getattr(shape, "has_text_frame", False):
                _frame_runs(shape.text_frame, offscreen,
                            f"pptx:s{si}sh{k}", si, style, runs, seen)
    return runs


def _frame_runs(text_frame: Any, offscreen: bool, key: str, slide_no: int,
                style: tuple[Any, Any], runs: list[dict[str, Any]],
                seen: Callable[[str], None] | None = None) -> None:
    """Append every hidden run in one text frame. Module level, not nested:
    a closure's branches count toward its parent's complexity ratchet."""
    rgb_type, tiny = style
    for pi, para in enumerate(text_frame.paragraphs):
        for element in _dml_text_elements(para):
            # Per-RUN tolerance (the fail-open decision, `concealment.collect`).
            try:
                text = normalise(_dml_text(element))
                if seen is not None and text:
                    seen(text)
                why = _font_marker(_dml_font(element), rgb_type, tiny)
            except Exception:
                note_degraded()
                continue
            if not why and offscreen:
                why = "hidden_offscreen_text"
            if why and text:
                runs.append(run_record(
                    why, f"{key}p{pi}", f"pptx:slide{slide_no}", text))


def _dml_text_elements(paragraph: Any) -> list[Any]:
    """Every element in this paragraph that OWNS text, whatever holds it.

    ``paragraph.runs`` is the paragraph's DIRECT ``a:r`` children.
    ``text_frame.text`` renders more than that: DrawingML also carries text in
    other elements at the same level, each with its own ``a:rPr``, and a white
    one of those carried an injection into the corpus while this walk reported
    nothing (V15, 2026-09-03). The rule here names none of them — an element
    is text-bearing when it has an ``a:t`` child, so the next such element
    falls in with the ones already known (round 7, 2026-09-04).

    Document order, because the ledger's coverage cursor compares the walk's
    report against the renderer's chunks in the order both produced them.
    """
    from pptx.oxml.ns import qn

    tag = qn("a:t")
    return [el for el in paragraph._p.iter() if el.find(tag) is not None]


def _dml_text(element: Any) -> str:
    from pptx.oxml.ns import qn

    node = element.find(qn("a:t"))
    return "" if node is None else (node.text or "")


def _dml_font(element: Any) -> Any:
    """The element's own run properties as a python-pptx ``Font``, or ``None``.

    ``None`` for an element that declares none, which :func:`_font_marker`
    already reads as "no colour, no size" — the inherited case, and not a
    failure.
    """
    from pptx.oxml.ns import qn
    from pptx.text.text import Font

    props = element.find(qn("a:rPr"))
    return None if props is None else Font(props)


def _shape_offscreen(shape: Any, width: Any, height: Any) -> bool:
    """A shape whose origin sits outside the slide rectangle.

    ``left``/``top`` of ``None`` means the shape inherits its placeholder's
    position — unknown, not off-canvas.
    """
    try:
        left, top = shape.left, shape.top
    except (AttributeError, ValueError):
        # V14, 2026-09-03: this was the SIXTH swallowing catch in the
        # concealment surface and the only one that did not report. A
        # nonnumeric `x` in the shape's XML made `shape.left` raise, the
        # off-screen check answered "not off-screen", the counter stayed at
        # zero and the note read `full` over attacker-placed text.
        note_degraded()
        return False
    if left is None or top is None or width is None or height is None:
        return False
    return bool(left < 0 or top < 0 or left > width or top > height)
