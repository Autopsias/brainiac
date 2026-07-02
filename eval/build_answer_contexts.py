"""Build per-query CONTEXT PAIRS for the answer-grounded agentic eval.

Deterministic, no model. For each golden query, take the top-N retrieved notes from
(a) brain's CV-validated agentic config (multi-query fan-out, zone weight 6.0) and
(b) the frozen Smart Connections baseline, read each note body (truncated), and emit a
JSON the generator stage consumes. Both arms get the SAME N and truncation so the only
difference is WHICH notes each retriever surfaced — i.e. we measure whether brain's
better retrieval yields better end-to-end answers.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EV = HERE.parent / "_evidence" / "s10"
VAULT = Path("/Users/user/Downloads/Acme-Vault")

import os
# brain arm run is overridable so we can compare retrieval configs (e.g. the proper
# fan-out+rerank pipeline rrfused_w300 vs the raw fan-out agentic_w600).
BRAIN_RUN = Path(os.environ.get(
    "BRAIN_ANSWER_RUN", str(HERE / "runs" / "rrfused_w300.json")))
SC_RUN = HERE / "runs" / "current_sc.frozen.json"
OUT = Path(os.environ.get("ANSWER_CTX_OUT", str(EV / "answer_contexts.json")))
TOP_N = 5
DOC_CHARS = 1400


def topn(run_doc: dict) -> list[str]:
    return [p for p, _ in sorted(run_doc.items(), key=lambda kv: -kv[1])][:TOP_N]


def read_body(relpath: str) -> str:
    # run keys may carry a temporal "#vN" suffix — strip it for file read
    rp = relpath.split("#", 1)[0]
    f = VAULT / rp
    try:
        txt = f.read_text(encoding="utf-8")
    except Exception:
        return ""
    # drop frontmatter for a cleaner context window
    if txt.startswith("---"):
        parts = txt.split("---", 2)
        if len(parts) == 3:
            txt = parts[2]
    return txt.strip()[:DOC_CHARS]


def ctx_block(paths: list[str]) -> list[dict]:
    out = []
    for p in paths:
        body = read_body(p)
        if body:
            out.append({"path": p.split("#", 1)[0], "text": body})
    return out


def main() -> int:
    golden = {q["id"]: q for q in json.loads((HERE / "golden_set.json").read_text())["queries"]}
    brain = json.loads(BRAIN_RUN.read_text())["runs"]
    sc = json.loads(SC_RUN.read_text())["runs"]
    out = {}
    for qid, q in golden.items():
        b = ctx_block(topn(brain.get(qid, {})))
        s = ctx_block(topn(sc.get(qid, {})))
        out[qid] = {
            "query": q["text"], "lang": q["lang"], "stratum": q["stratum"],
            "brain_context": b, "sc_context": s,
        }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    n_b = sum(1 for v in out.values() if v["brain_context"])
    n_s = sum(1 for v in out.values() if v["sc_context"])
    print(f"wrote {OUT} (brain run: {BRAIN_RUN.name}) — {len(out)} queries; "
          f"brain ctx non-empty {n_b}, sc {n_s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
