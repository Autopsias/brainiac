"""Render the COS morning sheet's state as one self-contained HTML page.

Split out of `sheet.py` (2026-08-27). That module sat exactly at its 500-line
bound; SHEET-01's two owner-facing extensions — the per-thread date, category,
reason and file list, and the category legend — took it to 707 lines and
`render_html` to 125. The split follows the seam the file already had:
`sheet.py` BUILDS the state out of records other legs wrote, and this module
turns that state into a page. Nothing here reads a ledger, and nothing there
emits a tag.

`sheet.py` re-exports `render_html`, so every caller and test is unchanged.
"""

from __future__ import annotations

import html
import json
from importlib import resources
from typing import Any

from .feedback import SHEET_STATE_ELEMENT_ID, thread_digest
from .feedback_sheet import validate_sheet_state
from .sheet_glossary import COLUMN_MEANS, capture_html, glossary_block
from .sheet_marks import DRAFT_VALUES, JUDGMENT_VALUES, LABEL_RIGHT
from .sheet_questions import questions_block

#: WHAT THE PAGE SHOWS where the capture record has no subject. It lives here
#: rather than in `sheet.py` because it is display text, and `sheet.py` imports
#: it back for the same reason its state carries it — one string, one home.
UNAVAILABLE_SUBJECT = "Subject unavailable — capture record incomplete"


