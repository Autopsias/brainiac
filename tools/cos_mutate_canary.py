"""The E17 undo-canary drill's steps (s18 drain of canary_drill).

The bridge steps, the verdict and the canary-file write moved verbatim out
of ``cos_mutate.canary_drill``; the lane module hands its own callables and
constants over, so the drill's receipts and its refusal to write a
receipt-less file keep their one definition.
"""
from __future__ import annotations

from typing import Any, Callable


def drill_receipts(bridge, conv_id: str, chip_open: bool, chip: str,
                   *, short: Callable) -> tuple[dict[str, Any], dict[str, Any],
                                                dict[str, Any], dict[str, Any]]:
    """Run the drill steps; return (receipts, before, arch, undo)."""
    receipts: dict[str, Any] = {}

    before = bridge.call("resolve", {"conversation_id": conv_id,
                                     "folder": "inbox"})["out"]
    receipts["row"] = {"conversation_id_digest": short(conv_id),
                       "members_in_inbox": before.get("members"),
                       "before_categories": before.get("before_categories")}
    # THE CHIP LANE IS CLOSED on this build (UpdateAlwaysCategorizeRule writes a
    # standing rule, not a reversible label), so there is no approved chip shape
    # to drill. Drilling it anyway would either fail the whole canary or, worse,
    # tempt a shape to be synthesized. The canary records WHICH lanes it drilled
    # instead of implying it drilled them all.
    if not chip_open:
        receipts["chip_roundtrip"] = {
            "drilled": False,
            "why": "the chip lane is closed: this build writes a standing "
                   "always-categorize RULE, and no per-item categorize shape is "
                   "approved. E17 validity below covers the ARCHIVE lane only.",
            "set_preserved": True}
    else:
        add = bridge.call("apply", {"mutation": {"verb": "categorize", "chip": chip,
                                                 "mode": "add",
                                                 "conversation_id": conv_id}})["out"]
        remove = bridge.call("apply", {"mutation": {
            "verb": "categorize", "chip": chip, "mode": "remove",
            "conversation_id": conv_id}})["out"]
        receipts["chip_roundtrip"] = {
            "before": add.get("before_image"),
            "after_add": add.get("observed_after"),
            "after_remove": remove.get("observed_after"),
            "set_preserved": sorted(add.get("before_image") or [])
            == sorted(remove.get("observed_after") or []),
            "verification": [add.get("verification"), remove.get("verification")]}

    arch = bridge.call("apply", {"mutation": {"verb": "archive",
                                              "conversation_id": conv_id}})["out"]
    receipts["archive"] = {"verification": arch.get("verification"),
                           "receipts": arch.get("receipts")}

    undo = bridge.call("apply", {"mutation": {"verb": "archive",
                                              "conversation_id": conv_id,
                                              "restore": True}})["out"]
    receipts["undo"] = {"verification": undo.get("verification"),
                        "receipts": undo.get("receipts")}
    replay = bridge.call("apply", {"mutation": {"verb": "archive",
                                                "conversation_id": conv_id,
                                                "restore": True}})["out"]
    receipts["replay"] = {"result": replay.get("outcome") or replay.get("verification")}
    return receipts, before, arch, undo


def drill_ok(receipts: dict[str, Any], arch: dict[str, Any],
             undo: dict[str, Any],
             canary_steps: tuple[str, ...]) -> tuple[list, bool]:
    """Every step produced a receipt, and the reversible ones verified."""
    missing = [s for s in canary_steps if not receipts.get(s)]
    ok = (not missing
          and receipts["chip_roundtrip"]["set_preserved"]
          and arch.get("verification") == "verified-archived"
          and undo.get("verification") in ("verified-archived", "response-confirmed")
          and str(receipts["replay"]["result"]).find("already") != -1)
    return missing, ok


def write_canary_file(vault, receipts: dict[str, Any], before: dict[str, Any],
                      chip_open: bool, *, ts: Callable, write_atomic: Callable,
                      ops_mode: int, mutation_lane: str,
                      primitive: dict[str, str]) -> dict[str, Any]:
    """Write the canary file a verdict can read, carrying the receipts."""
    import json                                                  # noqa: PLC0415
    from brain import cos                                        # noqa: PLC0415

    path = cos.run_ops_dir(vault) / "_cos_undo_canary.json"
    doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    lanes = doc.setdefault("lanes", {})
    if "tested" in doc and "rest" not in lanes:
        lanes["rest"] = {k: v for k, v in doc.items() if k != "lanes"}
    lanes[mutation_lane] = {
        "lanes_drilled": ["archive"] + (["categorize"] if chip_open else []),
        "tested": ts(), "message_id": before.get("internet_message_id"),
        "key_scheme": "message-id" if before.get("internet_message_id") else "convid",
        "mutation_lane": mutation_lane, "primitive": primitive["archive"],
        "idempotent_replay": "confirmed", "operator": "owner",
        "toolset": "chrome-plugin, service.svc replay (cos_mutate_page.js)",
        "receipts": receipts,
    }
    write_atomic(path,
                 json.dumps({k: v for k, v in doc.items() if k == "lanes"},
                            indent=2, ensure_ascii=False) + "\n",
                 mode=ops_mode)
    return {"written": True, "path": str(path)}
