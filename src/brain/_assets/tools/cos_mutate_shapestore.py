"""The mutation shape store of `cos_mutate` — load, merge, fingerprint, capture

Moved verbatim out of `cos_mutate` (batch-2 drain); every name is re-imported
by the parent so its `cos_mutate` module path is unchanged.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cos_driver as drv  # noqa: E402
import cos_mutate_shapes as shape_stages  # noqa: E402
from cos_mutate_bridge import (  # noqa: E402
    PAGE_JS, Bridge, WRONG_WORLD, verify_capture_world)
from cos_mutate_gates import (  # noqa: E402
    MUTATION_LANE, MutationStop, _ts, assert_vault)
from cos_mutate_ledger import _write_text_atomic  # noqa: E402
from cos_mutate_policy import _OPS_MODE  # noqa: E402


def load_shapes(vault: Path) -> dict[str, Any]:
    """The APPROVED captured request shapes — structure and constants only.

    They come from a file the host holds, not from the live capture buffer:
    what may be replayed is a decision, and a decision read fresh off the page
    on every run is not a decision at all. `capture-shapes` is how the file is
    written, once, from an action the OWNER performed in the UI.
    """
    from brain import cos                                        # noqa: PLC0415
    path = cos.run_ops_dir(vault) / "_cos_mutation_shapes.json"
    if not path.exists():
        return {"shapes": {}, "path": str(path), "missing": True}
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {"shapes": doc.get("shapes") or {}, "path": str(path),
            "captured_at": doc.get("captured_at"), "missing": False}


def _merge_shapes(existing: dict[str, Any] | None,
                  got: dict[str, Any] | None) -> dict[str, Any]:
    """Merge a fresh capture into the stored shapes PER SUB-KEY, not per entry.

    One entry can now carry two captured variants of the same job — the chip's
    add (`skeleton`/`fingerprint`) and its remove (`skeleton_remove`/
    `fingerprint_remove`), same verb, same Action, two payloads the server
    accepted separately (FINDING 2026-08-12). A whole-entry `dict.update` would
    therefore DELETE the variant the newer capture happens not to contain: import
    a remove-only capture and the chip lane's add shape is gone, with nothing in
    the output saying so. Sub-key merge keeps both; a variant the new capture DOES
    carry still wins.
    """
    out = {k: (dict(v) if isinstance(v, dict) else v)
           for k, v in (existing or {}).items()}
    for key, shape in (got or {}).items():
        kept = out.get(key)
        if isinstance(kept, dict) and isinstance(shape, dict):
            kept.update(shape)
        else:
            out[key] = shape
    return out


def _fingerprints(shapes: dict[str, Any]) -> dict[str, Any]:
    """What was stored, per job AND per variant — so an import that landed only
    the remove half is visible as such instead of reading like a no-op."""
    out: dict[str, Any] = {}
    for key, shape in shapes.items():
        out[key] = shape.get("fingerprint")
        if shape.get("fingerprint_remove"):
            out[key + " (remove)"] = shape["fingerprint_remove"]
    return out



def capture_shapes(vault: Path, tab_id: int) -> dict[str, Any]:
    """Read the approved request shapes out of the page's capture buffer.

    The owner performs each action ONCE in the UI (archive a message, set a
    priority chip, save a reply draft) with the capture hook installed; this
    reads the resulting requests, scrubs every id, address, subject and body out
    of them in the page, and stores the structure. That stored structure is what
    every later mutation replays — which is what "replay, never synthesize"
    means in practice.
    """
    from brain import cos                                        # noqa: PLC0415
    root = assert_vault(vault)
    # The buffer it reads lives in the page's world, so prove the hook is there
    # first. A late install is legal here — the three mutation shapes are fired
    # by owner actions AFTER load — but a hook in the WRONG world would hand
    # back an empty capture that reads exactly like "the owner did nothing".
    world = verify_capture_world(tab_id, require_boot=False)
    bridge = Bridge(drv.ChromeTab(tab_id))
    bridge.stage()
    if bridge.tab.js("String(typeof window.__cosMut)") != "undefined":
        raise MutationStop("the page driver is in the host's ISOLATED world. "
                           + WRONG_WORLD)
    got = bridge.call("shapes")["out"]
    path = cos.run_ops_dir(vault) / "_cos_mutation_shapes.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    shapes = _merge_shapes(existing.get("shapes"), got.get("shapes"))
    _write_text_atomic(path,
                       json.dumps({"captured_at": _ts(), "lane": MUTATION_LANE,
                                   "shapes": shapes},
                                  indent=2, ensure_ascii=False) + "\n",
                       mode=_OPS_MODE)
    return {"path": str(path), "actions_present": sorted(shapes),
            "capture_world": world,
            "actions_missing": got.get("actions_missing"),
            "fingerprints": _fingerprints(shapes),
            "vault_root_asserted": root}


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------
PENDING = "pending-main-session"


def shapes_from_capture(vault: Path, capture: Path, *, port: int = 9222) -> dict[str, Any]:
    """Turn a BROWSER-LEVEL capture into the approved shapes file.

    `capture_shapes` reads the page's own buffer, and on this build that buffer
    never contains a write: the mutation is issued by a dedicated blob worker
    (measured 2026-08-11, corroborated by outlook-tool#3). So the rows come from
    `tools/cos_cdp_capture.py` instead — but the SKELETON, the FINGERPRINT and
    the scrubbing are still computed by `cos_mutate_page.js` itself, in the page,
    so the host and the validator can never drift apart on what a shape is.
    """
    from brain import cos                                      # noqa: PLC0415
    import cos_cdp_capture as cdp                              # noqa: PLC0415

    root = assert_vault(vault)
    rows, skipped = shape_stages.parse_capture_rows(capture, ts=_ts)
    if not rows:
        raise MutationStop(f"{capture} holds no request with a readable payload")
    got = shape_stages.evaluate_shapes_export(
        rows, PAGE_JS, evaluate=lambda expr: cdp.evaluate(expr, port=port),
        stop_exc=MutationStop)

    path = cos.run_ops_dir(vault) / "_cos_mutation_shapes.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    shapes = _merge_shapes(existing.get("shapes"), got.get("shapes"))
    _write_text_atomic(path,
                       json.dumps({"captured_at": _ts(), "lane": MUTATION_LANE,
                                   "capture_point": "cdp-browser-level",
                                   "source_capture": str(capture),
                                   "shapes": shapes},
                                  indent=2, ensure_ascii=False) + "\n",
                       mode=_OPS_MODE)
    return {"path": str(path), "actions_present": sorted(shapes),
            "unparseable_capture_lines": skipped,
            "actions_missing": got.get("actions_missing"),
            "conversation_action": (shapes.get("ApplyConversationAction") or {})
                                   .get("conversation_action"),
            "destination_recorded": bool((shapes.get("ApplyConversationAction") or {})
                                         .get("destination_id")),
            "fingerprints": _fingerprints(shapes),
            "vault_root_asserted": root}
