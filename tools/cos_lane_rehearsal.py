#!/usr/bin/env python3
"""Daytime, read-only rehearsal of the body-open leg on the elected lane.

WHY THIS EXISTS (measured, run 103, 2026-08-09). The night's first body open
returned success while the reading pane stayed on the previously-opened
conversation. The identity guard fired, correctly stopped every mutation leg,
and the whole run was spent discovering a lane regression — the same shape as
runs 73, 75 and 101. Each of those cost a NIGHT to learn one thing: does an
open on this lane land on the row it aimed at?

That question is answerable in ninety seconds, in the afternoon, without
spending a night. This tool asks exactly it and nothing else.

    python3 tools/cos_lane_rehearsal.py --rows 5
    python3 tools/cos_lane_rehearsal.py --deep-link --rows 20   # the new primitive

THE DEEP-LINK PRIMITIVE (v5.55). Clicking a virtualized row is the defect
itself: the row is verified, then the list re-uses that DOM node for another
conversation before the click lands. `--deep-link` opens by NAVIGATING to the
conversation's own URL — derived from its `conversation_id`, not captured — so
there is no node to recycle and landing on the wrong conversation stops being
possible by construction. The CLICK path stays, as the one bounded re-target.

Under navigation the reading-pane URL is the input we supplied, not evidence
the app produced, so it cannot be the whole assert on its own: an open counts
as `landed` only when the URL agrees AND OWA's own list names that same single
conversation as its selected row. URL-only agreement is reported as
`unconfirmed` and never promotes.

THE OPEN IS WAITED FOR, NOT SLEPT ON (v5.56). A navigation is a full page load
and a fixed sleep answers nothing about it — too short on a slow morning (one
open in two mismatched at the old 3.0s default, measured 2026-08-09, the assert
reading mid-route), wasted on a fast one. `--settle` is now the readiness
TIMEOUT: the tool polls until the document has finished loading, the URL carries
the intended conversation, OWA marks that same conversation selected, and then
until the reading pane STOPS GROWING — measured on the same mailbox, identity
holds at 1.54s with 28 characters of body and the text is still arriving at
4.32s, so returning at the first `ready` would report landed opens with empty
bodies. Expiry changes no outcome — it stamps `ready_timed_out` (identity never
held) or `body_settle_timed_out` (it did, the text never settled), and the
record reads exactly as it would have.

A FRESH TAB HAS NO FOLDER SEGMENT, AND THAT IS OWA'S DESIGN (v5.61). A run
opens its OWN tab, and a tab opened at `<origin>/mail/` sits on the INBOX with
no `/mail/<folder>` segment to derive a conversation URL from — so every
deep-link open used to fail closed before the first row. Measured 2026-08-10:
`location.href = '<origin>/mail/inbox'` does not navigate AT ALL (14s, URL
unmoved, `readyState` never leaving `complete`), and selecting Inbox in the
folder tree moves the URL BACK to `<origin>/mail/` — the default folder's list
route carries no segment, by design, while `Notes` gets `<origin>/mail/notes`.
The segment lives in the ITEM route, so the tool makes the APP produce one: ONE
click on an already-read row yields `<origin>/mail/inbox/id/<encoded id>` in
under a second, and the base is read off what the app wrote. The folder is
still never GUESSED — see `acquire_base`.

AN ABSENT ROW IS NOT A NEGATIVE ANSWER (v5.57). OWA re-renders about a dozen
rows after the navigation and is not guaranteed to include the conversation it
just opened — measured twice on 2026-08-09, the same conversation both times:
URL agreeing, 536 characters of body, and the opened row simply not in the list,
so every rendered row read `aria-selected="false"`. The corroborating half of
the assert had no row to read. The tool now SCROLLS that conversation back into
view (bounded, reusing the sample collector's scroll) and reads the assert off
the row itself. The assert is NOT relaxed: a row that renders and is NOT marked
selected is a genuine mismatch and still fails; a conversation that never
renders inside the bound is still `unconfirmed` with nothing extracted. Which
path corroborated each open is recorded (`corroborated_via`, `recovery_steps`)
so a rising recovery rate is visible instead of absorbed.

IT SCROLLS FOR ITS SAMPLE (v5.56). Only about a dozen rows render at a time, so
`--rows 20` used to measure 12 and print CLEAN. It now scrolls the list until it
has the rows it was asked for, and when it cannot, the VERDICT says
`SHORT SAMPLE` and exits 2 — a pass measured over fewer rows than requested is
a false all-clear, not a pass.

THE CHROME LANE IS THE ONE IT DRIVES ITSELF. The other lane, Codex's in-app
browser (`iab`), lives inside the Codex app: no AppleScript handle, no CDP
port, nothing a host process can reach. So that lane's rehearsal is a PROMPT
(`tools/cos_lane_rehearsal_iab.md`) executed by Codex, and this file is where
its shared parts live so the two rehearsals cannot drift apart:

    --emit-js {list,locate,pane}   the exact JS to evaluate, ready to paste
    --select  <list.json>          the fail-closed read-state screen
    --score   <attempts.json>      the same summary, verdict and exit codes

It opens N ALREADY-READ conversations by stable `data-convid`, asserts the
reading pane's identity after each open (`target_intended` vs
`target_produced`, plus `target_produced_pre` so "never moved" and "moved to
the wrong row" stay distinguishable), and allows the ONE bounded re-target
doctrine allows — at a DIFFERENT click point, because a retry that repeats the
attempt that just failed is not a retry.

WHAT IT REFUSES TO DO, structurally:
  * It takes no run id, stamps no manifest, writes no `cos-ops/` ledger and
    appends to no corpus. Its output is a standalone report.
  * It archives, categorizes, drafts, sends and deletes NOTHING.
  * **It never opens an UNREAD row.** Opening one marks it read, and
    `unread-touch` is a Layer-2 hard deny in both directions. The read-state
    screen is FAIL CLOSED: a row whose read state cannot be determined from the
    list is SKIPPED, and if no read-state affordance is found anywhere the tool
    opens nothing at all and says so. Run `--probe` to see what it can read
    without opening anything.

An open is not free — it puts a conversation in the reading pane and takes the
owner's screen while it runs — so keep N small. Five rows is enough to tell a
landing lane from a starved one.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import quote, unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_hold_visible import (                                     # noqa: E402
    HOST, JsUnavailable, OsaUnavailable, _assert_visible, _front_window_id,
    _frontmost_app, _pick, _restore, assert_visible_by_id, eval_js,
    eval_js_by_id, tab_id,
)

# One evaluation returns the row's id AND its rect (E30(d)): a rect from an
# earlier evaluation addresses whatever the virtualized list has since put
# there. `point` is the SENDER LINE near the row's top edge, never the vertical
# centre — v5.50 measured every centre click in run 101 failing and every
# sender-line open after it landing.
_ROW_JS = """(() => {
  const want = %(convid)s, dx = %(dx)s;
  const rows = [...document.querySelectorAll('[role="option"][data-convid]')];
  const row = rows.find(r => r.getAttribute('data-convid') === want);
  if (!row) return JSON.stringify({ok: false, reason: 'row-not-rendered'});
  row.scrollIntoView({block: 'center', behavior: 'auto'});
  const rect = row.getBoundingClientRect();
  const id = row.getAttribute('data-convid');
  const inView = rect.top >= 0 && rect.bottom <= window.innerHeight;
  const pre = decodeURIComponent((location.href.split('/id/')[1] || '').split('?')[0]);
  const x = Math.round(rect.x + dx), y = Math.round(rect.y + 20);
  const el = document.elementFromPoint(x, y);
  if (!el) return JSON.stringify({ok: false, reason: 'no-element-at-point',
                                  id, pre, point: {x, y}, in_view: inView});
  // The point must be INSIDE the row we resolved. A lane whose click takes
  // coordinates lands on whatever occupies those pixels (E30(d)) — including
  // a neighbouring row, or nothing — so prove containment BEFORE clicking
  // rather than diagnosing it afterwards from a mismatch.
  if (el.closest('[role="option"][data-convid]') !== row)
    return JSON.stringify({ok: false, reason: 'point-outside-row',
                           id, pre, point: {x, y}, in_view: inView});
%(act)s  return JSON.stringify({ok: true, id, pre, in_view: inView, point: {x, y},
    rect: {x: rect.x, y: rect.y, w: rect.width, h: rect.height}});
})()"""

_DISPATCH = """  const opts = {bubbles: true, cancelable: true, view: window, clientX: x, clientY: y};
  for (const type of ['pointerdown', 'mousedown', 'mouseup', 'click'])
    el.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type, opts));
