"""Rendering for the exceptions page — the one page that carries everything
needing the owner.

Split out of ``exceptions_page.py`` on 2026-08-22 (that file had reached the
500-line ratchet bound and would have blocked its own next edit). This module
is PURE: no I/O, no clock, no vault reads. ``exceptions_page`` gathers the
data and writes the files; everything here turns that data into HTML.

**The writing rule for this file.** The reader is the vault's owner, not an
engineer. Every string a person sees must be plain English, must say what
happened rather than which subsystem produced it, and — where the owner has
something to do — must say what to do in the same card. Internal keys still
appear, because they are how a person or an assistant addresses one item, but
never as the only description of it. Two whole vocabularies were removed here:
``invariant:``/``quarantine:`` key prefixes as row titles, and the six branch
names (``sign_repair``, ``reguard``, ``extract_retry``, ``link_lane``,
``update_retry``, ``synthesis_retry``), which now render as the sentence each
one actually means. Those sentences are derived from the remedy registry in
``remediation.py``, not invented — see ``BRANCH_LABELS``.
"""

from __future__ import annotations

import secrets
from typing import Any

from . import classification as _classification
from .brief_render import _esc, _html_page, _section
from .exceptions_tray import _option_picks, _TRAY_STYLE, _tray

#: What each automatic repair branch actually does, in the owner's words.
#: Each label is derived from the branch's entry in ``remediation.py``'s remedy
#: registry — the invariant or feed key it serves — never from the branch name.
#: A branch missing here renders its raw name rather than a wrong sentence.
BRANCH_LABELS: dict[str, str] = {
    # invariant:unsigned_notes — signs only notes the audit chain already
    # covers (an interrupted host write); anything else is a tamper exception.
    "sign_repair": "Notes whose signature was lost in an interrupted save",
    # invariant:unguarded_ingests — re-runs the ENF-04 labeller.
    "reguard": "Documents that arrived without a confidentiality label",
    # quarantine:* + invariant:subfloor_families — re-offers the bytes to the
    # reader, never merges stubs.
    "extract_retry": "Documents whose text could not be read",
    # invariant:unlinked_sources — the weekly linking lane.
    "link_lane": "Sources not yet linked to any note",
    # update:available / update:failed / staging:stale.
    "update_retry": "Engine updates that did not install",
    # synthesis:failing / synthesis:stale — retried at most once a night.
    "synthesis_retry": "The weekly summary run, when it fails",
}

#: How the owner answers a waiting question. The assistant route comes first
#: on purpose: the standing ruling (2026-08-21) is that questions are walked
#: through in conversation, never handed back as a command to go and run.
_ANSWER_HELP = (
    "Pick an answer above, tick anything else you want done, then press "
    "<strong>Copy my prompt</strong> at the bottom of the page and paste it "
    "into Claude Code, Codex or Cowork.")


#: Key prefixes that name a SUBSYSTEM rather than a problem, and so may be
#: dropped from the human-readable form. Anything else keeps every part: a
#: bare ``split(":", 1)[-1]`` turned ``synthesis:failing`` into "failing",
#: which reads as a fragment and loses the only word that says WHAT failed.
_DROPPABLE_PREFIXES = ("invariant", "quarantine", "quarantine-exhausted",
                       "remediation", "remediation-thrash")


def _plain_key(key: str) -> str:
    """A raw finding key as a person can read it.

    ``invariant:unlinked_sources`` -> ``unlinked sources``;
    ``synthesis:failing`` -> ``synthesis failing`` (nothing dropped — the
    prefix IS the subject). The key itself is still shown beside the row, so
    a person can name one item to their assistant, but it is never the only
    description of it.

    A FILENAME tail is left exactly as it is. Separators are punctuation in a
    key and part of the name in a file, so the blanket replace turned
    ``scan-2026-08-14.pdf`` into ``scan 2026 08 14.pdf`` — a name the owner
    cannot find on disk, in the one row that asks them to go look at it."""
    parts = [p for p in str(key).split(":") if p]
    if len(parts) > 1 and parts[0] in _DROPPABLE_PREFIXES:
        parts = parts[1:]
    tail = parts[-1] if parts else ""
    if "." in tail and not tail.endswith("."):      # a filename, not a key
        head = " ".join(parts[:-1]).replace("_", " ").replace("-", " ").strip()
        return f"{head} {tail}".strip() or str(key)
    text = " ".join(parts).replace("_", " ").replace("-", " ").strip()
    return text or str(key)


