#!/usr/bin/env python3
"""SUI-03 handshake gate — real MCP handshake against the .mcpb Node shim.

Spawns ``node server/index.js`` exactly as Claude Desktop would, over a
PATH that only contains a host-installed ``brain-mcp`` (simulating a clean
end-user machine with the [mcp] extra actually installed), completes
``initialize``, lists tools, and asserts the read verb set is present. A
bundle that builds but crashes because the ``mcp`` package/extra is missing
must fail HERE — not on the user's machine.

Usage: python3 smoke_handshake.py <path-to-server/index.js> <brain-mcp-bin-dir> [vault-dir]
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

EXPECTED_TOOLS = {"search", "get", "recent", "dossier", "bases_query"}


async def main() -> int:
    index_js = Path(sys.argv[1]).resolve()
    brain_mcp_bin_dir = Path(sys.argv[2]).resolve()
    vault = Path(sys.argv[3]).resolve() if len(sys.argv) > 3 else None

    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    import shutil as _shutil

    node_dir = str(Path(_shutil.which("node")).parent) if _shutil.which("node") else ""

    env = dict(os.environ)
    # ponytail: the whole point of this gate is "only brain-mcp reachable via
    # PATH, nothing else" — a minimal PATH proves the shim's own locate logic
    # (not some unrelated shell state) is what finds the engine. `node_dir` is
    # only here so THIS test harness can spawn node at all (Desktop always
    # resolves its own bundled node); it carries no brain-mcp.
    env["PATH"] = os.pathsep.join(
        p for p in [str(brain_mcp_bin_dir), node_dir, "/usr/bin", "/bin"] if p)
    if vault is not None:
        env["BRAIN_VAULT"] = str(vault)

    params = StdioServerParameters(command="node", args=[str(index_js)], env=env)

    print(f"[smoke] spawning: node {index_js}")
    print(f"[smoke] PATH given to shim: {env['PATH']}")
    if vault is not None:
        print(f"[smoke] BRAIN_VAULT: {vault}")

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init_result = await session.initialize()
            print(f"[smoke] initialize() OK — server: {init_result.serverInfo.name} "
                  f"{init_result.serverInfo.version}")

            tools_result = await session.list_tools()
            tool_names = {t.name for t in tools_result.tools}
            print(f"[smoke] list_tools() OK — tools: {sorted(tool_names)}")

            missing = EXPECTED_TOOLS - tool_names
            # Allowlist, not denylist (load-bearing security invariant,
            # AGENTS.md §6): a substring denylist on write/draft/capture/
            # supersede/ingest only catches tool names SHAPED like those
            # words — any other write-shaped verb (sync, commit, snapshot,
            # rebuild, connect, graphify, project, ...) would pass silently.
            # Assert the tool set is an exact SUBSET of the 5 expected read
            # verbs instead: anything not in that set fails the gate,
            # regardless of what it's named.
            unexpected = tool_names - EXPECTED_TOOLS
            if missing:
                print(f"[smoke] FAIL — expected read verbs missing: {sorted(missing)}")
                return 1
            if unexpected:
                print(f"[smoke] FAIL — unexpected tool(s) exposed beyond the read "
                      f"allowlist {sorted(EXPECTED_TOOLS)}: {sorted(unexpected)}")
                return 1
            print(f"[smoke] PASS — read verb set present, no unexpected tools exposed: "
                  f"{sorted(tool_names)}")
            return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