"""

# Chrome clicks from here (synthetic events). A lane whose click primitive is
# the HARNESS's own — `iab` — evaluates the LOCATE form instead, clicks
# `point` natively, and gets the identical rect/id/pre in one evaluation, which
# is what E30(d) actually requires.
_OPEN_JS = _ROW_JS.replace("%(act)s", _DISPATCH)
_LOCATE_JS = _ROW_JS.replace("%(act)s", "")

_PANE_JS = """JSON.stringify({produced: decodeURIComponent(
  (location.href.split('/id/')[1] || '').split('?')[0])})"""

# --- the deep-link primitive: navigate, never click a recycled node -----------
#
# The whole race this replaces lives in the click: a virtualized row is verified,
# then the list re-uses that DOM node for another conversation before the click
# lands. A navigation touches no node, so there is no node to recycle.
#
# The URL is DERIVED, never captured. Measured against every real one this
# project has recorded — 14 in `_cos_held_deep_links_…run103.json` and 20 on run
# 104's ingestion rows, 34 of 34 — the shape is exactly
# `<origin>/mail/<folder>/id/<encodeURIComponent(conversation_id)>`, and
# `_PANE_JS` above is already its exact inverse. So the run needs no captured
# link and no `deep_link_status`: it needs the conversation id it already has.
#
# The FOLDER SEGMENT is read off the tab's own current URL rather than hardcoded
# to `inbox`. Every recorded sample is an inbox link because the body pass reads
# the Inbox, but a lane parked in another folder would otherwise build a URL for
# a folder it is not in.
_NAV_JS = """(() => {
  const want = %(convid)s, here = location.href;
  const base = here.match(/^(https?:\\/\\/[^/]+\\/mail\\/[^/?#]+)/);
  if (!base) return JSON.stringify({ok: false, reason: 'not-on-a-mail-folder-url', here});
  const pre = decodeURIComponent((here.split('/id/')[1] || '').split('?')[0]);
  const url = base[1] + '/id/' + encodeURIComponent(want);
  // Sentinel: a value on `window` survives an in-app route change and dies in a
  // full document load. Reading it back after the navigation is how the report
  // says which of the two OWA actually did — the thing that decides whether the
  // list, its scroll position and every rendered row survive the open.
  window.__cosNav = (window.__cosNav || 0) + 1;
  location.href = url;
  return JSON.stringify({ok: true, id: want, pre, url, nav_from: here});
})()"""

# Go to a URL this process already computed, for the ONE case `_NAV_JS` cannot
# serve: the tab no longer carries a folder segment to derive from (v5.57).
#
# MEASURED, 2026-08-09, three live runs. Some conversations OWA simply will not
# deep-link — the navigation is answered by dropping the tab to
# `<origin>/mail/`, folder and id gone, an empty 42-character shell. From that
# moment `_NAV_JS` correctly refuses every remaining row
# (`not-on-a-mail-folder-url`, and inventing `inbox` is exactly what it must
# never do), so ONE unopenable conversation cost the other seven rows of a
# 20-row pass, twice. The folder is not unknown, though — it is the one this run
# has been reading all along, observed on the tab's OWN url. So the URL stays
# DERIVED, never composed: the base is remembered from this run's own successful
# navigations, and a row that used it says so (`nav_base: "remembered"`).
_GOTO_JS = """(() => {
  const url = %(url)s, here = location.href;
  const pre = decodeURIComponent((here.split('/id/')[1] || '').split('?')[0]);
  window.__cosNav = (window.__cosNav || 0) + 1;
  location.href = url;
  return JSON.stringify({ok: true, id: %(convid)s, pre: pre, url: url,
                         nav_from: here, nav_base: 'remembered'});
})()"""

# The tab's own `<origin>/mail/<folder>` prefix, or "" when it has none. Same
# regex as `_NAV_JS` — one definition of what a usable mail URL is.
_BASE_JS = """JSON.stringify({base: (location.href.match(
  /^(https?:\\/\\/[^/]+\\/mail\\/[^/?#]+)/) || ['', ''])[1], href: location.href})"""

# Read AFTER the navigation. `produced` alone would be near-vacuous here: under
# a CLICK the reading-pane URL is what the app produced, which is why run 73
# could use it as evidence; under a NAVIGATION it is the input we supplied, and
# a page that silently failed to load the conversation still shows the URL we
# typed. So the assert needs a SECOND, app-produced signal, and that is the row
# OWA marks selected in its own list.
#
# It also computes `ready` — the READINESS PREDICATE the poll below waits on.
# It lives HERE, in the same evaluation as the assert, so the thing that decides
# "the page has finished arriving" and the thing that decides "it arrived on the
# right conversation" can never disagree about what they are looking at.
_AFTER_JS = """(() => {
  const want = %(convid)s;
  const href = location.href;
  const produced = decodeURIComponent((href.split('/id/')[1] || '').split('?')[0]);
  const rows = [...document.querySelectorAll('[role="option"][data-convid]')];
  const sel = rows.filter(r => r.getAttribute('aria-selected') === 'true')
                  .map(r => r.getAttribute('data-convid'));
  // Exactly one selected row is a corroboration; none and several are both
  // "the app did not tell us", and neither may read as agreement.
  const selected = sel.length === 1 ? sel[0] : null;
  // Is the conversation we opened even IN this list? (v5.57.) Measured twice on
  // 2026-08-09: a navigation landed, the body rendered, and OWA re-rendered 13
  // rows that did not include the conversation it had just opened — so every
  // rendered row read `aria-selected="false"` and the corroborating half of the
  // assert had no row to read. Without these two fields, "the app says this row
  // is not selected" and "the app is not rendering this row" are one null.
  const targetRow = want ? rows.find(
    r => r.getAttribute('data-convid') === want) : null;
  const main = document.querySelector('[role="main"]') || document.body;
  const doc_complete = document.readyState === 'complete';
  return JSON.stringify({
    produced,
    selected,
    selected_count: sel.length,
    // Distinguishes "nothing is selected" from "this list has no selection
    // affordance at all" — without it a null `selected` is undiagnosable.
    selected_attr_seen: rows.some(r => r.hasAttribute('aria-selected')),
    target_rendered: !!targetRow,
    target_selected: targetRow
      ? targetRow.getAttribute('aria-selected') === 'true' : null,
    doc_complete,
    // All three, or the reading is premature: the document finished loading,
    // the URL carries the conversation we asked for, and the app's own list
    // marks that same one conversation selected. `want` empty (a caller that
    // did not name a target) can never be ready — an assert with no intended
    // id is not a thing to wait for.
    ready: doc_complete && !!want && produced === want && selected === want,
    body_chars: (main.innerText || '').length,
    rows_rendered: rows.length,
    spa_route: (window.__cosNav || 0) > 0,
    reloaded: !(window.__cosNav > 0)
  });
})()"""

# Bring further rows into a VIRTUALIZED list. OWA renders about a dozen rows at
# a time (measured 2026-08-09: 12 of 290), so a target pool read from ONE view
# can never reach the 20-row promotion bar — the tool silently measured 12 and
# reported CLEAN, which is the false-all-clear shape.
#
# ponytail: try each scrollable ancestor until one MOVES, rather than guessing
# the container from computed `overflow` — a virtualized list wraps its rows in
# spacer divs that look scrollable and are not. The caller detects the end of
# the list from stagnation (a scroll that renders no new convid), so this needs
# no end-of-list opinion of its own and works for either scroll method.
_SCROLL_JS = """(() => {
  const rows = [...document.querySelectorAll('[role="option"][data-convid]')];
  if (!rows.length) return JSON.stringify({ok: false, reason: 'no-rows'});
  const last = rows[rows.length - 1];
  let el = last.parentElement, moved = null;
  for (let i = 0; i < 12 && el; i++, el = el.parentElement) {
    if (el.scrollHeight - el.clientHeight < 8) continue;
    const before = el.scrollTop;
    el.scrollTop = before + Math.max(1, Math.round(el.clientHeight * 0.8));
    if (el.scrollTop > before) { moved = {before: before, after: el.scrollTop}; break; }
    el.scrollTop = before;                 // put back what we did not move
  }
  if (!moved) last.scrollIntoView({block: 'end', behavior: 'auto'});
  return JSON.stringify(Object.assign(
    {ok: true, method: moved ? 'scrollTop' : 'scrollIntoView'}, moved || {}));
})()"""


# Put the list back at its TOP before anything reads or searches it.
#
# WHY (measured 2026-08-09, three consecutive live runs). `collect_eligible`
# read the list from WHEREVER it happened to be parked — the previous run's last
# scroll, a stray probe, OWA restoring a position — so "the 20 rows I asked for"
# meant different conversations each run, and two of the three runs drew rows so
# deep in the folder that OWA REFUSED to deep-link them at all (it drops the tab
# to `<origin>/mail/` with no folder segment, after which every remaining row
# fail-closes on `not-on-a-mail-folder-url`). The run started from the top drew
# the folder's own top rows and opened 19 of 19. A sample that depends on hidden
# scroll state is not a sample, and neither of the two shapes above is
# diagnosable from the record when the pool itself moved.
#
# Same ancestor walk as `_SCROLL_JS` — the scrollable container is whichever
# ancestor actually MOVES — so the two cannot disagree about what the list is.
_TOP_JS = """(() => {
  const rows = [...document.querySelectorAll('[role="option"][data-convid]')];
  if (!rows.length) return JSON.stringify({ok: false, reason: 'no-rows'});
  let el = rows[0].parentElement, tried = false;
  for (let i = 0; i < 12 && el; i++, el = el.parentElement) {
    if (el.scrollHeight - el.clientHeight < 8) continue;
    const before = el.scrollTop;
    if (before === 0) continue;              // nothing to undo on this one
    tried = true;
    el.scrollTop = 0;
    if (el.scrollTop < before)
      return JSON.stringify({ok: true, before: before, after: el.scrollTop});
    el.scrollTop = before;                   // put back what we did not move
  }
  // Either nothing was scrolled away from its top, or nothing here scrolls at
  // all — both mean the list is already showing its first rows. A container we
  // TRIED and could not move is neither, and must not read as success.
  return tried ? JSON.stringify({ok: false, reason: 'could-not-scroll'})
               : JSON.stringify({ok: true, before: 0, after: 0,
                                 method: 'already-at-top'});
})()"""


def _to_top(ev) -> bool:
    """Best-effort. A list that cannot be re-anchored is still readable — it is
    only less reproducible — so this never ends a run on its own."""
    try:
        moved = json.loads(ev(_TOP_JS)).get("ok") is True
    except Exception:                                              # noqa: BLE001
        return False
    if moved:
        time.sleep(0.6)
    return moved


def acquire_base(ev, convid: str, timeout: float = 12.0) -> dict:
    """Make the APP produce a folder route on this tab, then read the base off it.

    WHY A FRESH RUN-OWNED TAB HAS NO FOLDER SEGMENT — measured on the live
    mailbox 2026-08-10, and it is OWA's design rather than a misconfigured tab:

      * A tab opened at `<origin>/mail/` IS ALREADY SHOWING THE INBOX: 13 rows
        rendered, `readyState: complete`, and the folder tree's own Inbox node
        carrying `aria-selected="true"`. The folder is known to the APP and
        missing only from the URL.
      * `location.href = '<origin>/mail/inbox'` DOES NOT NAVIGATE AT ALL — not a
        redirect to outwait, no navigation whatsoever. Polled every second for
        14s: the URL never left `<origin>/mail/`, `readyState` never left
        `complete`, the row count never moved off 13, the tree selection never
        changed. No retry reaches this and no wait outlasts it.
      * Selecting a folder IN-APP does write a segment: clicking the tree's
        `Notes` node moved the URL to `<origin>/mail/notes` in 746 ms with no
        `beforeunload` — an in-app route change, so the list survives it. **But
        clicking `Inbox` moved the URL BACK to `<origin>/mail/`.** The DEFAULT
        folder's list route carries no segment by design, so selecting the
        folder cannot produce one for the one folder a night actually reads.
      * The segment lives in the ITEM route. ONE click on an already-read row
        produced `<origin>/mail/inbox/id/<encoded id>` inside one second, and
        `deep_link(convid, base)` reproduced that URL EXACTLY — which is also
        this tenant's answer to the account-index question: no `/mail/0/…`
        segment appears here.

    So the folder is STILL never guessed — the app is made to say it, and the
    tool reads what the app said. The click is the seeding primitive precisely
    because it is the one open that needs no base, which is the bootstrap
    problem stated in one line.

    ponytail: no folder-name table, no tree-label→segment mapping. Both would be
    a guess wearing a lookup's clothes, and a custom folder or a non-English UI
    would break them silently.

    (v5.62) AND THE SEEDING CLICK HAS TO REACH ITS ROW TOO — the same defect the
    re-target had, one caller over, and it bit the moment a rehearsal targeted
    DEEP conversations. Collecting a pool for `--convids` scans the folder to
    exhaustion, so the list ends up parked at the BOTTOM while the spare
    proven-read row this seeds with comes from the TOP: `row-not-rendered`, and
    the whole run refused with `could-not-acquire-a-folder-route` (measured
    2026-08-10, immediately after the pool scan reached 270 rows over 31
    scrolls). Scroll for it first, with the same shared read-only machine.
    """
    started = time.time()
    bring_into_list(ev, convid, _REACH_STEPS)
    click = json.loads(ev(_OPEN_JS % {"convid": json.dumps(convid), "dx": 60}))
    if not click.get("ok"):
        return {"base": None, "acquired_via": "click", "seed_convid": convid,
                "reason": click.get("reason")}
    while True:
        seen = json.loads(ev(_BASE_JS))
        if seen.get("base"):
            return {"base": seen["base"], "acquired_via": "click",
                    "seed_convid": convid,
                    "waited_s": round(time.time() - started, 2)}
        if time.time() - started > timeout:
            return {"base": None, "acquired_via": "click", "seed_convid": convid,
                    "reason": "click-produced-no-folder-route",
                    "href": seen.get("href"),
                    "waited_s": round(time.time() - started, 2)}
        time.sleep(0.5)


def deep_link(convid: str, base: str) -> str:
    """The conversation's URL, derived from its id. See `_NAV_JS`.

    `base` is the tab's own `<origin>/mail/<folder>` prefix. Kept here as well
    as in the JS so the derivation is testable without a browser, and so the
    two can be probed against each other.
    """
    return f"{base.rstrip('/')}/id/{quote(convid, safe='')}"

# Read state comes from the LIST ONLY, never by opening the row (v5.51 /
# E22(a4)). OWA marks the UNREAD row, not the read one, so the only honest
# reading is negative: a row is READ when the unread affordance is observable
# in this list AND absent from that row. See `read_state` below for why the
# whole-list precondition is what keeps it fail-closed.
_LIST_JS = """(() => {
  const rows = [...document.querySelectorAll('[role="option"][data-convid]')];
  return JSON.stringify(rows.map(r => {
    // Harvest the row's DESCRIPTION, never its CONTROLS. Every OWA row carries
    // a `<button aria-label="Mark as unread">`, and that button appears on rows
    // that ARE read — it is the command, not the state. Sweeping it in made the
    // classifier report unread on all 12 live rows (measured 2026-08-09), i.e.
    // the exact inverse of the truth, and the rehearsal then opened nothing.
    const isControl = n => n.closest('button,[role="button"],[role="menuitem"],[role="toolbar"]');
    const label = [r.getAttribute('aria-label'), r.getAttribute('title'),
                   ...[...r.querySelectorAll('[aria-label],[title]')]
                     .filter(n => !isControl(n)).map(
                     n => `${n.getAttribute('aria-label') || ''} ${n.getAttribute('title') || ''}`)]
      .join(' ');
    // The row's own CONTROLS carry a POSITIVE read signal, and it is the one
    // honest reading available from the list: OWA offers "Mark as unread" only
    // on a row that IS read, and "Mark as read" only on one that is NOT. That
    // beats inferring READ from the absence of a marker, which needs some other
    // row in the same view to be unread before it can conclude anything.
    // OWA puts this text in `title` on some builds and `aria-label` on others —
    // measured 2026-08-09: `<button title="Mark as unread">` with NO aria-label.
    // Read both, or the control is invisible to us and every row falls through.
    const ctl = [...r.querySelectorAll('button,[role="button"],[role="menuitem"]')]
      .map(n => `${n.getAttribute('aria-label') || ''} ${n.getAttribute('title') || ''}`).join(' | ');
    return {convid: r.getAttribute('data-convid'),
            unread: /\\bunread\\b/i.test(label),
            marks_unread: /mark as unread/i.test(ctl),
            marks_read: /mark as read/i.test(ctl)};
  }));
})()"""


_TAB_ID: int | None = None

# How long a deep-link open is WAITED FOR, not how long it is slept for.
#
# Measured on the live mailbox, 2026-08-09, Chrome lane, ONE navigation polled
# every 0.5s from the moment `location.href` was set:
#
#     0.62s  document complete, URL ours, NOTHING selected, 0 rows, body 0
#     1.54s  the list names the intended conversation — identity holds — body 28
#     2.78s  body 3953
#     4.32s  body 4020, and unchanged for the following 15s
#
# So a full open costs ~4.5s end to end, of which identity is answerable at
# ~1.5s. The bound is four times the full cost: long enough that a slow morning
# still lands, short enough that a page which never arrives is REPORTED rather
# than waited on forever. On a fast morning the poll returns as soon as the page
# is ready and the rest of the bound is unspent — the earlier fixed `--settle 6`
# slept 6s per open whatever the page did (12 opens, 83s wall clock).
_NAV_TIMEOUT = 20.0
# The CLICK path's settle stays a sleep: a click produces no page load to wait
# on, and 1.2s is what runs 101/102 measured 20/20 opens with.
_CLICK_SETTLE = 1.2
# How far the list may be scrolled to bring an opened conversation back into
# view before the corroborating signal is given up on (v5.57, `recover_selection`).
# Measured 2026-08-09: one scroll took the eligible pool from 12 rendered rows
# to 22, and a step moves ~80% of a viewport (~10 rows) — so six steps covers
# several times the deepest row this rehearsal ever targets, and costs ~1s each
# only on the rows that need it.
# The search re-anchors to the TOP of the list first (`_TOP_JS`), so the bound
# means "the first six windows of the folder", never "six windows below wherever
# the list happened to be" — a list parked mid-folder was measured the same day
# (scrollTop 7656), and a down-only scroll can never find a row above itself.
# ponytail: a fixed window count, not a search. A conversation deeper than six
# windows stays `unconfirmed`; raise the bound only if a live run shows one.
_RECOVERY_STEPS = 6

# (v5.62) How far the list may be scrolled to bring a REFUSED conversation's own
# row into the rendered window so the click fallback has something to click.
# Deeper than `_RECOVERY_STEPS` on purpose, and the reason is the population:
# recovery chases a row OWA has just opened (it is near the list's current
# position by construction), while a refusal is met on the PRIORITY draw, whose
# rows are the oldest in the folder — run 111's four refusals were received
# 7/16, 7/20, 7/24 and 8/1 against a 304-row Inbox.
#
# CALIBRATED ON THOSE FOUR, live, 2026-08-10 — not guessed. They needed 22, 24,
# 10 and MORE THAN 24 steps: at a bound of 24 one row recovered AT exactly the
# bound and the deepest was still short, so the bound WAS the binding
# constraint rather than the folder. A step moves ~80% of a viewport (~10
# rows), so 40 reaches ~400 rows from the top, comfortably past this folder's
# 304. ponytail: still a fixed window count, not a search — raise it again only
# when a live run shows a row it cannot reach.
_REACH_STEPS = 40

# The bare `<origin>/mail/` shell OWA drops the tab to when it refuses to
# deep-link a conversation: folder and id gone, 42 characters of page text
# (measured across three live runs, 2026-08-09; run 111 met it four times).
_SHELL_CHARS = 42


def _ev(win: int, tab: int, js: str, timeout: float = 30) -> str:
    """eval_js, addressed by the tab's STABLE id once we know it.

    Chrome renumbers windows by z-order, so a (window, tab) pair goes stale the
    moment focus moves — measured three times on 2026-08-09, twice mid-rehearsal
    ("Can't get tab 10 of window 1. Invalid index"). Falls back to positional
    addressing only before the id is known.
    """
    if _TAB_ID is not None:
        return eval_js_by_id(_TAB_ID, js, timeout=timeout)
    return eval_js(win, tab, js, timeout=timeout)


def read_state(rows: list[dict]) -> tuple[bool, list[str]]:
    """(is the unread affordance observable, the rows PROVEN already read).

    OWA never labels a row "read" — it labels the UNREAD ones. So "no unread
    marker on this row" means READ only once at least one row in the same list
    carries the marker; without that, "no marker anywhere" and "the marker is
    not exposed to us" are the same observation, and treating them alike would
    open unread mail. No unread row visible ⇒ NOTHING is eligible.
    """
    # PRIMARY: the per-row control is a positive proof of state, so it needs no
    # whole-list precondition. A row is eligible only when it offers "Mark as
    # unread" AND does not offer "Mark as read" — both, so a row exposing
    # neither (or a relabelled/localized UI) falls through rather than passing.
    positive = [r["convid"] for r in rows
                if r.get("marks_unread") and not r.get("marks_read") and r.get("convid")]
    if positive:
        return True, positive
    # FALLBACK: the original negative inference, kept for a UI that exposes no
    # per-row controls. Fails closed for the reason in the docstring above.
    observable = any(r.get("unread") for r in rows)
    if not observable:
        return False, []
    return True, [r["convid"] for r in rows if not r.get("unread") and r.get("convid")]


def collect_eligible(ev, want: int, max_scrolls: int = 20) -> dict:
    """Rows PROVEN already read, scrolling the virtualized list until `want` of
    them exist — or reporting honestly that they do not.

    Only about a dozen rows render at once, so a pool read from ONE view caps
    the sample at a dozen whatever `--rows` asked for. That is how a rehearsal
    asked for 20 measured 12 and still printed CLEAN.

    The screen that decides eligibility is UNCHANGED and still fails closed: it
    is `read_state`, applied per rendered view, and a row that cannot be proven
    read is never added to the pool. Scrolling only changes which rows the
    screen gets to see — it never opens one, and it reads state from the list
    exactly as before.

    Stops on the first of: enough rows, two consecutive scrolls that rendered no
    new conversation (the end of the list, or a list that does not scroll), or
    `max_scrolls`.
    """
    eligible: list[str] = []
    pool, seen, unread = set(), set(), set()
    observable, scrolls, stagnant = False, 0, 0
    scroll_method = None
    # From the TOP, always — see `_TOP_JS`. Without it the pool is whatever the
    # last scroll left on screen, and two runs of the same command sample
    # different conversations.
    from_top = _to_top(ev)
    while True:
        rows = json.loads(ev(_LIST_JS))
        obs, elig = read_state(rows)
        observable = observable or obs
        new = 0
        for r in rows:
            cid = r.get("convid")
            if not cid:
                continue
            if cid not in seen:
                seen.add(cid)
                new += 1
            if r.get("unread"):
                unread.add(cid)
        for cid in elig:
            if cid not in pool:
                pool.add(cid)
                eligible.append(cid)
        if len(eligible) >= want:
            break
        stagnant = stagnant + 1 if (scrolls and not new) else 0
        if stagnant >= 2 or scrolls >= max_scrolls:
            break
        scroll = json.loads(ev(_SCROLL_JS))
        if not scroll.get("ok"):
            break
        scroll_method = scroll.get("method")
        scrolls += 1
        time.sleep(0.6)
    return {"eligible": eligible, "observable": observable, "seen": seen,
            "rows_seen": len(seen), "unread": len(unread),
            "scrolls": scrolls, "scroll_method": scroll_method,
            "from_top": from_top,
            # What the caller asked for, so "short" is a fact in the record and
            # not something the reader has to notice.
            "rows_requested": want, "reached_requested": len(eligible) >= want}


def _parse_convids(spec: str) -> list[str]:
    """`--convids` → an ordered, de-duplicated id list.

    Accepts commas, whitespace/newlines, or `@path` for a file of one id per
    line — a conversation id is long and contains `/` and `+`, so pasting nine
    of them on a command line is how one arrives silently truncated.
    """
    if not spec:
        return []
    if spec.startswith("@"):
        spec = Path(spec[1:]).expanduser().read_text()
    out: list[str] = []
    for raw in spec.replace(",", "\n").split():
        cid = raw.strip().strip('"\'')
        if cid and cid not in out:
            out.append(cid)
    return out


def _fingerprint(a: dict) -> tuple | None:
    """What this attempt actually DID, so "the re-target differed" is checkable
    on every primitive rather than only on the click.

    A click is identified by its point, a deep-link navigation by the URL it
    went to. An attempt that names neither never acted (`row-not-rendered`), and
    fingerprints as nothing — two of those are not one action taken twice.
    """
    point, url = a.get("point"), a.get("nav_url")
    if not point and not url:
        return None
    return (a.get("method"), json.dumps(point, sort_keys=True), url)


def contract_problems(attempts: list[dict]) -> list[str]:
    """What would make this record unscorable, checked rather than assumed.

    The Chrome lane's own `rehearse` cannot violate these; a rehearsal driven
    by a MODEL (`--score`, the `iab` lane) can, and a scorer that takes its
    input on trust is a check that cannot fail. Two things are load-bearing:
    every attempt is its own row with both identity fields present (E30(a),
    v5.48), and the second attempt CHANGED something (E30(e), v5.50) — run
    101's re-target re-clicked the same point of the same row and doctrine
    calls that no re-target at all, whatever it produced.
    """
    problems = []
    for i, a in enumerate(attempts):
        where = f"attempt[{i}]"
        for key in ("seq", "attempt", "intended", "outcome"):
            if a.get(key) in (None, ""):
                problems.append(f"{where}: missing {key}")
        # v5.57: a corroboration claimed from the recovery path must say what it
        # cost. A model-driven record can otherwise label a directly-corroborated
        # open `recovery` (or invent a third word), and the recovery rate — the
        # whole reason the field exists — would stop meaning anything.
        via = a.get("corroborated_via")
        if via not in (None, "direct", "recovery"):
            problems.append(f"{where}: unknown corroborated_via {via!r} (v5.57)")
        elif via == "recovery" and not a.get("recovery_steps"):
            problems.append(f"{where}: corroborated_via 'recovery' names no "
                            "recovery_steps (v5.57)")
        if a.get("outcome") == "already-open-skipped":
            continue
        for key in ("target_produced_pre", "target_produced"):
            if key not in a:
                problems.append(f"{where}: missing {key} (E30(a)/(d))")
    by_seq: dict = {}
    for a in attempts:
        by_seq.setdefault(a.get("seq"), []).append(a)
    for seq, rows in by_seq.items():
        second = [r for r in rows if r.get("attempt") == 2]
        if not second:
            continue
        first = [r for r in rows if r.get("attempt") == 1]
        if not first:
            problems.append(f"seq {seq}: attempt 2 with no attempt-1 row (E30(a))")
            continue
        if not second[0].get("retarget_changed"):
            problems.append(f"seq {seq}: re-target names no change (E30(e))")
        # Two attempts that never acted (`row-not-rendered`) fingerprint as
        # nothing, and "both are None" is not one action taken twice.
        elif _fingerprint(second[0]) and _fingerprint(second[0]) == _fingerprint(first[-1]):
            did = (f"clicked the SAME point {second[0]['point']}"
                   if second[0].get("point") else
                   f"navigated to the SAME URL {second[0].get('nav_url')}")
            problems.append(f"seq {seq}: re-target {did} — not a re-target (E30(e))")
    return problems


def summarize(attempts: list[dict]) -> dict:
    """The whole product of this tool: did the opens land, and on what?

    `first_attempt_ok` is the number that matters — 20/20 is what the Chrome
    lane measured on runs 101 and 102, and a night that cannot reach it is a
    lane regression, not a slow pass.
    """
    opened = [a for a in attempts if a["outcome"] in ("landed", "landed-on-retarget")]
    return {
        "rows_attempted": len({a["intended"] for a in attempts}),
        "opens_landed": len(opened),
        "first_attempt_ok": sum(1 for a in attempts
                                if a["attempt"] == 1 and a["outcome"] == "landed"),
        "retargets": sum(1 for a in attempts if a["attempt"] == 2),
        "mismatches": sum(1 for a in attempts if a["outcome"] == "mismatch"),
        "never_moved": sum(1 for a in attempts
                           if a["outcome"] == "mismatch"
                           and a["target_produced"] == a["target_produced_pre"]),
        "unreadable": sum(1 for a in attempts if a["outcome"] == "no-id"),
        # (v5.62) Deep-link only, and NOT a mismatch: OWA answered the
        # navigation with the bare `<origin>/mail/` shell — no conversation
        # opened, the reading pane never moved. Counted in three parts because
        # the three mean different things to a night: how often OWA refused, how
        # many of those the click fallback recovered, and how many it could not
        # reach at all (the only ones that cost the run a body).
        "navigation_refused": sum(1 for a in attempts
                                  if a["outcome"] == "navigation-refused"),
        "refused_recovered": sum(1 for a in attempts
                                 if a["attempt"] == 2
                                 and a["outcome"] == "landed-on-retarget"
                                 and a.get("retarget_scrolls") is not None),
        "row_unreachable": sum(1 for a in attempts
                               if a["outcome"] == "row-unreachable"),
        "reach_scrolls": _stats(attempts, "retarget_scrolls"),
        # Deep-link only: the URL agreed and the app never corroborated it. NOT
        # an open that landed — under navigation the URL is the input we
        # supplied, so on its own it is the vacuous-pass shape.
        "unconfirmed": sum(1 for a in attempts if a["outcome"] == "unconfirmed"),
        # Deep-link only, and the point of v5.57: WHICH path corroborated each
        # landed open — the app named the row in the list it re-rendered
        # (`direct`), or it only named it once the list was scrolled back to it
        # (`recovery`). Reported separately so a rising recovery rate is visible
        # rather than absorbed into one `landed` count, the discipline v5.53
        # applied to recovered mismatches. A CLICK-path landing carries neither:
        # under a click the reading-pane URL is itself app-produced, so these
        # need not sum to `opens_landed`.
        "corroborated_direct": sum(1 for a in attempts
                                   if a.get("corroborated_via") == "direct"),
        "corroborated_after_recovery": sum(1 for a in attempts
                                           if a.get("corroborated_via") == "recovery"),
        # How often the signal was UNAVAILABLE at all, and what it cost to get
        # back — including the recoveries that failed and stayed `unconfirmed`.
        "recovery_attempted": sum(1 for a in attempts
                                  if a.get("recovery_steps") is not None),
        "recovery_scrolls": _stats(attempts, "recovery_steps"),
        # What the navigation COST, measured rather than assumed: a full
        # document load discards the list, its scroll position and every
        # rendered row; an in-app route change keeps them.
        "full_reloads": sum(1 for a in attempts if a.get("reloaded") is True),
        # The readiness wait, reported rather than claimed: how long each open
        # actually took to become assertable, and how many never did. Without
        # these the "it gets faster on a fast morning" claim needs a stopwatch.
        "open_wait_s": _stats(attempts, "waited_s"),
        "identity_wait_s": _stats(attempts, "ready_s"),
        "ready_timeouts": sum(1 for a in attempts if a.get("ready_timed_out")),
        "body_settle_timeouts": sum(1 for a in attempts
                                    if a.get("body_settle_timed_out")),
        # Does navigating to one of these URLs actually RENDER the body? v5.55
        # listed that as unproven and this is the number that answers it — a
        # landed open whose reading pane held no text is not an open the night
        # could have extracted anything from.
        "bodies_rendered": sum(1 for a in attempts
                               if a["outcome"] in ("landed", "landed-on-retarget")
                               and (a.get("body_chars") or 0) > 0),
    }


def _stats(attempts: list[dict], key: str) -> dict | None:
    vals = sorted(a[key] for a in attempts if a.get(key) is not None)
    if not vals:
        return None
    return {"median": round(vals[len(vals) // 2], 2),
            "max": round(vals[-1], 2), "total": round(sum(vals), 2)}


def verdict(summary: dict, eligible: int, problems: list[str] | None = None,
            requested: int | None = None) -> str:
    if problems:
        return ("INVALID — the record cannot be scored: " + "; ".join(problems[:3])
                + (f" (+{len(problems) - 3} more)" if len(problems) > 3 else ""))
    if not eligible:
        return "NO-EVIDENCE — no eligible already-read row to rehearse on"
    if summary["mismatches"]:
        shape = ("the open never moved the pane"
                 if summary["never_moved"] == summary["mismatches"]
                 else "the pane moved to the wrong conversation")
        return (f"LANE REGRESSION — {summary['mismatches']} mismatch(es); "
                f"{shape}. This is run 103's shape; do not spend a night on it")
    # Before "did it land": could the app CORROBORATE any landing at all? A
    # deep-link run where the URL always agreed and the list never named a
    # selected row proves only that the address bar echoes what we typed.
    if summary.get("unconfirmed"):
        return (f"UNCORROBORATED — {summary['unconfirmed']} open(s) had the "
                "intended URL and NO app-produced confirmation (the list named "
                "no single selected row, and the conversation did not render "
                "even after the list was scrolled back to it). Under navigation "
                "the URL is the input, not the evidence: this does not promote")
    # (v5.62) A refusal the click fallback could not even REACH is its own
    # answer, and it is not "some rows never opened": the navigation was
    # refused, the row never rendered inside the bound, and nothing was opened
    # or touched. Named so a night reads it as a reach problem — raise the
    # bound, or draw differently — rather than as a lane regression.
    if summary.get("row_unreachable"):
        return (f"REFUSED, UNREACHABLE — {summary['row_unreachable']} "
                "navigation(s) OWA refused whose row never rendered inside the "
                f"{_REACH_STEPS}-step scroll bound, so the click fallback had "
                "nothing to click. Nothing was opened and nothing moved; these "
                "are held by name, not scored as identity mismatches")
    if summary["opens_landed"] < summary["rows_attempted"]:
        return "DEGRADED — some rows never opened; see the per-attempt records"
    # Everything below here would otherwise print CLEAN. A clean verdict over
    # fewer rows than were asked for is a FALSE ALL-CLEAR — the promotion bar is
    # "20 rows, first_attempt_ok: 20", and a run that sampled 12 of 20 has not
    # met it however well those 12 went. So shortness outranks the pass word
    # rather than sitting in the summary for a reader to notice.
    if requested and summary["rows_attempted"] < requested:
        return (f"SHORT SAMPLE — only {summary['rows_attempted']} of the "
                f"{requested} rows asked for could be sampled (the list exposed "
                f"no more rows PROVEN already read); "
                f"{summary['first_attempt_ok']}/{summary['rows_attempted']} "
                "first attempt"
                + (f", {summary['retargets']} re-target(s)"
                   if summary["retargets"] else "")
                + ". This is NOT a clean run: a pass measured over fewer rows "
                "than requested is a false all-clear")
    # A pass that needed the list scrolled back to corroborate is still a pass —
    # the open landed and the app named it — but it is not the same pass, so the
    # verdict SAYS so rather than leaving it in the summary to be noticed.
    rec = summary.get("corroborated_after_recovery") or 0
    tail = (f"; {rec} corroborated only after the list was scrolled back to it"
            if rec else "")
    # (v5.62) A refusal that the click fallback recovered is a PASS — nothing
    # wrong opened, and the row was read — but it is not the same pass, so the
    # verdict says so rather than leaving it in the summary to be noticed.
    ref = summary.get("refused_recovered") or 0
    if ref:
        tail += (f"; {ref} navigation(s) OWA refused, recovered by the click "
                 "fallback after scrolling the row into the list")
    if summary["retargets"]:
        return (f"LANDS, WITH RETRIES — {summary['first_attempt_ok']}/"
                f"{summary['rows_attempted']} first attempt{tail}")
    return (f"CLEAN — {summary['first_attempt_ok']}/{summary['rows_attempted']} "
            f"first attempt{tail}")


def _open_once(win: int, tab: int, convid: str, dx: int, settle: float) -> dict:
    raw = _ev(win, tab, _OPEN_JS % {"convid": json.dumps(convid), "dx": dx})
    click = json.loads(raw)
    if not click.get("ok"):
        return {"outcome": "no-click", "detail": click.get("reason"),
                "method": "click", **click}
    time.sleep(settle)
    produced = json.loads(_ev(win, tab, _PANE_JS))["produced"]
    click["produced"], click["method"] = produced, "click"
    if not produced:
        click["outcome"] = "no-id"          # an unreadable surface is a mismatch
    elif produced == convid:
        click["outcome"] = "landed"
    else:
        click["outcome"] = "mismatch"
    return click


def await_ready(ev, convid: str, timeout: float) -> dict:
    """Wait until the open is ASSERTABLE, then read it. Bounded, never slept.

    A fixed sleep is the wrong mechanism for a page load: too short on a slow
    morning — measured 2026-08-09, one open in two came back mismatched at the
    3.0s default because the assert read while the page was still routing — and
    wasted on a fast one. So this polls `_AFTER_JS` until its `ready` predicate
    holds: the document has finished loading (`readyState === 'complete'`), the
    URL carries the intended conversation id, AND OWA's list marks that same one
    conversation selected.

    ON TIMEOUT it returns the LAST reading and stamps `ready_timed_out`, leaving
    the outcome exactly what it already was — URL agreeing with no selection is
    `unconfirmed`, URL never agreeing is a mismatch, an unreadable surface is
    `no-id`. It never invents a pass, and it never counts an open from a page
    that was still loading.

    THEN IT WAITS FOR THE BODY, which arrives LATER than the identity does.
    Measured on the live mailbox, 2026-08-09, one navigation polled every 0.5s:
    identity held at 1.54s with the reading pane holding **28 characters**, and
    the text was still arriving at 2.78s (3953) and 4.32s (4020). Returning at
    the first `ready` reports a landed open with an empty body — and the night
    EXTRACTS immediately after this wait, so it would extract nothing from a
    thread it opened correctly. So once identity holds, this waits for the pane
    text to STOP GROWING (unchanged across two consecutive polls).

    The two waits are reported SEPARATELY (`ready_s` and `waited_s`,
    `ready_timed_out` and `body_settle_timed_out`) and never merged: the
    outcome — landed / mismatch / unconfirmed — is decided on IDENTITY alone,
    because a correctly-opened conversation with a genuinely empty body is still
    a correctly-opened conversation.

    A full document load takes the page away for a second or two and every read
    against it raises; that is a slow open, not an unreadable surface, so a
    failed read is retried inside the same bound rather than ending the attempt.
    """
    js = _AFTER_JS % {"convid": json.dumps(convid)}
    t0, err = time.time(), None
    last: dict = {"produced": "", "selected": None}
    ready_at, prev_body = None, None
    while True:
        try:
            last = json.loads(ev(js))
            if last.get("ready"):
                if ready_at is None:
                    ready_at = time.time() - t0
                body = last.get("body_chars")
                if body and body == prev_body:
                    break
                prev_body = body
        except Exception as exc:                                   # noqa: BLE001
            err = str(exc)[:160]
        if time.time() - t0 >= timeout:
            # Which of the two waits expired is the whole diagnosis: identity
            # never holding is a lane problem, a body that never settles is an
            # extraction problem, and one word for both would hide either.
            last["body_settle_timed_out" if ready_at is not None
                 else "ready_timed_out"] = True
            break
        time.sleep(0.25)
    last["ready_s"] = round(ready_at, 2) if ready_at is not None else None
    last["waited_s"] = round(time.time() - t0, 2)
    if err and not last.get("produced"):
        last["read_error"] = err
    return last


def classify(after: dict, convid: str) -> str:
    """The deep-link outcome, from one reading of the page. One rule, both the
    direct read and the post-recovery read — a second copy would be a second
    opinion about what an unmarked row means.

    The assert is UNCHANGED and still needs both halves: the URL carries the
    intended id (we supplied it, so on its own it proves the address bar echoes)
    AND the app's own list names that same single row selected.

    What v5.57 adds is the distinction the old null could not carry. A `selected`
    of null used to mean two different things at once:

      * the target row IS rendered, the list HAS a selection affordance, and
        NOTHING is selected — the app is telling us this row is not open. That
        is a genuine MISMATCH and it fails, exactly as a wrong id does.
      * the target row is NOT rendered — the app cannot mark a row it is not
        rendering. The signal is UNAVAILABLE, not negative, and that is the row
        `recover_selection` scrolls for.

    The negative reading fires only where it is honest: the affordance has to be
    observable somewhere in the list, and exactly zero rows selected. Several
    selected rows stay ambiguous, the same way `read_state` refuses to infer
    "read" from a missing unread marker nothing else in the list is carrying.

    (v5.62) AND ONE MORE DISTINCTION INSIDE THE OLD `no-id`, for the same reason
    v5.57 split the null `selected`: a produced id of "" meant two different
    things at once. Either the page is SOMETHING we could not read an id off —
    unreadable, and doctrine's vacuous-pass shape one layer down — or the tab is
    sitting on the bare `<origin>/mail/` shell, folder and id gone, at or below
    42 characters of page text. The second is OWA REFUSING THE NAVIGATION: it
    did not open the wrong conversation, it opened no conversation at all, and
    the reading pane never moved. That is `navigation-refused`, and its repair
    is the CLICK fallback, not a mismatch verdict. Both halves have to hold —
    an empty id AND a shell-length page actually read — so a page this process
    could not read at all (every evaluation raised, `body_chars` absent) stays
    `no-id` rather than being flattered into a refusal.
    """
    produced, selected = after.get("produced"), after.get("selected")
    body = after.get("body_chars")
    if not produced:
        if isinstance(body, int) and not isinstance(body, bool) and body <= _SHELL_CHARS:
            return "navigation-refused"
        return "no-id"              # an unreadable surface is a mismatch
    if produced != convid:
        return "mismatch"
    if selected == convid:
        return "landed"
    if selected is not None:
        return "mismatch"           # the app says a different row is open
    if (after.get("target_rendered") and after.get("selected_attr_seen")
            and after.get("selected_count") == 0):
        return "mismatch"
    return "unconfirmed"


def recover_selection(ev, convid: str, after: dict,
                      steps: int = _RECOVERY_STEPS) -> dict:
    """Scroll the opened conversation back into the list, then re-read the assert.

    WHY (measured twice, 2026-08-09, the same conversation both times — the
    20-row run's seq 14 and the 13-row probe's seq 2). The navigation succeeded:
    the URL agreed and the reading pane rendered 536 characters. OWA then
    re-rendered 13 list rows that did NOT include the conversation it had just
    opened, so every rendered row read `aria-selected="false"` and the
    corroborating half of the assert had no row to read at all. A targeted probe
    confirmed the shape: the conversation IS an Inbox row, it is simply not
    rendered after the reload.

    So bring it back into view — the list is virtualized, and the scroll that
    the v5.56 sample collector already uses is the mechanism. Nothing about the
    assert is relaxed: `classify` decides the outcome from the recovered
    reading exactly as it does from the first one, so a row that renders
    UNSELECTED is a genuine mismatch and still fails, and a conversation that
    never renders inside the bound is still `unconfirmed` with nothing
    extracted. Recovery that cannot find the row never degrades into "assume it
    is fine".

    It reads state and scrolls. It dispatches no click, sets no location, and
    can open nothing.
    """
    reading, taken = bring_into_list(ev, convid, steps)
    # The recovered reading wins on the fields it carries; the timings from the
    # readiness wait (`ready_s`, `waited_s`, either expiry) are not re-measured
    # and are not overwritten.
    return {**after, **reading, "recovery_steps": taken}


def bring_into_list(ev, convid: str, steps: int) -> tuple[dict, int]:
    """Scroll a virtualized list until it RENDERS `convid`'s row. (reading, steps)

    ONE scroll machine with two callers, deliberately — a second copy would be a
    second opinion about where a row is and how far the list may be moved to
    find it. `recover_selection` (v5.57) needs the row rendered so OWA has
    something to mark selected; the v5.62 click fallback needs it rendered so
    there is a node to click at all. Same list, same bound shape, same absence.

    Search from the TOP down, not from wherever the list was left: a scroll that
    only goes down can never find a row ABOVE the current view, and a list parked
    mid-folder was measured on 2026-08-09 (scrollTop 7656 after a navigation the
    app refused). Re-anchoring makes the bound mean "the first N windows of the
    folder" instead of "N windows below here".

    It reads state and scrolls. It dispatches no click, sets no location, and
    can open nothing — the read-state screen upstream still decides what is
    eligible, and scrolling changes only which rows it gets to SEE.
    """
    reading: dict = {}
    js = _AFTER_JS % {"convid": json.dumps(convid)}
    taken, stalled = 0, 0
    _to_top(ev)
    while True:
        try:
            reading = json.loads(ev(js))
        except Exception as exc:                                   # noqa: BLE001
            reading = {**reading, "recovery_error": str(exc)[:160]}
        if reading.get("target_rendered") or taken >= steps:
            break
        try:
            scroll = json.loads(ev(_SCROLL_JS))
        except Exception as exc:                                   # noqa: BLE001
            reading = {**reading, "recovery_error": str(exc)[:160]}
            break
        if not scroll.get("ok"):
            # (v5.62) A refusal is a FULL DOCUMENT LOAD (measured: 4 of 4), so
            # the list is often still re-rendering when the first scroll fires
            # and `_SCROLL_JS` answers `no-rows` — "this list cannot scroll" and
            # "this list has not rendered yet" are the same reply. Giving up on
            # the first one cost a recoverable row: the same conversation
            # reached its row in 22 steps on one pass and gave up after ONE on
            # another. Wait and re-ask a bounded number of times, then believe
            # it. ponytail: three tries, not a readiness protocol.
            stalled += 1
            if stalled >= 3:
                reading = {**reading, "scroll_stalled": scroll.get("reason")}
                break
            time.sleep(0.8)
            continue
        stalled = 0
        taken += 1
        time.sleep(0.6)
    return reading, taken


def _navigate_once(win: int, tab: int, convid: str, settle: float,
                   base: str | None = None) -> dict:
    """Attempt 1 of the deep-link primitive: go to the conversation's own URL.

    Nothing here touches a row, so nothing here can act on a recycled node —
    which is the entire failure this replaces (runs 73, 75, 101, 103, 104, 105).

    `settle` is the readiness TIMEOUT, not a sleep — see `await_ready`.
    """
    try:
        nav = json.loads(_ev(win, tab, _NAV_JS % {"convid": json.dumps(convid)}))
    except Exception as exc:                                       # noqa: BLE001
        # The script sets `location.href` and returns synchronously, so it
        # normally answers before the navigation commits — but a page torn down
        # mid-call answers with nothing, and losing the whole attempt to that
        # would read as a lane failure. The navigation may well have fired, so
        # go on to the assert: it reads the truth off the page either way.
        nav = {"ok": True, "id": convid, "pre": None, "url": None,
               "nav_eval_error": str(exc)[:160]}
    # The tab lost its folder segment (a conversation OWA refused to open drops
    # it to `<origin>/mail/`). The folder is still known — this run has been
    # reading it — so re-anchor on the remembered base rather than losing every
    # remaining row. See `_GOTO_JS`.
    if (not nav.get("ok") and base
            and nav.get("reason") == "not-on-a-mail-folder-url"):
        nav = json.loads(_ev(win, tab, _GOTO_JS % {
            "url": json.dumps(deep_link(convid, base)),
            "convid": json.dumps(convid)}))
    if not nav.get("ok"):
        return {"outcome": "no-click", "detail": nav.get("reason"),
                "method": "navigate", **nav}
    def ev(js: str) -> str:
        return _ev(win, tab, js)

    after = await_ready(ev, convid, settle)
    outcome = classify(after, convid)
    # The corroborating signal can be UNAVAILABLE rather than negative: OWA
    # re-renders about a dozen rows and is not guaranteed to include the one it
    # just opened, and it cannot mark a row it is not rendering. Scroll it back
    # into view and read the same assert off the row itself (v5.57). Fires only
    # where the null is genuinely an absence — `classify` has already ruled the
    # readable negative a mismatch, and a list with no selection affordance at
    # all has nothing to recover.
    if (outcome == "unconfirmed" and not after.get("target_rendered")
            and after.get("selected_attr_seen")):
        after = recover_selection(ev, convid, after)
        outcome = classify(after, convid)
        recovered = True
    else:
        recovered = False
    nav.update(after, method="navigate", produced=after.get("produced"),
               nav_url=nav.get("url"), outcome=outcome)
    if outcome == "landed":
        # WHICH path corroborated it. A rising recovery rate is a lane drifting;
        # absorbed into one `landed` count it would be invisible.
        nav["corroborated_via"] = "recovery" if recovered else "direct"
    return nav


def _reach_and_click(win: int, tab: int, convid: str, dx: int) -> tuple[dict, int]:
    """The v5.62 click fallback: bring the row into the list, THEN click it.

    WHY THIS IS THE FIX (measured, run 111, 2026-08-10). The bounded re-target
    has been the click path since v5.55, and on the night it never fired: all
    four refused conversations died at ATTEMPT 1. OWA answers a refused deep
    link by dropping the tab to the bare shell and re-rendering about a dozen
    rows from the top of the folder, and a PRIORITY row is the oldest mail in
    that folder — so `_OPEN_JS` correctly reported `row-not-rendered` and there
    was nothing to click. A fallback that cannot reach its row is not a
    fallback; it is a second refusal wearing the first one's cause.

    So scroll for it first, with the same bounded, read-only machine
    `recover_selection` already uses. If the row still never renders, that is
    NAMED (`row-unreachable`) and counted — never a silent skip, and never
    softened into a landing.

    AND A RECOVERED OPEN IS MEASURED THE SAME WAY A NAVIGATED ONE IS. The whole
    point of the fallback is that the night can READ the thread, and the click
    path on its own answers only "which conversation is in the pane" — it reads
    no `body_chars` at all. The first daylight run of this fix landed 3 of 4
    refusals and could not say whether ANY of them rendered text, which is the
    same half-answer v5.55 shipped and v5.57 had to close. So once the click
    lands, wait for the body exactly as the navigation does (`await_ready`,
    which returns as soon as the pane stops growing) and keep its page facts.
    """
    ev = lambda js: _ev(win, tab, js)                               # noqa: E731
    reading, steps = bring_into_list(ev, convid, _REACH_STEPS)
    if not reading.get("target_rendered"):
        return ({"outcome": "row-unreachable", "method": "click",
                 "detail": f"row-not-rendered-after-{steps}-scroll-step(s)",
                 "pre": reading.get("produced") or None, "produced": None,
                 "rows_rendered": reading.get("rows_rendered")}, steps)
    click = _open_once(win, tab, convid, dx, _CLICK_SETTLE)
    if click.get("outcome") == "no-click":
        # The row rendered and the click was still REFUSED — the v5.50
        # containment guard resolving its candidate point onto something that is
        # not this row (measured live 2026-08-10: `point-outside-row`, on a row
        # that had landed cleanly from the same point on an earlier pass, so it
        # is a transient overlay rather than a property of the row). That
        # refusal is CORRECT and is never softened: a coordinate click that
        # cannot prove containment is how run 61 filtered the list by clicking a
        # category chip. But the thread still owes an outcome, and the outcome
        # is the same one the unrendered case gets — nothing opened, held by
        # name, counted — so it takes that word with its own sub-cause in
        # `detail` rather than a twelfth word meaning the same thing.
        click["outcome"] = "row-unreachable"
        click["detail"] = f"click-refused-after-{steps}-scroll-step(s): " \
                          f"{click.get('detail') or 'unknown'}"
    if click.get("outcome") == "landed":
        after = await_ready(ev, convid, _NAV_TIMEOUT)
        # The CLICK decided identity and keeps it — this read only adds what the
        # click path cannot see. `produced`/`outcome` are never overwritten.
        for k in ("body_chars", "rows_rendered", "selected", "selected_count",
                  "selected_attr_seen", "target_rendered", "ready_s",
                  "waited_s", "body_settle_timed_out"):
            if after.get(k) is not None:
                click[k] = after[k]
    return click, steps


def rehearse(win: int, tab: int, convids: list[str], settle: float,
             deep_link_mode: bool = False) -> list[dict]:
    """Open each row twice at most: the primitive, then ONE re-target that DIFFERS.

    In deep-link mode the re-target is the CLICK path — the documented fallback,
    and the maximally different action doctrine's E30(e) asks for. Re-navigating
    to the same URL would be run 101's defect one primitive over: a retry that
    repeats the attempt that just failed is not a retry.

    `settle` means different things to the two primitives and deliberately so:
    for a navigation it is the readiness TIMEOUT, for a click the sleep it has
    always been. So the click fallback of a deep-link run keeps its own 1.2s
    rather than sleeping out the navigation's whole bound.
    """
    attempts: list[dict] = []
    # The folder this run is reading, observed on the tab's own URL and kept
    # current from every navigation that produced one. It is what lets a row
    # survive the tab being dropped to `<origin>/mail/` — see `_GOTO_JS`.
    try:
        base = json.loads(_ev(win, tab, _BASE_JS)).get("base") or None
    except Exception:                                              # noqa: BLE001
        base = None
    if deep_link_mode:
        steps = [(None, None), (60, "fell back to the CLICK primitive: "
                                    "scrolled the virtualized list until the row "
                                    "rendered, re-read rect and id in one "
                                    "evaluation, clicked the sender line")]
    else:
        steps = [(60, None),
                 (140, "re-scrolled into view, re-read rect+id, clicked a "
                       "different point on the sender line")]
    for seq, convid in enumerate(convids, 1):
        for attempt, (dx, changed) in enumerate(steps, start=1):
            reach_steps = None
            if dx is None:
                r = _navigate_once(win, tab, convid, settle, base)
            elif deep_link_mode:
                r, reach_steps = _reach_and_click(win, tab, convid, dx)
            else:
                r = _open_once(win, tab, convid, dx, settle)
            if r.get("url"):                       # keep the folder current
                base = r["url"].split("/id/")[0] or base
            if r.get("pre") == convid:
                attempts.append({"seq": seq, "attempt": attempt, "intended": convid,
                                 "method": r.get("method"),
                                 "target_produced_pre": convid, "target_produced": convid,
                                 "outcome": "already-open-skipped"})
                break
            row = {"seq": seq, "attempt": attempt, "intended": convid,
                   "target_intended": convid, "method": r.get("method"),
                   # WHY it never acted. Without this a `no-click` row says
                   # nothing at all, and diagnosing eight of them in a row cost
                   # a live re-probe (2026-08-09) that the record should have
                   # answered by itself.
                   "detail": r.get("detail"),
                   "target_produced_pre": r.get("pre"),
                   "target_produced": r.get("produced"),
                   "in_view": r.get("in_view"), "point": r.get("point"),
                   "nav_url": r.get("nav_url"), "rect": r.get("rect"),
                   "selected": r.get("selected"),
                   "selected_count": r.get("selected_count"),
                   "selected_attr_seen": r.get("selected_attr_seen"),
                   # v5.57: whether the app had a row to answer with at all,
                   # which path answered, and what the recovery cost.
                   "target_rendered": r.get("target_rendered"),
                   "target_selected": r.get("target_selected"),
                   "corroborated_via": r.get("corroborated_via"),
                   "recovery_steps": r.get("recovery_steps"),
                   "body_chars": r.get("body_chars"),
                   "rows_rendered": r.get("rows_rendered"),
                   "reloaded": r.get("reloaded"),
                   # What the readiness wait actually cost, per open, and
                   # whether it expired — the evidence for "faster on a fast
                   # morning, still correct on a slow one". `ready_s` is when
                   # IDENTITY held, `waited_s` when the body also stopped
                   # growing; the gap between them is why one number would lie.
                   "ready_s": r.get("ready_s"),
                   "waited_s": r.get("waited_s"),
                   "ready_timed_out": r.get("ready_timed_out"),
                   "body_settle_timed_out": r.get("body_settle_timed_out"),
                   # v5.62: what it cost to make the fallback REACHABLE. A
                   # re-target that needed 17 scroll steps and one that needed
                   # none are not the same lane, and one `landed-on-retarget`
                   # count hides the difference.
                   "retarget_scrolls": reach_steps,
                   "outcome": r["outcome"]}
            if attempt == 2:
                row["retarget_changed"] = changed
                if reach_steps:
                    row["retarget_changed"] += (
                        f" (after {reach_steps} scroll step(s) to bring the row "
                        "into the rendered list)")
                if row["outcome"] == "landed":
                    row["outcome"] = "landed-on-retarget"
            attempts.append({k: v for k, v in row.items() if v is not None
                             or k in ("target_produced_pre", "target_produced")})
            # `unconfirmed` is not a targeting failure — the re-target exists
            # for a MISMATCH, and re-opening a row the app never corroborated
            # cannot corroborate it either. Stop and report it as what it is.
            if row["outcome"] in ("landed", "landed-on-retarget", "unconfirmed"):
                break
    return attempts


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--rows", type=int, default=5,
                   help="how many already-read conversations to open (default 5)")
    p.add_argument("--probe", action="store_true",
                   help="report what the list exposes and open NOTHING")
    p.add_argument("--match", default=HOST, help="URL substring naming the OWA tab")
    p.add_argument("--tab-id", default=None,
                   help="rehearse THIS Chrome tab, by its stable id, instead of "
                        "whichever OWA tab scores highest. NAME IT WHENEVER THE "
                        "RUN OPENED ITS OWN: the owner has an OWA tab too, a "
                        "substring match cannot tell them apart, and the tab "
                        "that loses the pick is the one the visibility hold is "
                        "holding — so the pass then drives a tab nothing is "
                        "keeping visible while the hold re-activates the other, "
                        "and the two fight. Measured 2026-08-10: 19 of 20 opens "
                        "timed out waiting for identity, 0 landed, on a lane "
                        "that was healthy. Bind this and the hold's --tab-id to "
                        "the same id")
    p.add_argument("--deep-link", action="store_true",
                   help="open by NAVIGATING to each conversation's own URL "
                        "instead of clicking its row; the click path becomes "
                        "the one bounded re-target")
    p.add_argument("--settle", type=float, default=None,
                   help="clicking: seconds to let the reading pane settle after "
                        f"an open (default {_CLICK_SETTLE}). NAVIGATING: the "
                        "readiness-wait TIMEOUT, not a sleep — the open is "
                        "waited for until the page has loaded, the URL carries "
                        "the intended conversation, OWA marks it selected and "
                        "the reading pane has stopped growing (default "
                        f"{_NAV_TIMEOUT:.0f}s ≈ 4x the ~4.5s measured cost of "
                        "one open)")
    p.add_argument("--convids", default="",
                   help="rehearse EXACTLY these conversation ids (comma- or "
                        "whitespace-separated, or @<file> one per line) instead "
                        "of the first --rows of the pool. The read-state screen "
                        "is NOT bypassed: a requested id that the list does not "
                        "render, or does not PROVE already-read, is reported as "
                        "skipped and never opened. Use it to ask whether the "
                        "conversations that failed on a given night fail in "
                        "daylight too")
    p.add_argument("--max-scrolls", type=int, default=20,
                   help="how many times the list may be scrolled to reach "
                        "--rows eligible conversations (default 20)")
    p.add_argument("--out", type=Path, help="also write the report as JSON here")
    p.add_argument("--self-check", action="store_true",
                   help="verify the report logic, touch no browser")
    # --- off-lane halves, for a rehearsal this process cannot drive itself ---
    p.add_argument("--emit-js",
                   choices=("list", "locate", "pane", "nav", "after", "scroll",
                            "top", "base"),
                   help="print the JS to evaluate and exit (touches no browser)")
    p.add_argument("--convid", default="",
                   help="--emit-js locate/nav/after: the target id. On `after` "
                        "it is what `ready` is computed against — omit it and "
                        "`ready` is always false")
    p.add_argument("--dx", type=int, default=60,
                   help="--emit-js locate: click-point x offset (60, re-target 140)")
    p.add_argument("--select", type=Path, metavar="LIST.json",
                   help="screen a list JSON for rows PROVEN read; open nothing")
    p.add_argument("--score", type=Path, metavar="ATTEMPTS.json",
                   help="score an off-lane rehearsal record; touches no browser")
    args = p.parse_args(argv)

    if args.self_check:
        return _self_check()
    if args.emit_js:
        fixed = {"list": _LIST_JS, "pane": _PANE_JS, "scroll": _SCROLL_JS,
                 "top": _TOP_JS, "base": _BASE_JS}
        templates = {"nav": _NAV_JS, "after": _AFTER_JS, "locate": _LOCATE_JS}
        print(fixed.get(args.emit_js) or templates[args.emit_js]
              % {"convid": json.dumps(args.convid), "dx": args.dx})
        return 0
    if args.select:
        return _select(args.select, args.rows)
    if args.score:
        return _score(args.score, args.out)

    seed_needed = False
    prior_app, prior_win = None, _front_window_id()
    try:
        prior_app = _frontmost_app()
    except Exception:                                          # noqa: BLE001
        pass

    report: dict = {"tool": "cos_lane_rehearsal", "mutations": "none",
                    "primitive": "deep-link" if args.deep_link else "click",
                    "run_id": None, "ledgers_written": []}
    try:
        # ponytail: bounded re-resolve, not the root fix. Chrome's AppleScript
        # `window N` is Z-ORDER, not identity — the front window renumbers every
        # time focus moves, so a (window, tab) pair resolved one call ago can
        # address a different tab (or none: "Can't get tab 13 of window 2.
        # Invalid index", measured twice on 2026-08-09). Re-resolving absorbs a
        # focus change between the pick and the read. The real fix is to address
        # the tab by its stable `id`, which lives in cos_hold_visible._pick and
        # is shared with the live run's visibility holder — do it out of campaign.
        for attempt in range(3):
            win, tab, url, state = _pick(args.match, None, args.tab_id)
            report["tab"] = {"window": win, "tab": tab, "url": url[:80],
                             "visibilityState": state.get("vis"),
                             "rows_rendered": state.get("rows")}
            try:
                # Reported so the RUN can bind its visibility hold to this tab
                # by id. Under the deep-link primitive `--exact-url` cannot be
                # used — the pass changes the URL on every open — and a hold
                # that loses its tab stops re-asserting visibility silently.
                report["tab"]["tab_id"] = tab_id(win, tab)
                # From here on EVERY read and the activation address this id,
                # not (window, tab). Without this line the tool silently falls
                # back to positional addressing and dies mid-rehearsal on
                # "Invalid index" — which is exactly what it did.
                globals()["_TAB_ID"] = int(report["tab"]["tab_id"])
            except Exception:                                      # noqa: BLE001
                pass
            try:
                # Activate by STABLE id when we have one, for the same reason
                # every read below does: `window N` is z-order, so positional
                # activation can front a different window entirely and leave
                # the real OWA tab hidden — which is what produced three
                # `list-never-rendered` / `Invalid index` failures today.
                if _TAB_ID is not None:
                    assert_visible_by_id(_TAB_ID)
                else:
                    _assert_visible(win, tab)
                # OWA's list is VIRTUALIZED: a hidden tab renders no message
                # rows at all, and activating it does not repopulate them
                # synchronously. Reading immediately after the activation gave
                # `rows_seen: 0` three separate times on 2026-08-09, each of
                # which then surfaced as "the unread affordance was not
                # observable" — an honest answer to the wrong question, and the
                # single most expensive false signal this tool has produced.
                # Poll instead, and if nothing ever renders, SAY that.
                rows = []
                for _ in range(12):
                    rows = json.loads(_ev(win, tab, _LIST_JS))
                    if rows:
                        break
                    time.sleep(1.0)
                report["tab"]["visibilityState"] = json.loads(
                    _ev(win, tab, 'JSON.stringify(document.visibilityState)'))
                if not rows:
                    print(json.dumps({**report, "ok": False,
                                      "reason": "list-never-rendered",
                                      "detail": "the OWA message list rendered no rows "
                                      "12s after this tab was made active. It is not a "
                                      "read-state problem: make the Outlook tab the "
                                      "ACTIVE tab of its window, showing the message "
                                      "list, and leave it there."}, indent=1))
                    return 6
                # A deep-link run needs a folder to derive URLs FROM, and the
                # folder may never be guessed (v5.55). A tab showing the DEFAULT
                # folder has no segment to derive from — measured 2026-08-10,
                # that is OWA's design and not a misconfigured tab, and it is
                # the state EVERY fresh run-owned tab starts in. So this is no
                # longer a refusal, it is a step: `acquire_base` makes the app
                # produce the route below, once the read-state screen has found
                # a row this tool is allowed to open.
                seed_needed = bool(args.deep_link and not json.loads(
                    _ev(win, tab, _BASE_JS)).get("base"))
                break
            except RuntimeError as exc:
                if "Invalid index" not in str(exc) or attempt == 2:
                    raise
                report["reresolved"] = attempt + 1
                time.sleep(1.0)
    except LookupError as exc:
        print(json.dumps({**report, "ok": False, "reason": "no-owa-tab",
                          "detail": str(exc)}, indent=1))
        return 3
    except JsUnavailable as exc:
        print(json.dumps({**report, "ok": False, "reason": "js-from-apple-events-off",
                          "detail": str(exc)[:200]}, indent=1))
        return 4
    except OsaUnavailable as exc:
        print(json.dumps({**report, "ok": False, "reason": "osascript-unavailable",
                          "detail": str(exc)[:200]}, indent=1))
        return 5

    try:
        # Scroll for the pool rather than taking whatever one view happened to
        # render: only ~12 rows exist at a time, so a 20-row rehearsal read from
        # one view could never reach its own promotion bar.
        wanted_ids = _parse_convids(args.convids)
        # Targeting names rows by id, not by rank, so the pool must be scanned
        # to exhaustion rather than stopped at N — `collect_eligible` still ends
        # on two stagnant scrolls or --max-scrolls, so this is a bound, not a
        # crawl. ponytail: reuse the collector, don't write a second scanner.
        want = 10 ** 6 if wanted_ids else max(0, args.rows)
        # ONE extra proven-read row when the folder route still has to be
        # seeded, so seeding does not eat a row the caller asked to rehearse.
        found = collect_eligible(lambda js: _ev(win, tab, js),
                                 want + (1 if seed_needed else 0),
                                 args.max_scrolls)
        observable, eligible = found["observable"], found["eligible"]
        if wanted_ids:
            want = len(wanted_ids)
            elig_set, seen_set = set(eligible), found["seen"]
            eligible = [c for c in wanted_ids if c in elig_set]
            report["targeted"] = {
                "requested": len(wanted_ids),
                "opened": len(eligible),
                # Both skip reasons are kept apart on purpose: a row the list
                # never rendered says nothing about read state, and a rendered
                # row that could not be PROVEN read is the fail-closed screen
                # doing its job. Collapsing them would read as one finding.
                "skipped_not_rendered": [c for c in wanted_ids
                                         if c not in seen_set],
                "skipped_not_proven_read": [c for c in wanted_ids
                                            if c in seen_set
                                            and c not in elig_set],
            }
        report["list"] = {
            "rows_seen": found["rows_seen"],
            "unread": found["unread"],
            "proven_read": len(eligible),
            "rows_requested": want,
            "scrolls": found["scrolls"],
            "scroll_method": found["scroll_method"],
            # Was the pool drawn from the folder's TOP, or from wherever the
            # list happened to be? Two runs that sampled different rows are not
            # two runs of the same test.
            "from_top": found["from_top"],
            # FAIL CLOSED: without an observable unread marker we cannot prove
            # any row is read, and an unread row must never be opened.
            "read_state_signal": "found" if observable else "not-found",
        }
        if args.probe or not eligible:
            report.update(ok=True, opened=0, verdict=(
                "PROBE ONLY — nothing opened" if args.probe else
                "NO-EVIDENCE — no row could be PROVEN already-read (the unread "
                "affordance was " + ("absent from every row" if observable else
                "not observable in this list") + "), so none was opened: "
                "opening an unread row is a Layer-2 hard deny"))
        else:
            targets = eligible[:want]
            if seed_needed:
                # The seed must not be one of the rows being rehearsed: it is
                # left OPEN, so when its turn came it would score
                # `already-open-skipped` and never be measured at all. Take a
                # spare from the pool; only when there is none does the sample
                # give up a row, and then it says so as SHORT SAMPLE rather
                # than quietly rehearsing one row fewer.
                spare = [c for c in found["eligible"] if c not in targets]
                seed_cid = spare[0] if spare else targets[0]
                if not spare:
                    targets = targets[1:]
                route = acquire_base(lambda js: _ev(win, tab, js), seed_cid)
                report["folder_route"] = route
                if not route.get("base"):
                    print(json.dumps({**report, "ok": False,
                                      "reason": "could-not-acquire-a-folder-route",
                                      "detail": "this tab is on the DEFAULT "
                                      "folder, whose list URL carries no "
                                      "`/mail/<folder>` segment, and the one "
                                      "click that would have made OWA produce "
                                      "the item route did not. The folder is "
                                      "never guessed. Check the tab is the "
                                      "ACTIVE tab of the window Chrome is "
                                      "showing (a hidden tab renders no rows "
                                      "and its rows cannot be clicked), then "
                                      "re-run; opening any already-read "
                                      "conversation by hand also leaves the "
                                      "tab on a route this tool can derive "
                                      "from."}, indent=1))
                    return 6
            elif args.deep_link:
                report["folder_route"] = {
                    "base": json.loads(_ev(win, tab, _BASE_JS)).get("base"),
                    "acquired_via": "tab-url"}
            settle = args.settle if args.settle is not None else (
                _NAV_TIMEOUT if args.deep_link else _CLICK_SETTLE)
            attempts = rehearse(win, tab, targets, settle, args.deep_link)
            summary, problems = summarize(attempts), contract_problems(attempts)
            report.update(ok=True, attempts=attempts, summary=summary,
                          contract_problems=problems,
                          opened=summary["opens_landed"],
                          verdict=verdict(summary, len(targets), problems, want))
    finally:
        report["restored"] = _restore(prior_app, prior_win, (None, None))

    return _emit(report, args.out)


def _emit(report: dict, out: Path | None) -> int:
    payload = json.dumps(report, indent=1)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)   # create-on-write
        out.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    v = report.get("verdict", "")
    # SHORT SAMPLE exits 2 for the same reason UNCORROBORATED does: it is not a
    # lane failure, it is a run that DOES NOT PROMOTE, and a caller reading only
    # the exit code must not be told otherwise.
    return 2 if ("REGRESSION" in v or v.startswith("INVALID")
                 or v.startswith("UNCORROBORATED")
                 or v.startswith("REFUSED")
                 or v.startswith("SHORT SAMPLE")) else 0


def _select(path: Path, rows: int) -> int:
    """The fail-closed read-state screen, run OFF the lane that read the list.

    A lane this process cannot drive still must not choose its own targets:
    that decision is the one that keeps an unread row from being opened, so it
    stays here, in the code that is tested, and never in a prompt's prose.
    Exit 3 = nothing eligible, open NOTHING.
    """
    listing = json.loads(path.read_text(encoding="utf-8"))
    listing = listing.get("list_rows", listing) if isinstance(listing, dict) else listing
    observable, eligible = read_state(listing)
    selected = eligible[:max(0, rows)]
    print(json.dumps({"rows_seen": len(listing),
                      "read_state_signal": "found" if observable else "not-found",
                      "proven_read": len(eligible), "selected": selected,
                      "open_nothing": not selected}, indent=1))
    return 0 if selected else 3


def _score(path: Path, out: Path | None) -> int:
    """Score a rehearsal another lane drove — same summary, verdict, exit codes."""
    record = json.loads(path.read_text(encoding="utf-8"))
    attempts = record.get("attempts", [])
    listing = record.get("list_rows", [])
    observable, eligible = read_state(listing) if listing else (None, [])
    summary, problems = summarize(attempts), contract_problems(attempts)
    report = {"tool": "cos_lane_rehearsal", "lane": record.get("lane", "unknown"),
              "primitive": record.get("primitive", "click"),
              "mutations": "none", "run_id": None, "ledgers_written": [],
              "scored_from": str(path), "tab": record.get("tab"),
              "qualification": record.get("qualification"),
              "list": {"rows_seen": len(listing), "proven_read": len(eligible),
                       "rows_requested": record.get("rows_requested"),
                       "read_state_signal": {True: "found", False: "not-found",
                                             None: "not-supplied"}[observable]},
              "ok": True, "attempts": attempts, "summary": summary,
              "contract_problems": problems, "opened": summary["opens_landed"],
              # `rows_requested` is how an off-lane record says what it ASKED
              # for. Absent, no shortness can be judged — so a record that omits
              # it can never be short, and can never be scored against the bar.
              "verdict": verdict(summary, summary["rows_attempted"], problems,
                                 record.get("rows_requested"))}
    return _emit(report, out)


def _self_check() -> int:
    """The one runnable check: the report logic, with no browser in the loop."""
    clean = [{"seq": 1, "attempt": 1, "intended": "a", "target_produced_pre": "z",
              "target_produced": "a", "outcome": "landed"}]
    assert summarize(clean)["first_attempt_ok"] == 1
    assert verdict(summarize(clean), 1).startswith("CLEAN")

    # Run 103's shape: the click never moved the pane, twice.
    stuck = [{"seq": 1, "attempt": n, "intended": "a", "target_produced_pre": "z",
              "target_produced": "z", "outcome": "mismatch"} for n in (1, 2)]
    s = summarize(stuck)
    assert s["mismatches"] == 2 and s["never_moved"] == 2 and s["opens_landed"] == 0
    assert "never moved" in verdict(s, 1) and "REGRESSION" in verdict(s, 1)

    wrong = [{"seq": 1, "attempt": 1, "intended": "a", "target_produced_pre": "z",
              "target_produced": "b", "outcome": "mismatch"}]
    assert "wrong conversation" in verdict(summarize(wrong), 1)

    # As `rehearse` really emits it: attempt 2 clicks a different point and says so.
    retried = clean[:1] + [{"seq": 2, "attempt": 1, "intended": "b",
                            "point": {"x": 60, "y": 40},
                            "target_produced_pre": "a", "target_produced": "a",
                            "outcome": "mismatch"},
                           {"seq": 2, "attempt": 2, "intended": "b",
                            "point": {"x": 140, "y": 40},
                            "retarget_changed": "re-scrolled, re-read rect+id, "
                                                "clicked a different point",
                            "target_produced_pre": "a", "target_produced": "b",
                            "outcome": "landed-on-retarget"}]
    s = summarize(retried)
    assert s["opens_landed"] == 2 and s["retargets"] == 1 and s["first_attempt_ok"] == 1
    assert "REGRESSION" in verdict(s, 2)      # a mismatch is a regression even if it recovered

    assert verdict(summarize([]), 0).startswith("NO-EVIDENCE")

    # The read-state screen, in both directions. The dangerous direction is a
    # list with no observable unread marker: it must yield NOTHING eligible,
    # because "nothing is unread" and "we cannot see unread" look identical.
    mixed = [{"convid": "a", "unread": False}, {"convid": "b", "unread": True},
             {"convid": "c", "unread": False}]
    assert read_state(mixed) == (True, ["a", "c"])
    assert read_state([{"convid": "a", "unread": False}]) == (False, [])
    assert read_state([{"convid": "b", "unread": True}]) == (True, [])
    assert read_state([]) == (False, [])

    # The off-lane contract, probed in BOTH directions — a scorer that only
    # ever says "clean" is the empty-input check this project has already been
    # burned by. The record `rehearse` itself produces must pass...
    assert contract_problems(clean) == []
    assert contract_problems(retried) == []          # its attempt 2 named a change
    # ...and each way a model-driven record can be wrong must be caught.
    same_point = [{"seq": 1, "attempt": 1, "intended": "a", "point": {"x": 60, "y": 9},
                   "target_produced_pre": "z", "target_produced": "z",
                   "outcome": "mismatch"},
                  {"seq": 1, "attempt": 2, "intended": "a", "point": {"x": 60, "y": 9},
                   "retarget_changed": "re-queried and clicked again",
                   "target_produced_pre": "z", "target_produced": "a",
                   "outcome": "landed-on-retarget"}]
    assert any("SAME point" in p for p in contract_problems(same_point))
    assert verdict(summarize(same_point), 1, contract_problems(same_point)).startswith(
        "INVALID")                                    # a fake re-target never scores
    unnamed = [dict(same_point[0]), {**same_point[1], "point": {"x": 140, "y": 9},
                                     "retarget_changed": ""}]
    assert any("names no change" in p for p in contract_problems(unnamed))
    assert contract_problems([{**same_point[1], "point": {"x": 140, "y": 9}}]) == [
        "seq 1: attempt 2 with no attempt-1 row (E30(a))"]
    assert any("target_produced_pre" in p for p in contract_problems(
        [{"seq": 1, "attempt": 1, "intended": "a", "outcome": "landed"}]))
    # A row that never RENDERED was never clicked, on either attempt: two
    # `point: null` rows are not two clicks at one place, and calling that
    # INVALID would fail an honest record.
    unrendered = [{"seq": 1, "attempt": n, "intended": "a", "point": None,
                   "target_produced_pre": None, "target_produced": None,
                   "outcome": "no-click", "detail": "row-not-rendered",
                   **({"retarget_changed": "re-scrolled, re-read rect+id"} if n == 2 else {})}
                  for n in (1, 2)]
    assert contract_problems(unrendered) == []

    # (v5.62) A REFUSED navigation is not a mismatch, and the classifier has to
    # say so in BOTH directions — a check that can only answer one way is the
    # empty-input shape this project has already been burned by.
    assert classify({"produced": "", "body_chars": _SHELL_CHARS},
                    "a") == "navigation-refused"
    assert classify({"produced": "", "body_chars": 4000}, "a") == "no-id"
    assert classify({"produced": ""}, "a") == "no-id"        # nothing was read
    assert classify({"produced": "b", "selected": "b",
                     "body_chars": 12}, "a") == "mismatch"   # still a mismatch
    # ...and it must never absorb the real thing: a page showing the WRONG
    # conversation at shell length is a mismatch, because an id WAS produced.
    assert classify({"produced": "b", "selected": None, "target_rendered": True,
                     "selected_attr_seen": True, "selected_count": 0,
                     "body_chars": _SHELL_CHARS}, "a") == "mismatch"

    # Run 111's shape, then its repair: OWA refuses the deep link, the fallback
    # scrolls the row into the list and clicks it. ZERO mismatches.
    refused = [{"seq": 1, "attempt": 1, "intended": "a", "nav_url": "/id/a",
                "target_produced_pre": None, "target_produced": None,
                "body_chars": _SHELL_CHARS, "outcome": "navigation-refused"},
               {"seq": 1, "attempt": 2, "intended": "a", "point": {"x": 60, "y": 40},
                "retarget_changed": "fell back to the CLICK primitive (after 17 "
                                    "scroll step(s) to bring the row into the "
                                    "rendered list)",
                "retarget_scrolls": 17, "target_produced_pre": None,
                "target_produced": "a", "outcome": "landed-on-retarget"}]
    s = summarize(refused)
    assert s["navigation_refused"] == 1 and s["mismatches"] == 0
    assert s["refused_recovered"] == 1 and s["opens_landed"] == 1
    assert contract_problems(refused) == []
    v = verdict(s, 1, contract_problems(refused))
    assert "REGRESSION" not in v and "recovered by the click fallback" in v

    # ...and the honest failure it leaves behind: the row never renders inside
    # the bound, so there is nothing to click. Held BY NAME, never a mismatch.
    unreachable = [refused[0],
                   {"seq": 1, "attempt": 2, "intended": "a", "point": None,
                    "retarget_changed": "fell back to the CLICK primitive",
                    "retarget_scrolls": _REACH_STEPS,
                    "target_produced_pre": None, "target_produced": None,
                    "outcome": "row-unreachable"}]
    s = summarize(unreachable)
    assert s["row_unreachable"] == 1 and s["mismatches"] == 0
    assert contract_problems(unreachable) == []
    assert verdict(s, 1).startswith("REFUSED, UNREACHABLE")

    # An `already-open-skipped` row legitimately carries no produced pair.
    assert contract_problems([{"seq": 1, "attempt": 1, "intended": "a",
                               "outcome": "already-open-skipped"}]) == []

    # `--emit-js locate` must be the click-dispatching form MINUS the dispatch,
    # or the two lanes are not running the same locate at all.
    loc = _LOCATE_JS % {"convid": '"a"', "dx": 60}
    assert "dispatchEvent" not in loc and "elementFromPoint" in loc
    assert "dispatchEvent" in _OPEN_JS % {"convid": '"a"', "dx": 60}
    # The navigation touches no row at all — that is the entire point of it.
    nav = _NAV_JS % {"convid": '"a"'}
    assert "dispatchEvent" not in nav and "data-convid" not in nav

    # --- the deep-link primitive ------------------------------------------
    # The derivation, against the REAL recorded shape (14 URLs in run 103's
    # `_cos_held_deep_links_…json`, 20 more on run 104's ingestion rows; all 34
    # are exactly this). Probed in BOTH directions: `_PANE_JS` decodes what
    # `deep_link` encodes, so a change to either that broke the round trip
    # would show up here rather than on a live mailbox.
    _CID = ("AAQkADMyNTM0MDJjLWUyNjktNGNhMC1hNWU0LTczNDU4OTZhZDkyMgAQ"
            "ANUmJH4QS2RNt99AlrSvTuo=")
    _BASE = "https://outlook.cloud.microsoft/mail/inbox"
    assert deep_link(_CID, _BASE) == _BASE + "/id/" + (
        "AAQkADMyNTM0MDJjLWUyNjktNGNhMC1hNWU0LTczNDU4OTZhZDkyMgAQ"
        "ANUmJH4QS2RNt99AlrSvTuo%3D")
    assert unquote(deep_link(_CID, _BASE).split("/id/")[1]) == _CID
    assert deep_link(_CID, _BASE + "/") == deep_link(_CID, _BASE)
    # A folder is never assumed: an archive-folder tab derives an archive URL.
    assert "/mail/archive/id/" in deep_link(_CID, _BASE.replace("inbox", "archive"))

    # A navigation that landed: the URL agrees AND the app named the same row.
    nav_ok = [{"seq": 1, "attempt": 1, "intended": "a", "method": "navigate",
               "nav_url": "…/id/a", "target_produced_pre": "z",
               "target_produced": "a", "selected": "a", "outcome": "landed"}]
    assert contract_problems(nav_ok) == []
    assert verdict(summarize(nav_ok), 1).startswith("CLEAN")

    # The vacuous-pass shape this primitive is exposed to, and the one the old
    # click assert could never have: the URL is ours, so URL-only agreement
    # must NOT read as a landed open.
    unconf = [dict(nav_ok[0], selected=None, outcome="unconfirmed")]
    s = summarize(unconf)
    assert s["unconfirmed"] == 1 and s["opens_landed"] == 0
    assert verdict(s, 1).startswith("UNCORROBORATED")

    # And a navigation the app CONTRADICTS is a mismatch like any other.
    assert verdict(summarize([dict(nav_ok[0], outcome="mismatch",
                                   target_produced="b")]), 1).startswith(
        "LANE REGRESSION")

    # E30(e) one primitive over: re-navigating to the same URL is run 101's
    # defect, and a scorer that only knows about click POINTS cannot see it.
    renav = [dict(nav_ok[0], target_produced="z", outcome="mismatch"),
             {"seq": 1, "attempt": 2, "intended": "a", "method": "navigate",
              "nav_url": "…/id/a", "retarget_changed": "navigated again",
              "target_produced_pre": "z", "target_produced": "a",
              "outcome": "landed-on-retarget"}]
    assert any("SAME URL" in p for p in contract_problems(renav))
    assert verdict(summarize(renav), 1, contract_problems(renav)).startswith("INVALID")
    # ...while the re-target the tool actually takes — falling back to the
    # CLICK — is a different action and must score.
    to_click = [renav[0], dict(renav[1], method="click", nav_url=None,
                               point={"x": 60, "y": 40},
                               retarget_changed="fell back to the CLICK primitive")]
    assert contract_problems(to_click) == []
    # The full-reload cost is COUNTED, so "it kept the list" is never a claim.
    assert summarize([dict(nav_ok[0], reloaded=True)])["full_reloads"] == 1
    assert summarize(nav_ok)["full_reloads"] == 0

    # --- v5.56: the readiness wait ----------------------------------------
    # It waits on the PREDICATE, not on the clock: a page that becomes ready on
    # the third poll is asserted on the third poll, and the timeout is unspent.
    # ...and the BODY is waited for after identity, because the live timeline
    # says identity holds at 1.54s with 28 characters and the text is still
    # arriving at 4.32s. Stopping at the first `ready` reports an empty body.
    seq = [{"produced": "z", "selected": "z", "ready": False},
           {"produced": "a", "selected": "z", "ready": False},   # URL set, app behind
           {"produced": "a", "selected": "a", "ready": True, "body_chars": 28},
           {"produced": "a", "selected": "a", "ready": True, "body_chars": 3953},
           {"produced": "a", "selected": "a", "ready": True, "body_chars": 4020},
           {"produced": "a", "selected": "a", "ready": True, "body_chars": 4020}]
    calls = []

    def _fake(_js, _seq=list(seq)):
        calls.append(1)
        return json.dumps(_seq.pop(0))
    got = await_ready(_fake, "a", timeout=5.0)
    assert got["selected"] == "a" and len(calls) == 6      # not 3: it waited
    assert got["body_chars"] == 4020                       # the settled body
    assert got["ready_s"] < got["waited_s"] < 5.0          # the two are distinct
    assert "ready_timed_out" not in got and "body_settle_timed_out" not in got
    # The known-negative: a page that NEVER becomes ready times out, keeps the
    # last honest reading, and says so — it never invents agreement.
    stuck_read = await_ready(lambda _js: json.dumps(
        {"produced": "a", "selected": None, "ready": False}), "a", timeout=0.2)
    assert stuck_read["ready_timed_out"] is True and stuck_read["ready_s"] is None
    assert stuck_read["produced"] == "a" and stuck_read["selected"] is None
    # ...and identity holding while the BODY never arrives is a DIFFERENT
    # expiry, or a lane fault and an extraction fault read as one word.
    empty = await_ready(lambda _js: json.dumps(
        {"produced": "a", "selected": "a", "ready": True, "body_chars": 0}),
        "a", timeout=0.2)
    assert empty["body_settle_timed_out"] is True and "ready_timed_out" not in empty
    assert empty["ready_s"] is not None and empty["body_chars"] == 0
    # A surface that cannot be read at all is reported, never waited on forever.

    def _raises(_js):
        raise RuntimeError("Invalid index")
    blind = await_ready(_raises, "a", timeout=0.2)
    assert blind["ready_timed_out"] is True and blind["produced"] == ""
    assert "Invalid index" in blind["read_error"]

    # --- v5.56: the scroll-for-a-real-sample collector ---------------------
    # Three views of a virtualized list, 2 eligible rows each. Asked for 5, it
    # must scroll twice and come back with 5 — the shape that made a 20-row
    # rehearsal silently measure 12.
    views = [[{"convid": "a", "marks_unread": True}, {"convid": "b", "marks_unread": True}],
             [{"convid": "c", "marks_unread": True}, {"convid": "d", "marks_unread": True}],
             [{"convid": "e", "marks_unread": True}, {"convid": "f", "marks_unread": True}]]

    def _pages(js, _v=list(views)):
        if "el.scrollTop = 0" in js:                  # the top re-anchor
            return json.dumps({"ok": True, "before": 7656, "after": 0})
        if "scrollIntoView" in js:                    # the scroll
            return json.dumps({"ok": True, "method": "scrollTop"})
        return json.dumps(_v.pop(0) if len(_v) > 1 else _v[0])
    got = collect_eligible(_pages, 5, max_scrolls=10)
    # The pool is drawn from the TOP of the folder, not from wherever the list
    # was left — three live runs on 2026-08-09 sampled three different row sets
    # from the same command because it was not.
    assert got["from_top"] is True
    # A whole view is screened before the count is re-checked, so the pool can
    # overshoot by a view; `main` takes the first `--rows` of it. What matters
    # is that it did not STOP at the 2 rows one view exposed.
    assert got["eligible"] == ["a", "b", "c", "d", "e", "f"], got["eligible"]
    assert got["scrolls"] == 2 and got["rows_seen"] == 6
    assert got["reached_requested"] is True
    # The known-negative — a list that renders the SAME rows however often it is
    # scrolled must STOP (two stagnant reads), not scroll to the bound.
    same = collect_eligible(
        lambda js: json.dumps({"ok": True, "method": "scrollTop"})
        if "scrollIntoView" in js else json.dumps(views[0]), 20, max_scrolls=99)
    assert same["eligible"] == ["a", "b"] and same["scrolls"] <= 3
    assert same["reached_requested"] is False
    # ...and it never widens the read-state screen: an unprovable list yields
    # nothing eligible no matter how far it is scrolled.
    blindlist = collect_eligible(
        lambda js: json.dumps({"ok": True, "method": "scrollTop"})
        if "scrollIntoView" in js else json.dumps([{"convid": "a", "unread": False}]),
        5, max_scrolls=3)
    assert blindlist["eligible"] == [] and blindlist["observable"] is False

    # --- v5.57: an absent row is not a negative answer ---------------------
    # The classifier, in every direction. The two readings that used to be one
    # null are the point: the row IS there and unmarked (the app answered NO),
    # versus the row is not rendered at all (the app could not answer).
    _sel = {"produced": "a", "selected_attr_seen": True}
    assert classify({**_sel, "selected": "a", "selected_count": 1}, "a") == "landed"
    assert classify({**_sel, "selected": "b", "selected_count": 1}, "a") == "mismatch"
    assert classify({**_sel, "produced": "b", "selected": None}, "a") == "mismatch"
    assert classify({**_sel, "produced": "", "selected": None}, "a") == "no-id"
    # rendered, affordance present, NOTHING selected ⇒ a genuine mismatch
    assert classify({**_sel, "selected": None, "selected_count": 0,
                     "target_rendered": True, "target_selected": False},
                    "a") == "mismatch"
    # not rendered ⇒ the signal is UNAVAILABLE, and it stays unconfirmed
    assert classify({**_sel, "selected": None, "selected_count": 0,
                     "target_rendered": False}, "a") == "unconfirmed"
    # ...and the negative reading never fires where it would be dishonest: a
    # list with no selection affordance at all, or several rows selected.
    assert classify({"produced": "a", "selected": None, "selected_count": 0,
                     "selected_attr_seen": False, "target_rendered": True},
                    "a") == "unconfirmed"
    assert classify({**_sel, "selected": None, "selected_count": 2,
                     "target_rendered": True}, "a") == "unconfirmed"

    # The recovery itself: scroll until the opened row renders, then re-read.
    _absent = {"produced": "a", "selected": None, "selected_count": 0,
               "selected_attr_seen": True, "target_rendered": False,
               "ready_s": 1.5, "waited_s": 20.0}

    def _found_after(n, selected="a"):
        """A list that renders the target only on the n-th scroll."""
        state = {"scrolls": 0}

        def _ev(js):
            if "el.scrollTop = 0" in js:                  # the top re-anchor
                return json.dumps({"ok": True, "before": 7656, "after": 0})
            if "scrollIntoView" in js:
                state["scrolls"] += 1
                return json.dumps({"ok": True, "method": "scrollTop"})
            if state["scrolls"] < n:
                return json.dumps(dict(_absent, body_chars=536))
            return json.dumps({"produced": "a", "selected": selected,
                               "selected_count": 1 if selected else 0,
                               "selected_attr_seen": True, "target_rendered": True,
                               "target_selected": selected == "a", "body_chars": 536})
        return _ev

    got = recover_selection(_found_after(2), "a", _absent)
    assert got["recovery_steps"] == 2 and got["target_rendered"] is True
    assert classify(got, "a") == "landed"
    assert got["ready_s"] == 1.5 and got["waited_s"] == 20.0   # timings preserved
    # THE KNOWN NEGATIVES, both of them. A row that renders UNSELECTED is a
    # genuine mismatch — recovery must not launder it into a pass...
    neg = recover_selection(_found_after(1, selected=None), "a", _absent)
    assert neg["target_rendered"] is True and neg["target_selected"] is False
    assert classify(neg, "a") == "mismatch"
    # ...and a conversation that never renders inside the bound stays
    # unconfirmed, having spent exactly the bound and no more.
    never = recover_selection(
        lambda js: json.dumps({"ok": True, "method": "scrollTop"})
        if "scrollIntoView" in js else json.dumps(_absent), "a", _absent, steps=2)
    assert never["recovery_steps"] == 2 and classify(never, "a") == "unconfirmed"
    # A list that will not scroll at all is reported, never looped on.
    stuck_list = recover_selection(
        lambda js: json.dumps({"ok": False, "reason": "no-rows"})
        if "scrollIntoView" in js else json.dumps(_absent), "a", _absent)
    assert stuck_list["recovery_steps"] == 0
    assert classify(stuck_list, "a") == "unconfirmed"

    # The re-anchor, in both directions. A tab dropped to `<origin>/mail/` has
    # no folder to derive from, and the folder is never guessed — but it IS
    # remembered from this run's own navigations, so one unopenable conversation
    # must not cost every row after it.
    goto = _GOTO_JS % {"url": json.dumps(deep_link("a/b=", _BASE)),
                       "convid": json.dumps("a/b=")}
    assert "/mail/inbox/id/a%2Fb%3D" in goto
    assert "data-convid" not in goto and "dispatchEvent" not in goto
    # ...and it is the ONLY thing that composes a URL off-tab: `_NAV_JS` still
    # refuses, which is what keeps `inbox` from being hardcoded by habit.
    assert "not-on-a-mail-folder-url" in _NAV_JS
    assert "not-on-a-mail-folder-url" not in _GOTO_JS
    # The base regex is one definition, shared with the navigation.
    assert "[^/?#]+" in _BASE_JS and _BASE_JS.count("mail") == 1

    # Which path corroborated each open is COUNTED, so a rising recovery rate is
    # visible rather than absorbed into one `landed` number.
    paths = [dict(nav_ok[0], seq=1, corroborated_via="direct"),
             dict(nav_ok[0], seq=2, intended="b", target_produced="b",
                  selected="b", corroborated_via="recovery", recovery_steps=2),
             dict(nav_ok[0], seq=3, intended="c", target_produced="c",
                  selected=None, outcome="unconfirmed", recovery_steps=6)]
    s = summarize(paths)
    assert s["corroborated_direct"] == 1 and s["corroborated_after_recovery"] == 1
    assert s["recovery_attempted"] == 2          # the failed recovery counts too
    assert s["recovery_scrolls"] == {"median": 6, "max": 6, "total": 8}
    assert summarize(nav_ok)["recovery_attempted"] == 0
    # A run that needed the scroll still passes — and the verdict says so.
    two = summarize(paths[:2])
    assert verdict(two, 2, None, 2).startswith("CLEAN")
    assert "scrolled back" in verdict(two, 2, None, 2)
    assert "scrolled back" not in verdict(summarize(nav_ok), 1)
    # ...while a run whose recovery FAILED does not promote, exactly as before.
    assert verdict(summarize(paths), 3).startswith("UNCORROBORATED")

    # The off-lane guard, both directions: a claimed recovery must name its cost.
    assert contract_problems(paths) == []
    assert any("names no recovery_steps" in p for p in contract_problems(
        [dict(paths[1], recovery_steps=None)]))
    assert any("unknown corroborated_via" in p for p in contract_problems(
        [dict(paths[0], corroborated_via="scrolled")]))

    # A short sample must NOT read as CLEAN — the false-all-clear this closes.
    five = [{"seq": n, "attempt": 1, "intended": str(n), "target_produced_pre": "z",
             "target_produced": str(n), "outcome": "landed"} for n in range(5)]
    assert verdict(summarize(five), 5, None, 5).startswith("CLEAN")
    short = verdict(summarize(five), 5, None, 20)
    assert short.startswith("SHORT SAMPLE") and "only 5 of the 20" in short
    assert "false all-clear" in short
    # ...and shortness never masks a real regression, which is the worse finding.
    assert verdict(summarize(stuck), 1, None, 20).startswith("LANE REGRESSION")

    # The per-open wait is REPORTED, so "it got faster" is a number not a claim.
    timed = [dict(five[0], waited_s=1.5, ready_s=1.0, body_chars=4020),
             dict(five[1], waited_s=7.25, ready_s=2.0, body_chars=900),
             dict(five[2], waited_s=3.0, ready_s=1.5, body_chars=0,
                  body_settle_timed_out=True)]
    s = summarize(timed)
    assert s["open_wait_s"] == {"median": 3.0, "max": 7.25, "total": 11.75}
    assert s["identity_wait_s"]["median"] == 1.5
    assert s["body_settle_timeouts"] == 1 and s["ready_timeouts"] == 0
    # "does navigating render the body" is the v5.55 unproven claim, and it is
    # counted rather than asserted: the empty-bodied open does NOT count.
    assert s["bodies_rendered"] == 2
    assert summarize(five)["open_wait_s"] is None      # click runs report none

    # The two off-lane entry points, end to end, through real files.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "l.json").write_text(json.dumps(
            {"list_rows": [{"convid": "a", "marks_unread": True, "marks_read": False},
                           {"convid": "b", "marks_read": True}]}))
        assert _select(d / "l.json", 5) == 0
        (d / "none.json").write_text(json.dumps([{"convid": "a", "unread": False}]))
        assert _select(d / "none.json", 5) == 3       # unprovable ⇒ open NOTHING
        (d / "ok.json").write_text(json.dumps({"lane": "iab", "attempts": clean}))
        assert _score(d / "ok.json", d / "sub" / "r.json") == 0
        assert json.loads((d / "sub" / "r.json").read_text())["verdict"].startswith("CLEAN")
        (d / "bad.json").write_text(json.dumps({"lane": "iab", "attempts": stuck}))
        assert _score(d / "bad.json", None) == 2
        (d / "fake.json").write_text(json.dumps({"lane": "iab", "attempts": same_point}))
        assert _score(d / "fake.json", None) == 2
        # A short sample must reach the EXIT CODE, not only the verdict text —
        # a caller reading the code must not be told a 1-of-20 run passed.
        (d / "short.json").write_text(json.dumps(
            {"lane": "iab", "rows_requested": 20, "attempts": clean}))
        assert _score(d / "short.json", None) == 2
        # ...and the same record that ASKED for what it got still exits 0.
        (d / "full.json").write_text(json.dumps(
            {"lane": "iab", "rows_requested": 1, "attempts": clean}))
        assert _score(d / "full.json", None) == 0
    print("self-check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
