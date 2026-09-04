"""The PDF lane: a text-render-mode state machine, and the font decode.

Split out of :mod:`concealment` for the 500-LOC file bound, exactly as
``concealment_html``/``concealment_office`` were. Every name is re-exported
from ``concealment``, so no caller imports this module directly.

**Gap 5 is CLOSED here (round 7, 2026-09-04).** A ``/Encoding /Differences``
or CID font makes a show operator's string operand a run of GLYPH CODES:
``page.extract_text()`` maps them through the font and the reader sees the
sentence, while this visitor used to see control bytes. Round 6 put a number
on the damage — the visitor reports what it decoded, the ledger compares that
with the page ``extract_text()`` returned, and 131 of 144 PDFs in the
reference corpus had at least one page where the two disagreed, so those notes
could not claim a scan they had not really performed.

The fix is to decode the operand the same way pypdf's own extractor does:
:func:`page_fonts` builds the page's font resources into
``pypdf._font.Font`` objects, the visitor tracks the ``Tf`` operator to know
which one is current, and :func:`_decode_operand` runs the bytes through that
font's encoding and ToUnicode ``character_map``. Measured on three corpus PDFs
that failed round 6: pages whose report did not cover the page went 2/8/10 to
**0/0/0**.

Three ceilings, all of which fail CLOSED — a decode that goes wrong produces
text that does NOT match the page, which reads ``uninspected``, never a false
``full``:

* A font map is built per PAGE, from ``page.get_inherited("/Resources")``.
  Content inside a form XObject resolves font names against the XObject's OWN
  resource dictionary, so a name colliding across the two decodes with the
  wrong font. (pypdf's visitor does not descend into form XObjects at all —
  gap 4 — so today nothing reaches that case.)
* Anything the font map cannot build (a broken resource, an unreadable
  stream) leaves the operand on the pre-round-7 raw decode.
* A font with no ToUnicode CMap has nothing to map through; its glyph codes
  stay glyph codes and the page still reads short.

Those ceilings have a MEASURED residue, and it is the reason this list is not
prose: after the fix, 3 of 144 reference-corpus PDFs still read
``uninspected``, each on one page carrying an agency reference stamp whose
operand the walker decoded to mojibake where ``extract_text`` read it
correctly. That is the third bullet doing its job — the note says nothing
searched that text, which is true.
"""
from __future__ import annotations

from typing import Any, Callable

from .concealment import enabled, normalise, note_degraded, run_record

_SHOW_OPS = (b"Tj", b"TJ", b"'", b'"')
_INVISIBLE_MODES = (3, 7)


def page_fonts(page: Any) -> dict[str, Any]:
    """The page's font resources, by resource name, as ``pypdf`` fonts.

    ``get_inherited`` is used rather than ``page["/Resources"]`` because a
    page legitimately inherits its resource dictionary from an ancestor
    ``/Pages`` node. An empty result is the normal no-op: the visitor then
    decodes exactly as it did before this existed.
    """
    fonts: dict[str, Any] = {}
    try:
        from pypdf._font import Font

        resources = page.get_inherited("/Resources", None)
        table = resources.get("/Font") if resources else None
        if not table:
            return fonts
        for name in table:
            try:
                fonts[name] = Font.from_font_resource(table[name].get_object())
            except Exception:  # coverage-audit: one unbuildable font leaves
                # its operands on the raw decode; the page then reads short
                # rather than claiming a scan it did not perform.
                note_degraded()
    except Exception:  # coverage-audit: same, for the whole page
        note_degraded()
    return fonts


