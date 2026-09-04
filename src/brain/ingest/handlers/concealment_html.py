"""HTML concealment detection — the lxml path and the stdlib fallback (M-3).

Its own module for the 500-LOC file bound, and because HTML is the one format
whose styling is a CASCADE: colour, background, size and the render-state
properties inherit down the tree, so it needs a resolved-style stack that no
other handler does.

``concealment`` re-exports :func:`html_runs_lxml` and :func:`html_runs_stdlib`,
so callers keep the one import they had.

**Both paths must agree.** ``handlers/html.py`` prefers lxml and falls back to
the stdlib parser wherever lxml is missing or raised; if only one of them saw
concealment, the rule would silently vanish on some machines. ``email.py``'s
html-only body uses the stdlib path directly — mail is the primary ingest lane
and an html-only ``.eml`` was a one-line bypass of this whole control until
2026-09-02.

What this file resolves, and the ceiling on each (rework 2, after review):

* inline ``style=""`` — fully;
* a ``<style>`` block, but only for SIMPLE selectors (``tag``, ``.class``,
  ``#id``). No descendant, attribute or pseudo selector is matched and
  specificity is approximated by tag < class < id < inline. A payload hidden
  by ``.a .b {display:none}`` is therefore still missed. An ``@media`` block
  is NOT skipped: the at-rule wrapper is stepped over and the simple rules
  inside it are resolved as if they applied unconditionally, so
  ``@media print{.p{color:#fff}}`` convicts (corrected 2026-09-02, review
  finding V8 — this said "``@media`` blocks never match" and the probe
  ``st_media_print`` contradicted it). That is an over-fire and it is inside
  the measured 0.00% conviction rate, not an untested claim;
* ``display:none``, the ``hidden`` attribute, ``opacity:0`` and
  ``visibility:hidden`` — the five ordinary hiding states the round-1 review
  found admitted. ``display``/``opacity``/``hidden`` latch for the whole
  subtree (a descendant cannot undo them); ``visibility`` is inherited and IS
  undone by a descendant's ``visibility:visible``, which is how a real browser
  behaves and how a real page reveals one cell of a hidden table;
* ``clip`` and ``clip-path`` collapsed to nothing (med-11, 2026-09-04) — the
  sixth. Measured before it was built: ``display:none``, ``visibility:hidden``
  and an off-canvas ``text-indent`` in absolute units were ALREADY caught here,
  and five clip shapes were not. It latches like ``display`` and carries the
  same ``hidden_display_none`` marker, because a box clipped to nothing paints
  no pixels and no descendant can escape its ancestor's clip — the property
  that family is named for. See :func:`_clipped_away` for what counts and for
  the ceiling on it.
"""
from __future__ import annotations

import html.parser
import re
from typing import Any, Callable

from .concealment import (
    WHITE, colour_marker, declarations, is_tiny, normalise, note_degraded,
    parse_colour, run_record, to_px, OFFSCREEN_PX, _OFFSET_PROPS,
)

#: A clip narrower or shorter than this paints nothing a reader can read. Not
#: exactly zero: the ``clip: rect(1px,1px,1px,1px)`` idiom is the same trick as
#: ``rect(0,0,0,0)`` and is what several real toolkits actually ship.
CLIP_FLOOR_PX = 1.0
_CLIP_RECT = re.compile(r"rect\(([^)]*)\)", re.I)
_CLIP_SHAPE = re.compile(r"(inset|circle|ellipse|polygon)\(([^)]*)\)", re.I)
_PERCENT = re.compile(r"(-?[0-9.]+)\s*%")

#: Elements whose text the reader sees as one paragraph. Used only to name a
#: container for REPORTING — classification is document-wide (see
#: ``injection_scan.fold_concealed``).
_CONTAINER_TAGS = frozenset({
    "p", "div", "li", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "blockquote", "pre", "body", "tr", "table",
    "dd", "dt", "figcaption", "header", "footer", "main", "aside",
})
#: Void elements never fire ``handle_endtag``; pushing a style frame for one
#: would leave the stack permanently unbalanced.
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})
#: An alpha this low is invisible on any ground. Not exactly zero: a real
#: attacker writes ``opacity:.01`` and a real designer never does.
OPACITY_FLOOR = 0.05

_STYLE_BLOCK = re.compile(r"<style[^>]*>(.*?)</style>", re.I | re.S)
_RULE = re.compile(r"([^{}@]+)\{([^{}]*)\}", re.S)
#: A selector this module refuses to resolve — anything past one simple name.
_COMPLEX_SELECTOR = re.compile(r"[\s>+~:\[*(]")


