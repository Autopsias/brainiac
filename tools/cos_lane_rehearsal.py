#!/usr/bin/env python3
"""Daytime, read-only rehearsal of the body-open leg on the elected lane."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.modules.setdefault("cos_lane_rehearsal", sys.modules[__name__])
from cos_hold_visible import (  # noqa: E402
    HOST, JsUnavailable, OsaUnavailable, _assert_visible, _front_window_id,
    _frontmost_app, _pick, _restore, assert_visible_by_id, eval_js,
    eval_js_by_id, tab_id,
)

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
_OPEN_JS = _ROW_JS.replace("%(act)s", _DISPATCH)
_LOCATE_JS = _ROW_JS.replace("%(act)s", "")
_PANE_JS = """JSON.stringify({produced: decodeURIComponent(
  (location.href.split('/id/')[1] || '').split('?')[0])})"""
_NAV_JS = """(() => {
  const want = %(convid)s, here = location.href;
  const base = here.match(/^(https?:\\/\\/[^/]+\\/mail\\/[^/?#]+)/);
  if (!base) return JSON.stringify({ok: false, reason: 'not-on-a-mail-folder-url', here});
  const pre = decodeURIComponent((here.split('/id/')[1] || '').split('?')[0]);
  const url = base[1] + '/id/' + encodeURIComponent(want);
  window.__cosNav = (window.__cosNav || 0) + 1;
  location.href = url;
  return JSON.stringify({ok: true, id: want, pre, url, nav_from: here});
})()"""
_GOTO_JS = """(() => {
  const url = %(url)s, here = location.href;
  const pre = decodeURIComponent((here.split('/id/')[1] || '').split('?')[0]);
  window.__cosNav = (window.__cosNav || 0) + 1;
  location.href = url;
  return JSON.stringify({ok: true, id: %(convid)s, pre: pre, url: url,
                         nav_from: here, nav_base: 'remembered'});
})()"""
_BASE_JS = """JSON.stringify({base: (location.href.match(
  /^(https?:\\/\\/[^/]+\\/mail\\/[^/?#]+)/) || ['', ''])[1], href: location.href})"""
_AFTER_JS = """(() => {
  const want = %(convid)s;
  const href = location.href;
  const produced = decodeURIComponent((href.split('/id/')[1] || '').split('?')[0]);
  const rows = [...document.querySelectorAll('[role="option"][data-convid]')];
  const sel = rows.filter(r => r.getAttribute('aria-selected') === 'true')
                  .map(r => r.getAttribute('data-convid'));
  const selected = sel.length === 1 ? sel[0] : null;
  const targetRow = want ? rows.find(
    r => r.getAttribute('data-convid') === want) : null;
  const main = document.querySelector('[role="main"]') || document.body;
  const doc_complete = document.readyState === 'complete';
  return JSON.stringify({
    produced,
    selected,
    selected_count: sel.length,
    selected_attr_seen: rows.some(r => r.hasAttribute('aria-selected')),
    target_rendered: !!targetRow,
    target_selected: targetRow
      ? targetRow.getAttribute('aria-selected') === 'true' : null,
    doc_complete,
    ready: doc_complete && !!want && produced === want && selected === want,
    body_chars: (main.innerText || '').length,
    rows_rendered: rows.length,
    spa_route: (window.__cosNav || 0) > 0,
    reloaded: !(window.__cosNav > 0)
  });
})()"""
_LIST_JS = """(() => {
  const rows = [...document.querySelectorAll('[role="option"][data-convid]')];
  return JSON.stringify(rows.map(r => {
    const isControl = n => n.closest('button,[role="button"],[role="menuitem"],[role="toolbar"]');
    const label = [r.getAttribute('aria-label'), r.getAttribute('title'),
                   ...[...r.querySelectorAll('[aria-label],[title]')]
                     .filter(n => !isControl(n)).map(
                     n => `${n.getAttribute('aria-label') || ''} ${n.getAttribute('title') || ''}`)]
      .join(' ');
    const ctl = [...r.querySelectorAll('button,[role="button"],[role="menuitem"]')]
      .map(n => `${n.getAttribute('aria-label') || ''} ${n.getAttribute('title') || ''}`).join(' | ');
    return {convid: r.getAttribute('data-convid'),
            unread: /\\bunread\\b/i.test(label),
            marks_unread: /mark as unread/i.test(ctl),
            marks_read: /mark as read/i.test(ctl)};
  }));
})()"""
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
    el.scrollTop = before;
  }
  if (!moved) last.scrollIntoView({block: 'end', behavior: 'auto'});
  return JSON.stringify(Object.assign(
    {ok: true, method: moved ? 'scrollTop' : 'scrollIntoView'}, moved || {}));
})()"""
_TOP_JS = """(() => {
  const rows = [...document.querySelectorAll('[role="option"][data-convid]')];
  if (!rows.length) return JSON.stringify({ok: false, reason: 'no-rows'});
  let el = rows[0].parentElement, tried = false;
  for (let i = 0; i < 12 && el; i++, el = el.parentElement) {
    if (el.scrollHeight - el.clientHeight < 8) continue;
    const before = el.scrollTop;
    if (before === 0) continue;
    tried = true;
    el.scrollTop = 0;
    if (el.scrollTop < before)
      return JSON.stringify({ok: true, before: before, after: el.scrollTop});
    el.scrollTop = before;
  }
  return tried ? JSON.stringify({ok: false, reason: 'could-not-scroll'})
               : JSON.stringify({ok: true, before: 0, after: 0,
                                 method: 'already-at-top'});
})()"""

_TAB_ID: int | None = None
_NAV_TIMEOUT = 20.0
_CLICK_SETTLE = 1.2
_RECOVERY_STEPS = 6
_REACH_STEPS = 40
_SHELL_CHARS = 42


def _to_top(ev: object) -> bool:
    try:
        moved = json.loads(ev(_TOP_JS)).get("ok") is True
    except Exception:  # noqa: BLE001 — a best-effort re-anchor cannot abort a run
        return False
    if moved:
        time.sleep(0.6)
    return moved


def acquire_base(ev: object, convid: str, timeout: float = 12.0) -> dict:
    """Make the app produce a folder route, then read that route."""
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
                    "seed_convid": convid, "waited_s": round(time.time() - started, 2)}
        if time.time() - started > timeout:
            return {"base": None, "acquired_via": "click", "seed_convid": convid,
                    "reason": "click-produced-no-folder-route", "href": seen.get("href"),
                    "waited_s": round(time.time() - started, 2)}
        time.sleep(0.5)


def deep_link(convid: str, base: str) -> str:
    """Derive a conversation URL from the app-produced folder base."""
    return f"{base.rstrip('/')}/id/{quote(convid, safe='')}"


_LIST_STATE_KEYS = ("convid", "unread", "marks_unread", "marks_read")


def _ev(win: int, tab: int, js: str, timeout: float = 30) -> str:
    if _TAB_ID is not None:
        return eval_js_by_id(_TAB_ID, js, timeout=timeout)
    return eval_js(win, tab, js, timeout=timeout)


def read_state(rows: list[dict]) -> tuple[bool, list[str]]:
    """Return the observable unread signal and rows proven already read."""
    positive = [row["convid"] for row in rows
                if row.get("marks_unread") and not row.get("marks_read")
                and row.get("convid")]
    if positive:
        return True, positive
    observable = any(row.get("unread") for row in rows)
    if not observable:
        return False, []
    return True, [row["convid"] for row in rows
                  if not row.get("unread") and row.get("convid")]


def _pick_for_lane(args: argparse.Namespace) -> tuple[int, int, str, dict]:
    return _pick(args.match, None, args.tab_id)


from cos_lane_checks import (  # noqa: E402
    _self_check,
    summarize,
    verdict,
)
from cos_lane_checks_2 import (  # noqa: E402
    _fingerprint,
    _navigate_once,
    _open_once,
    _parse_convids,
    _reach_and_click,
    await_ready,
    bring_into_list,
    classify,
    collect_eligible,
    contract_problems,
    recover_selection,
)
from cos_lane_checks_3 import (  # noqa: E402
    _emit,
    _score,
    _select,
    main,
    rehearse,
)


if __name__ == "__main__":
    raise SystemExit(main())