def _question_row_full(q: dict[str, Any]) -> str:
    expiry = (q["batch"] or {}).get("ttl_expires") if q["batch"] else None
    expiry_text = (f"If you say nothing, this is decided for you on {expiry}."
                   if expiry else "This waits until you answer it.")
    opts = _option_picks(q, q["key"], f'g-{q["key"]}',
                         question_text=q["question"]
                         or _generic_summary_text(q["source"]))
    context = (f'<p class="muted-note">{_esc(q["context"])}</p>'
               if q["context"] else "")
    # An empty question text used to render an empty headline — a row saying
    # only "If you do nothing…", with nothing above it saying what.
    headline = q["question"] or _generic_summary_text(q["source"])
    return (
        f'<li><p><strong>{_esc(headline)}</strong></p>'
        f'{context}'
        f'<p>If you do nothing: <strong>{_esc(q["default"])}</strong>. '
        f'{_esc(expiry_text)}</p>'
        f'<p class="date">Pick your answer:</p>'
        f'<ul class="picks">{opts}</ul>'
        f'<p class="date">Reference: <code>{_esc(q["key"])}</code></p></li>'
    )


def _question_row_mount(q: dict[str, Any], ceiling: str, token: str) -> str:
    """The same question on the shared mount, where a tier above this vault's
    egress ceiling may not be shown. Withheld rows still say a decision is
    waiting — hiding the CONTENT must never hide the EXISTENCE."""
    batch = q["batch"]
    shown = True
    withheld = ""
    if batch is not None:
        tier = _classification.TIERS[int(batch.get("tier_rank") or 0)]
        expiry = batch.get("ttl_expires")
        if _classification.rank(tier) <= _classification.rank(ceiling):
            body = f"<strong>{_esc(q['question'])}</strong>"
        else:
            shown = False
            body = ("<strong>A decision is waiting, and it cannot be shown "
                    "here.</strong>")
            # The TIER WORD itself is not sensitive, and it is the reason the
            # row is blank — dropping it leaves the reader with a refusal and
            # no cause. Plain language is about the SENTENCE, never about
            # removing the fact that explains it.
            withheld = (f'<p class="muted-note">Its content is marked '
                        f'{_esc(tier)}, which is above what this machine may '
                        f'show. Open the page on your Mac to read it.</p>')
    else:
        body = f"<strong>{_esc(_generic_summary_text(q['source']))}</strong>"
        expiry = None
    expiry_text = (f"If you say nothing, this is decided for you on {expiry}."
                   if expiry else "This waits until you answer it.")
    # A withheld question gets no picker at all. Its text is the thing this
    # machine may not see, and a prompt built from it would carry that text
    # straight back out through the reader's clipboard.
    if shown:
        picker = (f'<p class="date">Pick your answer:</p>'
                  f'<ul class="picks">'
                  f'{_option_picks(q, token, f"g-{token}", question_text=_summary_of(q))}'
                  f'</ul>')
    else:
        picker = ('<p class="muted-note">You cannot answer this here. Open '
                  'the page on your Mac.</p>')
    return (
        f'<li><p>{body}</p>{withheld}'
        f'<p>If you do nothing: <strong>{_esc(q["default"])}</strong>. '
        f'{_esc(expiry_text)}</p>'
        f'{picker}'
        f'<p class="date">Reference: <code>{_esc(token)}</code></p></li>'
    )


def _summary_of(q: dict[str, Any]) -> str:
    """What to call this question in a copied prompt. The real text when the
    row shows it, the generic sentence when the row is a batch-less one."""
    return q["question"] if q["batch"] is not None else \
        _generic_summary_text(q["source"])


#: What a waiting question is ABOUT, for the shared-mount page where the real
#: question text is not shown. Longest-prefix wins is not needed — the sources
#: are disjoint — but order still matters for the two ``quarantine`` prefixes,
#: the exhausted one being the more specific.
_GENERIC_DESCRIPTIONS: tuple[tuple[str, str], ...] = (
    ("remediation:tamper:unsigned-note",
     "A note has no signature, and nothing records where its contents came "
     "from. Only you can decide whether to keep it."),
    ("remediation:tamper:redirected-write",
     "Something else wrote to the vault while a repair was running, so the "
     "repair stopped."),
    ("remediation:quarantine-exhausted:",
     "A document could not be read after several tries. Only you can decide "
     "what happens to it."),
    ("remediation:quarantine:",
     "A document in the drop zone could not be filed."),
    ("remediation-thrash:",
     "The same thing keeps being repaired and breaking again. A fix that "
     "never sticks is a symptom, not a repair."),
)


