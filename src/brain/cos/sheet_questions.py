"""The owner-interview panel on the morning sheet (INT-01).

HTML for the questions the vault asks the owner, rendered beside the verdict
marks so answering costs one click per question and rides the same Save. The
wording lives here, beside the glossary, because `sheet_render` sits against
the 500-LOC bound. State comes from `interview.sheet_block`; the browser
writes `answer`/`note` back into the same rows (`sheet-template.html`).
"""
from __future__ import annotations

import html
import re
from typing import Any

SHAPE_WORDS = {
    "tension": "A decision with newer sources against it",
    "decision": "A source that reads like a decision",
    "late": "A commitment past its date",
    "orphan": "A source nothing cites",
    "stale": "A central note that may be out of date",
}

INTRO = ("These are the things last night's folds could not settle on their "
         "own. Pick one answer per question — a word in the box helps but is "
         "never required — then press <b>Save my marks</b> at the bottom: the "
         "answers travel in the same file as your marks and are applied "
         "tonight. Skip is a real answer; the vault will not ask this again "
         "for a month.")


def _evidence(row: dict[str, Any]) -> str:
    bits = []
    for e in row.get("evidence") or []:
        title = html.escape(str(e.get("title") or ""))
        bits.append(f"<code>{html.escape(str(e['id']))}</code> "
                    f"({html.escape(str(e.get('date') or '?'))})"
                    + (f" {title}" if title and title != html.escape(str(e['id'])) else ""))
    return " · ".join(bits)


_WIKILINK_RE = re.compile(r"\[\[([^\]]{1,200})\]\]")


def _wikilinks(escaped: str) -> str:
    """`[[id]]` reads as a bare id on the page; the brackets are the vault's
    link syntax, not something the owner types."""
    return _WIKILINK_RE.sub(r"<code>\1</code>", escaped)


def _question(row: dict[str, Any]) -> str:
    key = html.escape(str(row["key"]), quote=True)
    opts = "".join(
        f'<label class="opt"><input type="radio" name="q-{key}" data-answer '
        f'value="{html.escape(str(o["action"]), quote=True)}"> '
        f'{html.escape(str(o["label"]))}</label>'
        for o in row["options"])
    return (
        f'<article class="question" data-qkey="{key}">'
        f'<div class="eyebrow">{html.escape(SHAPE_WORDS.get(str(row["shape"]), str(row["shape"])))}'
        f' · expires {html.escape(str(row.get("expires_on") or ""))}</div>'
        f'<p class="ask">{_wikilinks(html.escape(str(row["question"])))}</p>'
        f'<small>Evidence: {_evidence(row)}</small>'
        f'<div class="opts">{opts}</div>'
        f'<input type="text" data-note maxlength="2000" '
        f'placeholder="Anything to add — a date, a name, what changed (optional)">'
        f'<small>What your answer does: {html.escape(str(row.get("change") or ""))}</small>'
        "</article>")


def questions_block(state: dict[str, Any]) -> str:
    """The whole panel: what the last answers did, then today's questions."""
    block = state.get("questions") or {}
    rows = list(block.get("rows") or [])
    applied = list(block.get("applied") or [])
    parts = ['<section class="panel questions"><div class="section-head"><div>'
             '<div class="eyebrow">The vault asks</div>']
    if rows:
        parts.append(f"<h2>{len(rows)} question(s) only you can answer</h2>")
    else:
        parts.append("<h2>Nothing to ask today</h2>")
    parts.append("</div></div>")
    if applied:
        parts.append('<div class="applied"><small>From your last answers:</small><ul>'
                     + "".join(f"<li><small>{html.escape(str(a.get('date') or ''))} · "
                               f"{html.escape(str(a.get('line') or ''))}</small></li>"
                               for a in applied)
                     + "</ul></div>")
    if rows:
        parts.append(f'<p class="meta">{INTRO}</p>')
        if block.get("quiet"):
            parts.append('<p class="meta">Nothing was answered for a week, so the '
                         'vault is down to one question a day until you answer one.</p>')
        parts.extend(_question(r) for r in rows)
    else:
        parts.append('<p class="meta">The folds settled everything they found, or '
                     'the open questions are waiting for their answers.</p>')
    parts.append("</section>")
    return "".join(parts)


__all__ = ["SHAPE_WORDS", "INTRO", "questions_block"]