def stylesheet(raw_html: str) -> dict[str, str]:
    """Declarations keyed by SIMPLE selector, from every ``<style>`` block.

    ``{".x": "display:none", "td": "color:#fff"}``. Deliberately not a CSS
    engine: a selector carrying a combinator, pseudo-class, attribute test or
    wildcard is SKIPPED rather than guessed at.

    **``@media`` is stepped over, not skipped** (corrected 2026-09-02, review
    finding V8 — this said such blocks "never [apply]"). ``_RULE``'s selector
    class excludes ``@``, so ``@media print { .p{color:#fff} }`` fails to match
    at the at-rule and the scan resumes INSIDE the block, where ``.p{...}``
    matches like any other simple rule. The condition is discarded and the rule
    is treated as unconditional, so a print-only ``display:none`` convicts. Two
    consequences a reader needs: an over-fire on legitimate print-only styling
    (measured — still 0 convictions in 538 real originals), and no bypass in
    the other direction, since wrapping a hiding rule in ``@media`` does not
    hide it from this parser.

    ponytail: simple selectors only, and the at-rule condition unread. A real
    cascade engine is the upgrade path and is session s05d's; today this closes
    the ``.hidden{display:none}`` case that the round-1 review demonstrated and
    ``concealment``'s module gap list names the rest.
    """
    out: dict[str, str] = {}
    for block in _STYLE_BLOCK.findall(raw_html or ""):
        for selectors, decls in _RULE.findall(block):
            for sel in selectors.split(","):
                sel = sel.strip().lower()
                if not sel or _COMPLEX_SELECTOR.search(sel):
                    continue
                out[sel] = f"{out.get(sel, '')};{decls}"
    return out


def _sheet_style(sheet: dict[str, str], tag: str, attrs: dict[str, Any]) -> str:
    """The stylesheet declarations that apply to one element, least specific
    first, so the inline ``style=""`` applied after them still wins."""
    if not sheet:
        return ""
    parts = [sheet.get(tag, "")]
    parts += [sheet.get(f".{c.lower()}", "")
              for c in str(attrs.get("class") or "").split()]
    parts.append(sheet.get(f"#{str(attrs.get('id') or '').lower()}", ""))
    return ";".join(p for p in parts if p)


# --------------------------------------------------------------------------
class _Style:
    """A resolved style, inherited down the element tree."""

    __slots__ = ("colour", "ground", "size", "offscreen", "undisplayed", "invisible")

    def __init__(self, colour: Any = (0, 0, 0, 1.0), ground: Any = (*WHITE, 1.0),
                 size: float = 16.0, offscreen: bool = False,
                 undisplayed: bool = False, invisible: bool = False) -> None:
        self.colour, self.ground, self.size = colour, ground, size
        self.offscreen, self.undisplayed = offscreen, undisplayed
        self.invisible = invisible

    def resolve(self, style_attr: str | None, tag: str = "",
                attrs: dict[str, Any] | None = None,
                sheet: dict[str, str] | None = None) -> "_Style":
        attrs = attrs or {}
        own = declarations(
            f"{_sheet_style(sheet or {}, tag, attrs)};{style_attr or ''}")
        out = _Style(self.colour, self.ground, self.size, self.offscreen,
                     self.undisplayed, self.invisible)
        if "color" in own:
            c = parse_colour(own["color"])
            if c is not None:
                out.colour = c
        for bg in ("background-color", "background"):
            if bg in own:
                c = parse_colour(own[bg])
                if c is not None:
                    out.ground = c
        if "font-size" in own:
            px = to_px(own["font-size"])
            if px is not None:
                out.size = px
        for prop in _OFFSET_PROPS:
            px = to_px(own.get(prop, ""))
            if px is not None and px <= -OFFSCREEN_PX:
                out.offscreen = True
        out.undisplayed = out.undisplayed or _undisplayed(own, attrs)
        # `visibility` is INHERITED and a descendant can turn it back on.
        vis = own.get("visibility", "").strip().lower()
        if vis in ("hidden", "collapse"):
            out.invisible = True
        elif vis == "visible":
            out.invisible = False
        return out

    def marker(self) -> str | None:
        """The ONE marker this resolved style earns, most specific first."""
        if self.undisplayed:
            return "hidden_display_none"
        if self.invisible:
            return "hidden_visibility"
        why = colour_marker(self.colour, self.ground)
        if why:
            return why
        if is_tiny(self.size):
            return "hidden_tiny_text"
        return "hidden_offscreen_text" if self.offscreen else None