def _generic_summary_text(source: str) -> str:
    """The plain description of a question whose real text may not be shown
    here. Never leaks the question itself — that is the whole point of it."""
    for prefix, text in _GENERIC_DESCRIPTIONS:
        if str(source).startswith(prefix):
            return text
    return "A decision is waiting."


def _findings_section(title: str, rows: list[tuple[str, str]],
                      *, what_to_do: str) -> str:
    """One findings card. ``what_to_do`` is the card's action line and is
    always rendered when there ARE rows — a list of problems with no stated
    next step is the thing this page exists to stop."""
    if not rows:
        return _section(title, '<p class="ok">Nothing here.</p>')
    items = "".join(
        f'<li><label><input type="checkbox" class="pick" data-kind="look" '
        f'data-ref="{_esc(k)}" data-what="{_esc(_plain_key(k))} — {_esc(t)}">'
        f'<span><span class="id">{_esc(_plain_key(k))}</span> — {_esc(t)}'
        f'<br><span class="date">Reference: <code>{_esc(k)}</code></span>'
        f'</span></label></li>'
        for k, t in rows)
    return _section(
        title,
        f'<ul class="list">{items}</ul>'
        f'<p class="do">What to do: {_esc(what_to_do)}</p>')



def _healed_section(healed: dict[str, Any]) -> str:
    """The card that shows the vault working. It is rendered on EVERY page,
    including the one that says nothing needs you — that page is the normal
    case, and a normal day is exactly when the owner wants evidence the
    self-repair is alive rather than silent."""
    rows = "".join(
        f'<li><span class="id">{_esc(BRANCH_LABELS.get(name, name))}</span>'
        f'<br><span class="date">{_branch_counts(row)}</span></li>'
        for name, row in healed["branches"].items())
    total = healed["healed_last_7_days_total"]
    headline = (
        f'In the last 7 days it fixed <strong>{total}</strong> '
        f'thing{"s" if total != 1 else ""} without asking.'
        if total else
        "Nothing has needed fixing in the last 7 days.")
    return _section(
        "What the vault fixed by itself",
        f'<p>{headline} The rows below are today&rsquo;s own run.</p>'
        f'<ul class="list">{rows}</ul>'
        f'<p class="do">Nothing to do. This card is the self-repair '
        f'working.</p>')


#: A repair lane's mode, in words. ``shadow`` is the one that MUST reach the
#: page: a shadow lane works out what it would repair and then does nothing,
#: so a row reading "fixed 0" is correct AND misleading unless it also says
#: the lane is not allowed to act yet. ``live`` is the normal state and adds
#: no words. ``n/a`` means the lane did not run in this record at all.
_MODE_WORDS = {
    "shadow": "watching only, not fixing yet",
    "n/a": "did not run today",
}


def _branch_counts(row: dict[str, Any]) -> str:
    """One repair lane's numbers, or the plain sentence that says it had no
    work. "fixed 0, skipped 0, 0 left" is six words that mean "nothing
    happened" and reads like a failure."""
    healed, skipped, remaining = (
        int(row.get("healed") or 0), int(row.get("skipped") or 0),
        int(row.get("remaining") or 0))
    mode_note = _MODE_WORDS.get(str(row.get("mode") or "").strip().lower())
    if not (healed or skipped or remaining):
        parts = [mode_note] if mode_note else ["nothing needed fixing today"]
    else:
        parts = [f"fixed {healed}"]
        if skipped:
            parts.append(f"could not fix {skipped}")
        if remaining:
            parts.append(f"{remaining} still to do")
        if mode_note:
            parts.append(mode_note)
    return " &middot; ".join(_esc(p) for p in parts)


def _cost_section(cost: dict[str, Any] | None) -> str:
    if not cost:
        return _section(
            "What the repairs cost",
            '<p class="empty">No cost recorded yet. It appears once the '
            'nightly run has priced a few repairs.</p>')
    return _section("What the repairs cost", f'<p>{_esc(cost)}</p>')


