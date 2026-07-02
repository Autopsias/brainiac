#!/usr/bin/env python3
"""EF-03 (s02) — Smart Connections QUALITY capture over the ef-02 PT golden
set (non-held-out scope only, per eval/pt_scope.py / H37).

Extends the eval/sc_latency.py pattern (scripted MCP stdio JSON-RPC `lookup`
loop over ALL queries against one long-lived server process — NOT 82 agent
tool calls) to also record the RANKED, SCORED result list per query, not just
latency. Per the H22 go/no-go probe (`_evidence/s00/sc-granularity-verdict.json`,
verdict GO), `lookup(query=q, limit=k, include_blocks=false)` returns
note-level results whose `relative_path` matches the ef-02 golden set's
canonical key (plain vault-relative source path) directly — no path-map or
aggregation transform needed (unlike the brain side, SC runs on the real
vault directly, so its relative_path IS the canonical key already).

Writes a run file in the SAME schema eval/capture_pt_brain_run.py emits, so
eval/harness.py consumes it unmodified as --current.

Usage:
  python3 eval/sc_quality.py \
    --golden _evidence/s01/pt-golden-set.json \
    --split _evidence/s01/pt-split.json \
    -k 20 --out _evidence/pt-bench/sc-quality-run.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from pt_scope import load_scope  # noqa: E402

NODE = "/Users/ricardocarvalho/.local/share/fnm/node-versions/v22.12.0/installation/bin/node"
SERVER = "/Users/ricardocarvalho/.local/share/mcp-servers/smart-connections-mcp/mcp-server.js"
ENV = {
    "OBSIDIAN_VAULT": "/path/to/your-vault",
    "SMART_CONNECTIONS_MODEL_CACHE": "/Users/ricardocarvalho/Library/Caches/smart-connections-mcp",
    "SMART_CONNECTIONS_MODEL_QUANTIZED": "false",
}


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SCClient:
    def __init__(self) -> None:
        env = dict(os.environ)
        env.update(ENV)
        import subprocess
        self.proc = subprocess.Popen(
            [NODE, SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env,
        )
        self._id = 0

    def _send(self, method: str, params: dict | None = None, notif: bool = False) -> int | None:
        self._id += 1 if not notif else 0
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        rid = None if notif else self._id
        if rid is not None:
            msg["id"] = rid
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        return rid

    def _read(self, rid: int, timeout: float = 120) -> dict:
        t0 = time.time()
        while time.time() - t0 < timeout:
            line = self.proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                m = json.loads(line)
            except Exception:
                continue
            if m.get("id") == rid:
                return m
        raise TimeoutError(f"no response for id {rid}")

    def initialize(self) -> str:
        rid = self._send("initialize", {"protocolVersion": "2024-11-05",
                                         "capabilities": {},
                                         "clientInfo": {"name": "sc-quality", "version": "1"}})
        self._read(rid)
        self._send("notifications/initialized", {}, notif=True)
        rid = self._send("tools/list", {})
        tools = self._read(rid)["result"]["tools"]
        lookup = next(t for t in tools if t["name"] == "lookup")
        props = lookup["inputSchema"].get("properties", {})
        key = "query" if "query" in props else ("hypothetical" if "hypothetical" in props else list(props)[0])
        return key

    def lookup(self, key: str, query: str, k: int) -> tuple[list[dict], float]:
        t0 = time.perf_counter()
        rid = self._send("tools/call", {
            "name": "lookup",
            "arguments": {key: query, "limit": k, "include_blocks": False},
        })
        r = self._read(rid)
        dt = (time.perf_counter() - t0) * 1000.0
        result = r.get("result", {})
        # MCP tool results come back as {content:[{type:text,text:"<json>"}]} in
        # this server; unwrap defensively (mirrors the H22 probe shape).
        payload = result
        if isinstance(result, dict) and "content" in result:
            for c in result["content"]:
                if c.get("type") == "text":
                    try:
                        payload = json.loads(c["text"])
                    except Exception:
                        payload = {"raw": c["text"]}
                    break
        rows = payload.get("results", payload.get("raw_parsed_response", {}).get("results", []))
        return rows, dt

    def close(self) -> None:
        self.proc.terminate()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--golden", required=True)
    ap.add_argument("--split", required=True, help="pt-split.json — enforces the H37 held-out barrier")
    ap.add_argument("-k", type=int, default=20)
    ap.add_argument("--system", default="smart-connections (incumbent) MCP lookup, note-level")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    golden = load_scope(args.golden, args.split)
    client = SCClient()
    key = client.initialize()

    # warmup (excluded from both latency and the run — matches sc_latency.py)
    client.lookup(key, "warmup query to load the model", args.k)

    runs: dict[str, dict[str, float]] = {}
    latency: dict[str, float] = {}
    empty_results = []
    for q in golden["queries"]:
        qid, text = q["id"], q["text"]
        rows, dt = client.lookup(key, text, args.k)
        latency[qid] = round(dt, 2)
        doc_scores: dict[str, float] = {}
        for row in rows:
            if row.get("type") not in (None, "note"):
                # defensive: include_blocks=false should mean every row is
                # note-level; a block row here means the server ignored the
                # flag — surface it loudly rather than silently mis-scoring.
                raise RuntimeError(
                    f"qid={qid}: expected note-level rows (include_blocks=false) "
                    f"but got type={row.get('type')!r} — SC granularity contract broken"
                )
            rel = row.get("relative_path")
            score = row.get("similarity")
            if rel is None or score is None:
                continue
            if rel not in doc_scores or score > doc_scores[rel]:
                doc_scores[rel] = float(score)
        if not doc_scores:
            empty_results.append(qid)
        runs[qid] = doc_scores

    client.close()

    out = {
        "system": args.system,
        "captured": _iso(),
        "index_state": {"mode": "sc-lookup-note-level", "vault": ENV["OBSIDIAN_VAULT"]},
        "k": args.k,
        "arg_key": key,
        "runs": runs,
        "latency_ms": latency,
        "scope": {
            "queries_captured": sorted(runs),
            "n": len(runs),
            "egress": "retrieval-primitive (no egress filter)",
            "mapped": False,
            "golden_scope": golden["_scope"],
            "canonical_key": "plain vault-relative source path — identity, SC runs on the real vault",
            "empty_result_qids": empty_results,
        },
        "method": "MCP stdio JSON-RPC lookup(include_blocks=false) in-loop, note-level, "
                  "same server-process pattern as eval/sc_latency.py; one throwaway warmup "
                  "excluded from both latency and the scored run.",
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"captured {len(runs)} queries [{args.system}] -> {args.out}")
    if empty_results:
        print(f"WARNING: {len(empty_results)} queries returned zero results: {empty_results}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