def safe_json(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


VERDICT_MEANS = (
    ("right", "Right", "good call — leave the behaviour as it is"),
    ("wrong", "Wrong", "never touch this thread again"),
    ("missed", "Missed", "you should have done something here"),
)

#: The three questions, in the order they are asked, with what each one DOES.
#: One entry per column of `sheet_marks.COLUMNS`; the how-to panel and the
#: controls are both built from this, so the page cannot explain a control it
#: does not render.

#: What each DRAFT answer means in the owner's words.
DRAFT_MEANS = {
    "send-as-is": "send as it stands",
    "not-needed": "no draft was needed",
    "tone": "needs my tone",
    "facts": "gets facts wrong",
    "too-long": "too long",
    "missing-point": "misses the main ask",
}

#: The UNSET option's label. It is the SELECTED one on every fresh sheet and it
#: says so out loud, because a control whose first option looks like an answer
#: manufactures the agreement this whole page exists to avoid counting.
UNSET_LABEL = "— not answered —"


def _option(value: str, label: str, selected: str) -> str:
    picked = " selected" if value == selected else ""
    return (f'<option value="{html.escape(value, quote=True)}"{picked}>'
            f"{html.escape(label)}</option>")


def _judgment_options(current: str) -> str:
    body = [_option("", UNSET_LABEL, current),
            _option("right", "Right as it is", current)]
    for value in JUDGMENT_VALUES[1:]:
        bucket, tier = value.split(":")
        body.append(_option(value, f"Should be {bucket} · {tier}", current))
    return "".join(body)


def _label_options(current: str, vocabulary: list[str]) -> str:
    body = [_option("", UNSET_LABEL, current),
            _option(LABEL_RIGHT, "Right as it is", current)]
    body += [_option(name, f"Should be {name}", current)
             for name in vocabulary]
    return "".join(body)


def _draft_options(current: str) -> str:
    return "".join(
        [_option("", UNSET_LABEL, current)]
        + [_option(value, DRAFT_MEANS.get(value, value), current)
           for value in DRAFT_VALUES])


def _select(column: str, title: str, options: str) -> str:
    return (f'<label class="field">{title}'
            f'<select data-col="{column}">{options}</select></label>'
            f'<label class="field rulebox" data-rule-for="{column}" hidden>'
            "Standing rule this proposes — edit it, or clear the box to "
            f'propose none<textarea data-rule-text rows="2"></textarea>'
            "</label>")


def _marks_block(row: dict[str, Any], vocabulary: list[str]) -> str:
    """The three controls, plus a draft column only when there IS a draft.

    A draft question on a thread nothing was written for has no honest answer,
    and rendering it would put an unanswerable control on 100 of 118 rows. Its
    absence leaves the column UNSET, which is exactly what it is.
    """
    parts = [_select("judgment", "Judgment",
                     _judgment_options(str(row.get("judgment") or ""))),
             _select("label", "Label",
                     _label_options(str(row.get("label") or ""), vocabulary))]
    draft_text = str(row.get("draft_text") or "")
    if draft_text or row["action_taken"] == "drafted":
        parts.append(_select("draft", "Draft",
                             _draft_options(str(row.get("draft_mark") or ""))))
    return "".join(parts)


def _draft_block(row: dict[str, Any]) -> str:
    """THE REPLY ITSELF, shown and editable.

    The owner was asked whether a draft was right without being shown it. When
    the drafting leg wrote one and its text did not survive to the sheet, the
    row SAYS the text is missing rather than rendering an empty box that reads
    as an empty draft.
    """
    text = str(row.get("draft_text") or "")
    if not text:
        if row["action_taken"] != "drafted":
            return ""
        return ('<p class="field">A reply was drafted, but its text is not in '
                "this run's drafting record — open it in Outlook to read "
                "it.</p>")
    # SIZED TO THE DRAFT, in the HTML, because a saved copy is read with its
    # scripts stripped and a textarea does not grow on its own. Measured on the
    # 2026-09-04 sheet: 25 drafts of 8-15 lines each, every one of them inside
    # a `rows="6"` box — the owner was being asked to judge a reply he could
    # only read a third of at a time. The cap keeps one runaway draft from
    # pushing every other row off the page; the box still scrolls past it.
    rows = min(max(len(text.splitlines()) + 1, 6), 24)
    return ('<label class="field">The reply the porter wrote — edit it here '
            'and the edit is filed with your mark<textarea data-draft-text '
            f'rows="{rows}">{html.escape(text)}</textarea></label>')


def _received_label(value: str) -> str:
    """`2026-08-13T12:10:11+01:00` is a timestamp; `13 Aug 2026` is a date.

    Sliced, never parsed: the ledger stamp is already ISO and a parse would
    add a failure mode for a label. An unexpected shape falls through to the
    raw value rather than to a wrong date.
    """
    head = value[:10]
    if len(head) == 10 and head[4] == "-" and head[7] == "-":
        months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
        try:
            return f"{int(head[8:10])} {months[int(head[5:7]) - 1]} {head[:4]}"
        except (ValueError, IndexError):
            return value
    return value


def _thread_row(row: dict[str, Any], vocabulary: list[str]) -> str:
    cid = html.escape(str(row["conversation_id"]), quote=True)
    subject = html.escape(str(row["subject"]), quote=True)
    flags = "".join(
        f'<span class="flag {kind}">{label}</span>'
        for kind, label in (
            ("sample", "held-out sample") if row["held_out"] else ("", ""),
            ("stale", "stale") if row["stale_archived"] else ("", ""),
        )
        if kind
    )
    received = str(row.get("received") or "")
    date_bit = (
        f'<span class="when">{html.escape(_received_label(received))}</span>'
        if received
        else '<span class="when">date not recorded</span>'
    )
    category = str(row.get("category") or "")
    cat_bit = (
        f'<span class="cat">{html.escape(category)}</span>' if category else ""
    )
    tier = str(row.get("tier") or "")
    tier_bit = f'<span class="tier">{html.escape(tier)}</span>' if tier else ""
    files = [str(name) for name in (row.get("files") or [])]
    files_bit = (
        '<div class="files"><span class="field">Files on this thread</span>'
        + "".join(f"<code>{html.escape(name)}</code>" for name in files)
        + "</div>"
        if files
        else ""
    )
    capture_bit = capture_html(row.get("capture") or {})
    reason = html.escape(str(row.get("reason") or ""))
    # THE PORTER'S OWN VERDICT AS TEXT, never as a pre-selected control. This
    # is the `suggestion` half of the record/question/suggestion/response shape
    # Argilla and Label Studio both settled on, and keeping it textual is what
    # lets an unmarked row stay UNKNOWN instead of counting as agreement.
    said = html.escape(
        f"The porter judged this {tier or 'no tier'}"
        + (f", category {category}" if category else "")
        + f", and {str(row['action_taken'])} it.")
    off = "" if row.get("shown") else " off"
    return (
        f'<article class="thread{off}" data-cid="{cid}">'
        f'<div class="thread-main">'
        f'<div class="subject">{subject}</div><div class="meta">{date_bit}'
        f'<span class="action">{html.escape(str(row["action_taken"]))}</span>'
        f"{cat_bit}{tier_bit}{flags}"
        f"<code>{html.escape(str(row['conversation_id_digest']))}</code></div>"
        f'<p class="why">{reason}</p><p class="said">{said}</p>'
        f"{capture_bit}{files_bit}"
        "</div>"
        f'<div class="marks">{_marks_block(row, vocabulary)}</div>'
        f'<div class="extras">{_draft_block(row)}'
        # THE SECOND COPY (2026-09-07): the how-to block carries the long
        # version, this is the per-row label. Fixing only the block would have
        # left "the judge never reads it" on all 109 rows.
        '<label class="field">Note — the judge reads this, and so do you on a '
        'later sheet.<textarea data-note rows="2" '
        'placeholder="Why this call was wrong, in your words."></textarea>'
        "</label></div></article>"
    )


def _list_rows(state: dict[str, Any], ids: list[str], empty: str) -> str:
    by_id = {row["conversation_id"]: row for row in state["threads"]}
    if not ids:
        return f'<p class="empty">{html.escape(empty)}</p>'
    return (
        "<ul>"
        + "".join(
            f"<li><strong>{html.escape(str(by_id.get(cid, {}).get('subject') or UNAVAILABLE_SUBJECT))}"
            f"</strong><br><code>{html.escape(thread_digest(cid))}</code></li>"
            for cid in ids
        )
        + "</ul>"
    )


def _counts_block(state: dict[str, Any]) -> str:
    """The five headline numbers, in the order the morning reads them."""
    return "".join(
        f'<div class="stat"><b>{int(state["counts"].get(key, 0))}</b>'
        f"<span>{label}</span></div>"
        for key, label in (
            ("archived", "Archived"),
            ("ingested", "Ingested"),
            ("drafted", "Drafted"),
            ("held", "Held"),
            ("stale_archived", "Stale"),
        )
    )


def _rulings_block(state: dict[str, Any]) -> str:
    """The live standing rules, each with its counters and its revoke control.

    `fired_count` and `contradicted_count` are on the row because a rule
    nothing can retire keeps driving the judge long after the owner stopped
    meaning it, and he cannot decide to retire one without seeing how often it
    has been in force and how often a later mark disagreed with it. `in_prompt`
    is on the row for the opposite reason: every live rule is listed so each
    keeps its revoke control, and revoking one believing it is in force
    tonight — when the cap left it out — is a wasted decision.

    ORDER: a rule with a POSITIVE NET (confirmed more than contradicted) sorts
    first. The list is what the owner reads top-down, and the rules worth
    keeping are the ones the evidence has kept agreeing with.
    """
    rows = sorted(
        state["standing_rulings"],
        key=lambda r: (int(r.get("confirmations") or 0)
                       - int(r.get("contradicted_count") or 0),
                       int(r.get("fired_count") or 0)),
        reverse=True)
    return (
        "".join(
            f'<article class="ruling" data-rule-key='
            f'"{html.escape(str(r["rule_key"]), quote=True)}">'
            f"<div><strong>{html.escape(str(r['rule']))}</strong><small>"
            f"{int(r['confirmations'])} confirmation(s) · fired on "
            f"{int(r.get('fired_count') or 0)} thread(s) · contradicted "
            f"{int(r.get('contradicted_count') or 0)} time(s) · "
            + ("in tonight&rsquo;s prompt" if r.get("in_prompt")
               else "NOT in tonight&rsquo;s prompt — the cap left it out")
            + f" · expires {html.escape(str(r['expires_at']))}</small></div>"
            '<label><input type="checkbox" data-revoke> Revoke</label>'
            "</article>"
            for r in rows
        )
        or '<p class="empty">No live rulings.</p>'
    )


def _overturned_block(state: dict[str, Any]) -> str:
    """What the owner overturned on the LAST sheet, shown back to him."""
    rows = state["overturned_last_time"]
    if not rows:
        return '<p class="empty">None.</p>'
    return (
        "<ul>"
        + "".join(
            f"<li><b>{html.escape(str(r.get('verdict')))}</b> · "
            f"{html.escape(str(r.get('action_taken')))} · "
            f"{html.escape(str(r.get('note') or 'No note'))}</li>"
            for r in rows
        )
        + "</ul>"
    )


def _door_label(door: dict[str, Any]) -> str:
    """The door verdict as the owner reads it.

    The batch ledger's machine verdict is hyphenated, while its named
    owner-facing status is the exact phrase the morning must preserve. The
    frozen state value stays untouched; only the display label is translated.
    """
    verdict = str(door.get("verdict"))
    return ("skipped: not signed in"
            if verdict == "skipped-not-signed-in" else verdict)


def _howto_block() -> str:
    """What each control on the page DOES.

    Built from the same constants the controls are built from, so the
    explanation and the control can never disagree.
    """
    return (
        '<div class="howto">'
        + "".join(
            f"<div><b>{label}</b><span>{html.escape(hint)}.</span></div>"
            for _column, label, hint in COLUMN_MEANS
        )
        + '<div><b>Leaving one blank</b><span>Means you have not answered that '
        "question. It is not agreement and it is never counted as one — only "
        "the answers you give are saved.</span></div>"
        '<div><b>Standing rule</b><span>Picking a &ldquo;should be&rdquo; '
        "offers a rule in the box under it. That box is the one thing the "
        "judge reads: it is quoted into every future night&rsquo;s "
        "instructions, applies to all mail rather than this thread, and "
        "expires after 90 days unless it keeps being confirmed. Clear the box "
        "to correct this thread without making a rule.</span></div>"
        # THE JUDGE READS THIS NOW (owner ruling 2026-09-07): the owner's
        # free text was the one thing on the sheet being discarded.
        '<div><b>Note</b><span>Say it in your own words. The judge reads '
        "this on every later night alongside your ruling on the thread, and "
        "is told to follow it where it applies. It is also shown back to you "
        "under &ldquo;Overturned last time&rdquo;. Use the rule box above "
        "instead when the correction should apply to <i>all</i> mail rather "
        "than this thread.</span></div>"
        '<div><b>Save my marks</b><span>Writes a small file to your '
        "Downloads with only the rows you answered. Nothing leaves this page "
        "until you press it, and nothing is sent anywhere — the file is read "
        "back into the record on this Mac.</span></div>"
        "</div>"
    )


def _effect_block(state: dict[str, Any]) -> str:
    """WHAT YOUR LAST MARKS CHANGED — the first thing on the page.

    `sentence` carries its own denominator in words and is built by
    `sheet_select.effect_block`, which is also the one place that decides what
    a sheet with no marks before it says. Rendering the sentence rather than
    re-deriving numbers here is deliberate: two producers of the same claim is
    how a page ends up printing a bare number nobody can scope.
    """
    effect = state["effect"]
    return (f'<p class="lede">{html.escape(str(effect["sentence"]))}</p>'
            + ('<p class="meta">Changed here means the porter DID something '
               "different, not that it did so because of your mark — a thread "
               "can change for its own reasons.</p>"
               if effect["marks"] else ""))


def _selection_block(state: dict[str, Any]) -> str:
    """How many rows you are being asked to look at, and why those ones."""
    selection = state["selection"]
    names = {"draft": "a reply was drafted", "held_out": "held-out sample",
             "loud_archive": "archived at P0/P1",
             "outlook": "you moved it in Outlook"}
    # `already_marked` is a DROP reason, not an ask: listing it beside the asks
    # made the header sum to 19 over a "15" (sheet 2026-09-08).
    reasons = ", ".join(
        f"{int(count)} {names.get(key, key)}"
        for key, count in sorted(selection["reasons"].items())
        if int(count) and key != "already_marked")
    already = int(selection["reasons"].get("already_marked") or 0)
    hidden = int(selection["total"]) - int(selection["shown"])
    return (
        f'<p class="lede">You are being asked to look at '
        f'<b>{int(selection["shown"])}</b> of tonight&rsquo;s '
        f'{int(selection["total"])} threads'
        + (f" — {html.escape(reasons)}." if reasons else ".")
        + (f" {already} you already marked on an earlier sheet are not asked "
           "again." if already else "")
        + "</p>"
        + (f'<p class="meta">The other {hidden} are on this page too, behind '
           "the toggle below. Marking one of them counts exactly the same.</p>"
           if hidden > 0 else
           '<p class="meta">Every thread tonight is one of them.</p>')
    )


def _categories_block(state: dict[str, Any]) -> str:
    """What the category words mean, for the categories on THIS page.

    The owner asked what they mean and whether he is judging them. The lede
    answers the second; the legend answers the first. An empty legend renders
    nothing rather than an invented definition.
    """
    legend = state.get("category_legend") or []
    if not legend:
        return ""
    return (
        '<p class="lede legend-lede">The grey chip on each row is its '
        "<b>category</b> — your own rule from "
        "<code>overlay/cos/ingest.md</code>, which is what decided whether the "
        "thread was recorded. It is context. You are not marking it right or "
        'wrong.</p><div class="howto">'
        + "".join(
            f'<div><b>{html.escape(entry["category"])}</b>'
            f'<span>{html.escape(entry["means"])}.</span></div>'
            for entry in legend
        )
        + "</div>"
    )


def render_html(state: dict[str, Any]) -> str:
    """Render a complete mailed/read-only page with capability-gated controls."""
    state = validate_sheet_state(state)
    template = (resources.files("brain") / "_assets/cos/sheet-template.html").read_text(
        encoding="utf-8"
    )
    door = state["door_check"]
    stop = state["batch_stop"]
    replacements = {
        "{{CATEGORIES}}": _categories_block(state),
        "{{HOWTO}}": _howto_block() + glossary_block(),
        "{{DATE}}": html.escape(str(state["date"])),
        "{{GENERATED_AT}}": html.escape(str(state["generated_at"])),
        "{{RUN_IDS}}": html.escape(", ".join(state["run_ids"]) or "No run opened"),
        "{{COUNTS}}": _counts_block(state),
        "{{DOOR}}": html.escape(_door_label(door)),
        "{{DOOR_DETAIL}}": html.escape(
            f"{door.get('lane', 'lane unknown')} · {door.get('toolset', 'toolset unknown')}"
        ),
        "{{STOP}}": html.escape(str(stop.get("reason"))),
        "{{STOP_DETAIL}}": html.escape(
            f"{int(stop.get('unreconciled_threads') or 0)} unreconciled thread(s)"
        ),
        "{{RULINGS}}": _rulings_block(state),
        "{{QUESTIONS}}": questions_block(state),
        "{{HELD_OUT}}": _list_rows(
            state,
            state["held_out"]["conversation_ids"],
            "No archived-not-drafted population.",
        ),
        "{{HELD_OUT_META}}": html.escape(
            f"Uniform k={state['held_out']['k']} sample from "
            f"{state['held_out']['population']} archived-not-drafted threads."
        ),
        "{{STALE}}": _list_rows(state, state["stale_archived"], "None."),
        "{{OVERTURNED}}": _overturned_block(state),
        "{{THREADS}}": "".join(
            _thread_row(row, state["label_vocabulary"])
            for row in state["threads"]),
        "{{THREAD_COUNT}}": str(int(state["selection"]["shown"])),
        "{{HIDDEN_COUNT}}": str(len(state["threads"])
                                - int(state["selection"]["shown"])),
        "{{EFFECT}}": _effect_block(state),
        "{{SELECTION}}": _selection_block(state),
        "{{SHEET_ID}}": html.escape(str(state["sheet_id"])),
        "{{MARKS_FILENAME}}": html.escape(f"cos-marks-{state['date']}.json"),
        "{{STATE_ID}}": SHEET_STATE_ELEMENT_ID,
        "{{STATE_JSON}}": safe_json(state),
    }
    for needle, value in replacements.items():
        template = template.replace(needle, value)
    if "{{" in template:
        raise ValueError("sheet template has an unreplaced placeholder")
    return template