def pdf_operand_visitor(page_no: int, sink: list[dict[str, Any]],
                        seen: Callable[[str], None] | None = None,
                        page: Any = None) -> Callable[..., None]:
    """A ``visitor_operand_before`` that records text drawn invisibly.

    Text render mode is PAGE state, not text-object state: it is initialised
    once per content stream and persists ACROSS ``BT``/``ET`` pairs, so it is
    deliberately NOT reset at ``BT``. Only ``q``/``Q`` save and restore it.
    ``BT`` opens a new container for reporting and nothing more.

    **The kill switch is honoured HERE, not by the caller** (review finding
    V6, 2026-09-02). Every other lane routes its walk through
    ``concealment_gate.collect``, which checks ``enabled``; the PDF lane
    cannot, because pypdf owns the walk and this is a callback handed to
    ``page.extract_text``. So the check sits on the only line both the switch
    and the PDF lane share. With the switch off a PDF could still be
    quarantined until this landed — during exactly the incident the switch
    exists to end.

    ``seen`` is the coverage sink (``Admitted.watch``): the decoded operand of
    EVERY show operator is reported to it, not only the invisible ones. That
    report is what the ledger holds against the page ``extract_text()``
    admitted. ``page`` is what makes that report READABLE — see the module
    docstring; omit it and the decode is the pre-round-7 raw one.
    """
    if not enabled():
        return lambda *_a, **_k: None
    state: dict[str, Any] = {"mode": 0, "obj": 0, "font": None}
    stack: list[int] = []
    fonts = page_fonts(page) if page is not None else {}

    def visit(operator: Any, args: Any, _cm: Any = None, _tm: Any = None) -> None:
        try:
            if operator == b"q":
                stack.append(state["mode"])
            elif operator == b"Q":
                state["mode"] = stack.pop() if stack else 0
            elif operator == b"BT":
                state["obj"] += 1
            elif operator == b"Tf":
                state["font"] = fonts.get(args[0]) if args else None
            elif operator == b"Tr":
                state["mode"] = int(args[0]) if args else 0
            elif operator in _SHOW_OPS:
                text = normalise(_shown_text(operator, args, state["font"]))
                if text and seen is not None:
                    seen(text)
                if text and state["mode"] in _INVISIBLE_MODES:
                    sink.append(run_record(
                        _pdf_marker(text),
                        f"pdf:p{page_no}o{state['obj']}",
                        f"pdf:page{page_no}", text))
        except Exception:  # a detector never breaks extraction
            note_degraded()

    return visit


def _shown_text(operator: Any, args: Any, font: Any = None) -> str:
    """Decode the string operand of a text-showing operator.

    ``str()`` on a raw operand yields a ``ByteStringObject``'s repr
    (``b'...'``), not its text, so the decode is explicit here.
    """
    if not args:
        return ""
    if operator == b"TJ":
        parts = []
        for item in (args[0] or []):
            if isinstance(item, (int, float)) and not isinstance(item, (str, bytes)):
                continue
            parts.append(_decode_operand(item, font))
        return "".join(parts)
    if operator == b'"':
        return _decode_operand(args[2], font) if len(args) > 2 else ""
    return _decode_operand(args[0], font)


#: Below this share of printable characters, the operand is glyph CODES that
#: no font could map, and no instruction pattern can ever match it.
PRINTABLE_FLOOR = 0.8


def _pdf_marker(text: str) -> str:
    """``hidden_render_mode`` when the run really is its own text.

    Since round 7 the operand is decoded through the current font
    (:func:`_decode_operand`), so the usual glyph-coded case reaches here as
    readable text and is pattern-matched like any other run. What still lands
    as ``hidden_undecoded_text`` is a run whose font offered no ToUnicode
    CMap, or none the map covers: reported by ``brain integrity --injection``
    as invisible text that could not be read, rather than silently carrying
    bytes no pattern can match. It never convicts on its own.
    """
    if not text:
        return "hidden_render_mode"
    printable = sum(1 for ch in text if ch.isprintable() or ch.isspace())
    return ("hidden_render_mode" if printable >= PRINTABLE_FLOOR * len(text)
            else "hidden_undecoded_text")


def _decode_operand(operand: Any, font: Any = None) -> str:
    """One string operand as text, through ``font`` when there is one.

    The font branch mirrors ``pypdf._text_extraction.get_text_operands`` —
    deliberately, so this visitor and the ``extract_text()`` the ledger
    compares it against read the same bytes the same way. It is not imported
    from there: that helper also needs the text and current-transformation
    matrices to decide orientation, which a ``visitor_operand_before`` is not
    given.
    """
    if isinstance(operand, str):
        return operand
    if not isinstance(operand, bytes):
        return ""
    if font is not None:
        try:
            return _through_font(operand, font)
        except Exception:  # coverage-audit: falls back to the raw decode
            # below, which reads short rather than wrong.
            note_degraded()
    try:
        return operand.decode("utf-8")
    except UnicodeDecodeError:  # coverage-audit: recovers, loses no text
        return operand.decode("latin-1", "replace")


def _through_font(raw: bytes, font: Any) -> str:
    encoding = font.encoding
    if isinstance(encoding, str):
        try:
            text = raw.decode(encoding, "surrogatepass")
        except Exception:  # coverage-audit: pypdf's own alternative, same
            # reason and same order as `get_text_operands`.
            text = raw.decode(
                "utf-16-be" if encoding == "charmap" else "charmap",
                "surrogatepass")
    else:
        text = "".join(encoding[x] if x in encoding
                       else bytes((x,)).decode("latin-1") for x in raw)
    mapping = font.character_map
    return "".join(mapping.get(ch, ch) for ch in text)