def _header(data: dict[str, Any], *, full: bool, ceiling: str,
            count: int) -> str:
    vault_name = str(data.get("vault_name") or "This vault")
    if count:
        headline = (f"{count} thing{'s' if count != 1 else ''} "
                    f"need{'' if count != 1 else 's'} you")
    else:
        headline = "Nothing needs you"
    where = ("You are reading the full page on your own Mac."
             if full else
             "You are reading the shared copy. Anything above this "
             f"machine&rsquo;s {_esc(ceiling)} limit is hidden here and "
             "readable on the host Mac.")
    return (
        '<header class="brief-header">'
        f'<h1>{_esc(vault_name)} &mdash; {_esc(headline)}</h1>'
        f'<p class="meta">Checked {_esc(data["today"])}. '
        f'This page is rewritten every night. {where}</p></header>')


def _feed_warning(findings: dict[str, Any]) -> str:
    """A missing or stale feed is itself a finding. Silence must never read as
    'nothing to report' — the same rule ``alerts.py`` applies to this feed."""
    if findings.get("missing"):
        return ('<p class="warn">&#9888; The nightly run has not left its '
                'findings file. The two cards below are UNKNOWN right now, '
                'not clean.</p>')
    if findings.get("stale"):
        return (f'<p class="warn">&#9888; The findings file was last written '
                f'{_esc(findings["stale"])}. The two cards below may be out '
                f'of date.</p>')
    return ""


def _empty_page(data: dict[str, Any], *, full: bool, ceiling: str) -> str:
    """Nothing needs the owner. Still show what the vault repaired — see
    ``_healed_section``."""
    body = (
        _header(data, full=full, ceiling=ceiling, count=0)
        + _section(
            "Nothing needs you",
            '<p class="ok">The vault checked itself and fixed what it could. '
            'No decision is waiting, and nothing is stuck.</p>')
        + _healed_section(data["healed"])
        + _cost_section(data["cost_trend"]))
    return _html_page(title="Brain — nothing needs you", accent="#059669",
                      body=body)


def render_page(
    data: dict[str, Any], *, full: bool, ceiling: str = "",
) -> tuple[str, dict[str, str]]:
    """One HTML page. Returns ``(html, token_map)`` — ``token_map`` is empty
    when ``full=True`` (the real key is shown directly; there is nothing to
    map). On the mount page every question id is a freshly-minted OPAQUE
    token; the map is host-only and thrown away between renders — nothing
    outside this one render needs a token to stay stable."""
    questions = data["questions"]
    findings = data["findings"]
    feed_issue = findings.get("missing") or findings.get("stale")
    dead = findings.get("dead_automation") or []
    untriaged = findings.get("untriaged") or []
    other = findings.get("other") or []
    if not questions and not feed_issue and not (dead or untriaged or other):
        return _empty_page(data, full=full, ceiling=ceiling), {}

    token_map: dict[str, str] = {}
    rows = []
    for q in questions:
        if full:
            rows.append(_question_row_full(q))
        else:
            token = secrets.token_hex(6)
            token_map[token] = q["key"]
            rows.append(_question_row_mount(q, ceiling, token))
    if rows:
        q_html = (f'<ul class="list">{"".join(rows)}</ul>'
                  f'<p class="do">{_ANSWER_HELP}</p>')
    else:
        q_html = '<p class="ok">No decision is waiting.</p>'

    count = len(questions) + len(dead) + len(untriaged) + len(other)
    body = (
        _header(data, full=full, ceiling=ceiling, count=count)
        + _feed_warning(findings)
        + _section("Decisions waiting for you", q_html)
        + _findings_section(
            "Automatic repairs that stopped", dead,
            what_to_do="Ask your assistant to look at these. They stopped on "
                       "their own and will not restart by themselves.")
        + _findings_section(
            "Problems nobody has looked at yet", untriaged,
            what_to_do="Ask your assistant to triage these. Each one is "
                       "either fixable automatically or a decision for you.")
        + _findings_section(
            "Other things flagged", other,
            what_to_do="Ask your assistant what these are. They did not fit "
                       "the categories above.")
        + _healed_section(data["healed"])
        + _cost_section(data["cost_trend"])
        + _tray(str(data.get("vault_name") or "this vault"),
                str(data["today"]), full=full)
        + f"<style>{_TRAY_STYLE}</style>")
    accent = "#b45309" if questions else "#2563eb"
    return _html_page(title="Brain — what needs you", accent=accent,
                      body=body), token_map