def _length_px(raw: str) -> float | None:
    """One CSS length in pixels, accepting the UNITLESS zero a clip may use.

    ``to_px`` answers ``None`` for ``0`` because it has no unit, and ``rect(0,
    0, 0, 0)`` is written exactly that way. ``auto`` is the box's own edge and
    cannot collapse anything, so it answers ``None`` too and the caller treats
    an unreadable side as not-clipped.
    """
    text = (raw or "").strip().lower()
    if not text or text == "auto":
        return None
    px = to_px(text)
    if px is not None:
        return px
    try:
        return float(text)
    except ValueError:  # coverage-audit: a clip argument that is not a
        # length (a keyword, a `calc()`) is UNKNOWN, not zero — `None` makes
        # `_clipped_away` decline to judge the shape rather than guess it
        # hidden. Nothing is swallowed: the run is still walked and still
        # judged on colour, size and position.
        return None


def _clipped_away(own: dict[str, str]) -> bool:
    """``clip`` or ``clip-path`` that leaves nothing of the box visible.

    The four shapes measured as UNCAUGHT on 2026-09-04, all of them the
    ordinary "visually hidden" idiom that a screen reader still reads and a
    model still ingests:

    * ``clip: rect(0,0,0,0)`` and ``clip: rect(1px,1px,1px,1px)`` — zero (or
      near-zero) width or height;
    * ``clip-path: inset(50%)`` upward — insetting half from every side leaves
      nothing;
    * ``clip-path: circle(0)`` / ``ellipse(0 0)`` — every radius zero;
    * ``clip-path: polygon(0 0,0 0,0 0)`` — fewer than three distinct points,
      so no area at all.

    CEILING, stated because this is a shape parser and not a renderer. It
    enumerates collapse FORMS, not areas: a polygon written with three
    distinct but collinear points, an inset in absolute units that happens to
    exceed the box, and every ``url(#id)`` reference to an SVG clipPath are
    not caught. Percentages are read only for ``inset``, where 50% has a
    box-independent meaning; anywhere else a percentage is left unresolved
    rather than guessed at.
    """
    rect = _CLIP_RECT.search(own.get("clip", ""))
    if rect:
        sides = [_length_px(part)
                 for part in re.split(r"[,\s]+", rect.group(1).strip()) if part]
        if len(sides) == 4 and not any(side is None for side in sides):
            top, right, bottom, left = sides  # type: ignore[misc]
            if (right - left) <= CLIP_FLOOR_PX or (bottom - top) <= CLIP_FLOOR_PX:
                return True
    shape = _CLIP_SHAPE.search(own.get("clip-path", ""))
    if not shape:
        return False
    name, args = shape.group(1).lower(), shape.group(2).strip()
    if name == "inset":
        return any(float(pct) >= 50.0 for pct in _PERCENT.findall(args))
    if name == "polygon":
        return len({tuple(point.split())
                    for point in args.split(",") if point.strip()}) < 3
    radii = [_length_px(token)
             for token in args.lower().split(" at ")[0].split()]
    return bool(radii) and all(radius == 0.0 for radius in radii)


def _undisplayed(own: dict[str, str], attrs: dict[str, Any]) -> bool:
    """``display:none``, ``opacity:0``, an empty clip, or ``hidden``.

    All four latch for the SUBTREE: a child of a ``display:none`` element is
    not rendered whatever it declares, ``opacity`` composites the whole
    subtree at once, and a descendant cannot paint outside an ancestor's clip.
    Only ``visibility`` is undoable, and it is handled apart.
    """
    if own.get("display", "").strip().lower() == "none":
        return True
    if _clipped_away(own):
        return True
    # PRESENCE, not value: `hidden` is a boolean attribute, and the stdlib
    # parser reports a valueless one as ``None`` — a truthiness test reads
    # `<div hidden>` as not hidden, which is the bug this comment prevents.
    if "hidden" in attrs:
        return True
    raw = own.get("opacity", "").strip().rstrip("%")
    if raw:
        try:
            scale = 100.0 if own["opacity"].strip().endswith("%") else 1.0
            return float(raw) / scale <= OPACITY_FLOOR
        except ValueError:
            # An opacity this walk cannot resolve. A browser drops the
            # declaration too, so `False` matches the render — but the walk
            # no longer knows this element's opacity, and saying so is what
            # keeps `full` honest (V14 audit, 2026-09-03).
            note_degraded()
            return False
    return False


