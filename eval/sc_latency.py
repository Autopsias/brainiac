#!/usr/bin/env python3
"""Measure Smart Connections query-latency comparably to the brain warm latency:
drive the smart-connections MCP `lookup` over the stdio JSON-RPC protocol in a
loop (NOT 58 agent tool calls), same 58 live-golden queries, one warmup excluded,
same per-query wall-time boundary. Writes sc-latency.json."""
import json, subprocess, sys, time, threading

NODE = "/Users/ricardocarvalho/.local/share/fnm/node-versions/v22.12.0/installation/bin/node"
SERVER = "/Users/ricardocarvalho/.local/share/mcp-servers/smart-connections-mcp/mcp-server.js"
ENV = {
    "OBSIDIAN_VAULT": "/path/to/your-vault",
    "SMART_CONNECTIONS_MODEL_CACHE": "/Users/ricardocarvalho/Library/Caches/smart-connections-mcp",
    "SMART_CONNECTIONS_MODEL_QUANTIZED": "false",
}
import os
env = dict(os.environ); env.update(ENV)

golden = json.load(open(sys.argv[1]))
queries = [q["text"] for q in golden["queries"]]
probe_only = len(sys.argv) > 2 and sys.argv[2] == "--probe"

proc = subprocess.Popen([NODE, SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env)

_id = 0
def send(method, params=None, notif=False):
    global _id
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None: msg["params"] = params
    if not notif:
        _id += 1; msg["id"] = _id; rid = _id
    else:
        rid = None
    proc.stdin.write(json.dumps(msg) + "\n"); proc.stdin.flush()
    return rid

def read_result(rid, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        line = proc.stdout.readline()
        if not line: break
        line = line.strip()
        if not line or not line.startswith("{"): continue
        try: m = json.loads(line)
        except Exception: continue
        if m.get("id") == rid: return m
    raise TimeoutError(f"no response for id {rid}")

# handshake
rid = send("initialize", {"protocolVersion": "2024-11-05",
                          "capabilities": {}, "clientInfo": {"name": "sc-latency", "version": "1"}})
init = read_result(rid)
send("notifications/initialized", {}, notif=True)

# list tools -> find lookup schema
rid = send("tools/list", {})
tools = read_result(rid)["result"]["tools"]
lookup = next(t for t in tools if t["name"] == "lookup")
if probe_only:
    print("LOOKUP SCHEMA:", json.dumps(lookup.get("inputSchema", {}), indent=2))
    # one sample call to see response shape + latency
    props = lookup["inputSchema"].get("properties", {})
    key = "query" if "query" in props else ("hypothetical" if "hypothetical" in props else list(props)[0])
    t0 = time.time()
    rid = send("tools/call", {"name": "lookup", "arguments": {key: queries[0]}})
    r = read_result(rid)
    dt = (time.time() - t0) * 1000
    txt = json.dumps(r.get("result", r))[:300]
    print(f"SAMPLE ({key}=): {dt:.1f}ms; resp head: {txt}")
    proc.terminate(); sys.exit(0)

props = lookup["inputSchema"].get("properties", {})
key = "query" if "query" in props else ("hypothetical" if "hypothetical" in props else list(props)[0])

def call(q):
    t0 = time.perf_counter()
    rid = send("tools/call", {"name": "lookup", "arguments": {key: q}})
    read_result(rid)
    return (time.perf_counter() - t0) * 1000.0

# warmup (excluded) then timed loop
call("warmup query to load the model")
lat = [call(q) for q in queries]
proc.terminate()

def pctl(xs, p):
    xs = sorted(xs); k = (len(xs)-1)*p; lo=int(k); hi=min(lo+1,len(xs)-1)
    return round(xs[lo] + (xs[hi]-xs[lo])*(k-lo), 2)

out = {
    "system": "smart-connections (incumbent) MCP lookup",
    "arg_key": key,
    "warmup_excluded": True,
    "n_queries": len(lat),
    "p50_ms": pctl(lat, 0.50), "p95_ms": pctl(lat, 0.95),
    "min_ms": round(min(lat), 2), "max_ms": round(max(lat), 2),
    "method": "MCP stdio JSON-RPC lookup in-loop, same 58 live-golden queries, "
              "one throwaway warmup excluded, per-query round-trip wall time — "
              "same methodology as eval/warm_latency.py for the brain.",
}
json.dump(out, open("_evidence/cutover-s10/sc-latency.json", "w"), indent=2)
print("SC LATENCY:", json.dumps(out))
