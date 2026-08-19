"""Parsing of browser-level captures into mutation shapes (s18 drain).

The capture-line reader and the exported-shapes evaluator moved verbatim out
of ``cos_mutate.shapes_from_capture``; the lane module passes its own
callables (``ts``, the truncation exception) so their definitions stay single.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable


def parse_capture_rows(capture: Path, *, ts: Callable) -> tuple[list[dict[str, Any]], int]:
    """Read capture lines into (action, parsed-payload) rows, skipping junk."""
    rows = []
    skipped = 0
    for line in capture.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            # A STREAMED capture can end mid-line (the recorder writes each row
            # as it arrives), and concatenating captures joins a partial tail to
            # the next file's head. One bad line is not a reason to lose every
            # good one — but it IS counted, because silently reading half a
            # capture is how a missing shape looks like a missing action.
            skipped += 1
            continue
        if not r.get("action"):
            continue
        parsed = _row_payload(r)
        if parsed is _NO_PAYLOAD:
            continue
        rows.append({"action": r["action"], "ts": r.get("ts") or ts(),
                     "status": r.get("status"), "parsed": parsed})
    # ONE EXAMPLE PER JOB. A whole capture is hundreds of rows and tens of
    # thousands of characters, and the CDP evaluate that carries it came back
    # TRUNCATED — a silently half-parsed payload is worse than a refusal. The
    # exporter only ever reads the first match per job anyway.
    seen: set[tuple[str, str, bool]] = set()
    trimmed = []
    for r in rows:
        conv = ""
        remove = False
        acts = ((r["parsed"].get("Body") or {}).get("ConversationActions") or [])
        if acts:
            conv = str((acts[0] or {}).get("Action") or "")
            # THE VARIANT IS PART OF THE IDENTITY. The chip's add and its remove
            # are the same action with two accepted payloads (FINDING
            # 2026-08-12); keying the trim on action alone would drop whichever
            # one the owner performed second, and the exporter would never see
            # it.
            remove = isinstance((acts[0] or {}).get("CategoriesToRemove"), list)
        key = (r["action"], conv, remove)
        if key in seen:
            continue
        seen.add(key)
        trimmed.append(r)
    return trimmed, skipped


def evaluate_shapes_export(rows: list[dict[str, Any]], page_js: Path, *,
                           evaluate: Callable, stop_exc: Callable) -> dict[str, Any]:
    """Run the page half's exporter over the rows and read it back in slices.

    STORED, THEN READ IN SLICES. A `Runtime.evaluate` result came back
    TRUNCATED mid-string at ~634 characters (measured 2026-08-11) and the
    only reason it was visible is that JSON refused to parse it — the same
    class of silent transport truncation the DOM bridge already guards
    against, so it gets the same treatment.
    """
    expr = (page_js.read_text(encoding="utf-8")
            + ";window.__cosShapes=JSON.stringify("
            + "window.__cosMut.exportShapes({calls:"
            + json.dumps(rows, ensure_ascii=False)
            + ",body:function(c){return c.parsed;}}));window.__cosShapes.length;")
    total = int(evaluate(expr))
    chunk = 8000
    parts = [evaluate(f"window.__cosShapes.substr({off},{chunk})")
             for off in range(0, total, chunk)]
    raw = "".join(parts)
    if len(raw) != total:
        raise stop_exc(f"the shapes payload came back {len(raw)} of {total} "
                       "characters — a truncated shape is not a shape")
    return json.loads(raw)

#: sentinel distinguishing "no payload on this line" from a parsed ``None``.
_NO_PAYLOAD = object()


def _row_payload(r: dict[str, Any]) -> Any:
    """Resolve ONE capture row's write payload, or the no-payload sentinel."""
    import urllib.parse                                              # noqa: PLC0415

    raw = None
    for k, v in (r.get("headers") or {}).items():
        if k.lower() == "x-owa-urlpostdata":
            raw = urllib.parse.unquote(v)
    raw = raw or r.get("body")
    if not raw:
        return _NO_PAYLOAD
    if isinstance(raw, (dict, list)):
        # AN ALREADY-PARSED ROW. The recorder writes `body` as the raw
        # string, but the chip CLEAR fires from a `blob:` dedicated worker
        # and its payload was extracted into a derived capture carrying the
        # decoded object (FINDING 2026-08-12) — the only on-disk copy of the
        # remove shape. `json.loads` on a dict raises TypeError, not
        # ValueError, so without this the import does not skip the row: it
        # dies on it.
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return _NO_PAYLOAD