def html_runs_lxml(doc: Any, raw_html: str = "",
                   seen: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
    """Hidden runs from an lxml tree, walked BEFORE ``doc.text_content()``.

    ``raw_html`` is the undropped source: ``html.py`` removes ``<style>``
    subtrees before this runs, so the stylesheet is read from the source text
    rather than the tree.

    ``seen`` is the coverage sink (``Admitted.watch``): every text node and
    tail is reported to it, hidden or not, in document order — the same order
    and the same nodes ``text_content()`` concatenates. The ledger compares
    the two rather than a table asserting they are the same set (V15).
    """
    runs: list[dict[str, Any]] = []
    counter = [0]
    sheet = stylesheet(raw_html)

    def walk(el: Any, inherited: _Style, container: str) -> None:
        tag = str(el.tag).lower() if isinstance(el.tag, str) else ""
        # Per-ELEMENT tolerance: one unparseable declaration loses this
        # element's own styling, never the rest of the document's runs.
        try:
            style = inherited.resolve(el.get("style"), tag, el.attrib, sheet)
        except Exception:
            note_degraded()
            style = inherited
        if tag in _CONTAINER_TAGS:
            counter[0] += 1
            container = f"html:b{counter[0]}"
        why = style.marker()
        # A COMMENT (or a processing instruction) has a non-string tag, and
        # `text_content()` does not return its body — so neither does this
        # walk. Reporting it would put characters the note never carries
        # between two halves of an admitted chunk and read as a shortfall
        # that is not one (measured: 55 of 145 corpus HTML files, 2026-09-03).
        # Its TAIL is ordinary parent text and is handled below.
        text = normalise(el.text or "") if isinstance(el.tag, str) else ""
        if text and seen is not None:
            seen(text)
        if why and text:
            runs.append(run_record(why, container, "html:document", text))
        for child in el:
            walk(child, style, container)
            tail = normalise(child.tail or "")
            if tail and seen is not None:
                seen(tail)
            if why and tail:
                runs.append(run_record(why, container, "html:document", tail))

    walk(doc, _Style(), "html:b0")
    return runs


class _ConcealedHtmlParser(html.parser.HTMLParser):
    """The stdlib mirror of :func:`html_runs_lxml`.

    ``handlers/html.py`` falls back to ``html.parser`` whenever lxml is
    missing or raised, and that fallback is production-adequate by ADR-0003
    Appendix B — so it needs the same detection, not a comment saying lxml
    covers it. ``email.py``'s html-only body has no lxml path at all and uses
    this one directly.
    """

    #: The tags both stdlib RENDERERS drop — ``html.py:_TextExtractor`` and
    #: ``email.py:_HtmlStripper``. This walk skips the identical set so its
    #: coverage report is over exactly the text they admit: reporting text
    #: from an ``<svg>`` neither of them extracts would put characters between
    #: two halves of an admitted chunk and read as a shortfall that is not one.
    _SKIP = frozenset({"script", "style", "noscript", "svg", "canvas",
                       "iframe", "object", "embed"})

    def __init__(self, sheet: dict[str, str] | None = None,
                 seen: Callable[[str], None] | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.runs: list[dict[str, Any]] = []
        self._stack: list[tuple[str, _Style, str]] = [("", _Style(), "html:b0")]
        self._skip = 0
        self._n = 0
        self._sheet = sheet or {}
        self._seen = seen

    def handle_starttag(self, tag: str, attrs: list) -> None:
        tag = tag.lower()
        if tag in self._SKIP:
            self._skip += 1
            return
        if tag in _VOID_TAGS:
            return
        _, parent, container = self._stack[-1]
        try:
            style = parent.resolve(dict(attrs).get("style"), tag,
                                   dict(attrs), self._sheet)
        except Exception:
            note_degraded()
            style = parent
        if tag in _CONTAINER_TAGS:
            self._n += 1
            container = f"html:b{self._n}"
        self._stack.append((tag, style, container))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
            return
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        _, style, container = self._stack[-1]
        why = style.marker()
        text = normalise(data)
        if text and self._seen is not None:
            self._seen(text)
        if why and text:
            self.runs.append(run_record(why, container, "html:document", text))


def html_runs_stdlib(raw_html: str,
                     seen: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
    parser = _ConcealedHtmlParser(stylesheet(raw_html), seen)
    parser.feed(raw_html)
    return parser.runs
