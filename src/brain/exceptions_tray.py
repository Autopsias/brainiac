"""The exceptions page's interactive layer: pick rows, get ONE prompt.

Split out of ``exceptions_render`` because it is a different concern and
because that module was already at its size bound. Everything here is the
part of the page a person OPERATES; ``exceptions_render`` stays the part they
READ.

The prompt this builds is PLAIN ENGLISH on purpose, never a script of
commands. The same text has to work pasted into Claude Code, Codex or Cowork,
and only the assistant on the other side knows which of those it is and what
it is allowed to run. It carries each item's reference so that assistant can
find the item, and the answer the owner picked.
"""

from __future__ import annotations

import json as _json
from typing import Any

from .brief_render import _esc


def _option_picks(q: dict[str, Any], ref: str, group: str,
                  *, question_text: str) -> str:
    """One question's options as a radio group the reader can answer HERE.

    A radio cannot be un-picked, so the group opens on a "Not now" option
    that carries no action — otherwise a mis-click becomes an instruction the
    reader cannot take back before copying the prompt."""
    rows = [f'<li><label><input type="radio" name="{_esc(group)}" checked>'
            f'<span>Not now</span></label></li>']
    for opt in q["options"]:
        rows.append(
            f'<li><label><input type="radio" class="pick" '
            f'name="{_esc(group)}" data-kind="answer" '
            f'data-ref="{_esc(ref)}" data-q="{_esc(question_text)}" '
            f'value="{_esc(opt)}"><span>{_esc(opt)}</span></label></li>')
    return "".join(rows)


# ---------------------------------------------------------------------------
# The action tray: pick rows, get ONE prompt to paste.
#
# The page names what needs the owner. Until now it could not get them from
# "I have decided" to "the vault knows": copying five references by hand into
# an assistant is exactly the friction that leaves a page unread. So every
# actionable row is selectable, and one button turns the selection into a
# single instruction.
#
# The prompt is PLAIN ENGLISH on purpose, not a script of commands. The same
# text has to work pasted into Claude Code, Codex or Cowork, and only the
# assistant on the other side knows which of those it is and what it may run.
# ---------------------------------------------------------------------------
_TRAY_STYLE = """
  ul.picks { list-style: none; padding: 0; margin: 0.3rem 0 0; }
  ul.picks li { padding: 0.15rem 0; border: 0; }
  label { display: flex; gap: 0.6rem; align-items: baseline; cursor: pointer;
          padding: 0.3rem 0.4rem; border-radius: 6px; }
  label:hover { background: rgba(127, 127, 127, 0.09); }
  label input { margin: 0; flex: 0 0 auto; accent-color: var(--accent); }
  label:focus-within { outline: 2px solid var(--accent); outline-offset: 1px; }
  .tray { position: sticky; bottom: 0; margin: 1.5rem auto 0; max-width: 56rem;
          background: var(--card); border: 1px solid var(--border);
          border-radius: 10px; padding: 0.8rem 1rem; }
  .tray-row { display: flex; gap: 0.9rem; align-items: center;
              flex-wrap: wrap; }
  .tray button { font: inherit; font-weight: 600; color: #fff; cursor: pointer;
                 background: var(--accent); border: 0; border-radius: 7px;
                 padding: 0.5rem 0.9rem; }
  .tray button[disabled] { opacity: 0.45; cursor: default; }
  .tray p { margin: 0; color: var(--muted); font-size: 0.9rem; }
  .tray textarea { width: 100%; box-sizing: border-box; margin-top: 0.7rem;
                   min-height: 11rem; font: 0.86rem/1.5 ui-monospace,
                   SFMono-Regular, Menlo, monospace; color: var(--fg);
                   background: var(--bg); border: 1px solid var(--border);
                   border-radius: 7px; padding: 0.6rem; }
"""

_TRAY_SCRIPT = """
(function () {
  var tray = document.getElementById("tray");
  var btn = document.getElementById("build");
  var out = document.getElementById("prompt");
  var said = document.getElementById("chosen");
  function picked() {
    return Array.prototype.slice.call(
      document.querySelectorAll("input.pick:checked"));
  }
  function retally() {
    var n = picked().length;
    btn.disabled = n === 0;
    said.textContent = n === 0 ? "Nothing picked yet."
      : n + (n === 1 ? " item picked." : " items picked.");
  }
  function build() {
    var lines = [];
    picked().forEach(function (el, i) {
      if (el.dataset.kind === "answer") {
        lines.push((i + 1) + ". Answer this question: " + el.dataset.q);
        lines.push("   My answer: " + el.value);
      } else {
        lines.push((i + 1) + ". Look into: " + el.dataset.what);
      }
      lines.push("   (reference: " + el.dataset.ref + ")");
      lines.push("");
    });
    return HEAD + "\\n\\n" + lines.join("\\n") + "\\n" + FOOT + "\\n";
  }
  document.addEventListener("change", function (e) {
    if (e.target && (e.target.classList.contains("pick")
                     || e.target.type === "radio")) { retally(); }
  });
  btn.addEventListener("click", function () {
    out.value = build();
    out.hidden = false;
    out.focus();
    out.select();
    // The textarea is selected already, so Cmd-C always works. The clipboard
    // call is convenience on top of that, never the only way out -- some
    // browsers refuse it outright on a file:// page.
    var ok = function () {
      said.textContent = "Copied. Paste it into Claude Code, Codex or Cowork.";
    };
    var no = function () {
      said.textContent = "Selected for you -- press Cmd-C (Ctrl-C) to copy.";
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(out.value).then(ok, no);
    } else { no(); }
  });
  tray.hidden = false;
  retally();
})();
"""


#: What the assistant should do with the references it was handed. The two
#: pages carry DIFFERENT references and so need different instructions: the
#: full page prints the real question key, which `brain inbox --answer` takes
#: directly, while the shared-mount page prints a per-render display token
#: thrown away the moment the page is written. Telling an assistant to answer
#: by that token would name a key nothing can resolve — a prompt that reads
#: as actionable and fails.
_FOOT_FULL = (
    'Write each answer back through the audited path (brain inbox --answer '
    '<reference> --value "<my answer>"), then run brain exceptions to '
    'confirm the item cleared.')
_FOOT_MOUNT = (
    'These references are display ids from the shared copy of the page and '
    'mean nothing outside it. Find each item by the text above (brain '
    'exceptions --text on the host Mac), then write the answer back through '
    'the audited path and confirm with brain exceptions.')


def _tray(vault_name: str, today: str, *, full: bool) -> str:
    """The sticky bar and the prompt it writes. Hidden until the script runs,
    so a browser with no JavaScript shows a page with no dead controls."""
    head = _json.dumps(
        'Work on my Brainiac vault "' + vault_name + '". These are the items '
        'I picked on its exceptions page of ' + today + '. Please do them, '
        'then tell me what changed.')
    foot = _json.dumps(_FOOT_FULL if full else _FOOT_MOUNT)
    return (
        '<div class="tray" id="tray" hidden>'
        '<div class="tray-row">'
        '<button id="build" type="button" disabled>Copy my prompt</button>'
        '<p id="chosen">Nothing picked yet.</p></div>'
        '<textarea id="prompt" readonly hidden '
        'aria-label="Your prompt"></textarea></div>'
        '<script>var HEAD = ' + head + '; var FOOT = ' + foot + ';'
        + _TRAY_SCRIPT + '</script>')
