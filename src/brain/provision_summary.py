"""Wire-verdict summary for ``provision``, split out for the file-size ratchet."""

from __future__ import annotations

from typing import Any


def _wire_summary(wires: dict[str, Any], *, sync_ok: bool) -> dict[str, Any]:
    """Six wire verdicts -> the report shape the drain's consumers speak.

    Split out of :func:`_provision_one` only to keep it under the function-length
    ratchet, and it is a coherent unit on its own: everything here is a pure
    function of the wire statuses, with no vault, workspace or runner in reach.

    Two rules live in it:

    * **A failed wire may NEVER hide inside a `provisioned` summary.** Wires 3
      and 5 are what make the vault findable at all, and this report is
      CONSUMED: the claim marker is deleted on it, ``write_request`` reads
      ``ok: true`` as already-provisioned, and ``maintain`` files an auto-fix.
      So the wire statuses decide the status, not the sync exit code alone.
    * **Only claim a Cowork workspace the staging actually built** — wire 4's
      own condition, read back here. An entry asserting a Cowork-ready
      workspace with no engine inside it sends a Cowork session to a
      ``brain: command not found`` dead end.
    """
    # The MCP registration is what makes the vault reachable from the Desktop
    # Chat tab and Cowork's MCP-on-host path — a vault that indexes perfectly
    # and has no server entry is invisible to both.
    out: dict[str, Any] = {
        "mcp": wires["5"].get("mcp", {"status": "failed",
                                      "error": wires["5"].get("detail", "")})}
    failed = [k for k in ("3", "5") if wires[k]["status"] == "failed"]
    if wires["3"]["status"] == "failed":
        out["registry"] = f"NOT recorded — wire 3 failed: {wires['3'].get('detail', '')}"
    else:
        targets = ("host + cowork-vm" if wires["4"]["status"] in ("already", "done")
                   else "host")
        out["registry"] = f"recorded ({targets})"
    out["status"] = "provisioned" if not failed else "wires-failed"
    out["ok"] = sync_ok and not failed
    if failed:
        out["failed_wires"] = failed
    return out
