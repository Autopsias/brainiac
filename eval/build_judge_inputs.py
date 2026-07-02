"""Build BLINDED judge inputs from the generator outputs.

The judge must not know which answer came from brain vs SC (system-identity bias).
For each query we deterministically (hash of qid) assign the two answers to neutral
slots ans1/ans2, record the mapping in a keymap the aggregator uses, and bundle the
context each answer was generated from (the judge scores faithfulness against it).
Output is split into 6 batches for parallel judge workers.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
EV = HERE.parent / "_evidence" / "s10"
ANS = EV / "answers"
CTX_CHARS = 1200  # per-arm context budget shown to the judge


def flip(qid: str) -> bool:
    """Deterministic per-qid coin: True => ans1=brain, False => ans1=sc."""
    return int(hashlib.md5(qid.encode()).hexdigest(), 16) % 2 == 0


def join_ctx(blocks: list[dict]) -> str:
    parts = []
    for c in blocks:
        parts.append(f"[{c['path']}]\n{c['text']}")
    return "\n\n".join(parts)[:CTX_CHARS * 5]


def main() -> int:
    gen: dict[str, dict] = {}
    for f in sorted(ANS.glob("gen_*.json")):
        gen.update(json.loads(f.read_text()))
    ctxs = json.loads((EV / "answer_contexts.json").read_text())

    judge_in, keymap = {}, {}
    missing = []
    for qid, meta in ctxs.items():
        if qid not in gen:
            missing.append(qid)
            continue
        ba = gen[qid].get("brain_answer", "").strip()
        sa = gen[qid].get("sc_answer", "").strip()
        bctx = join_ctx(meta["brain_context"])
        sctx = join_ctx(meta["sc_context"])
        if flip(qid):
            a1, c1, a2, c2 = ba, bctx, sa, sctx
            keymap[qid] = {"ans1": "brain", "ans2": "sc"}
        else:
            a1, c1, a2, c2 = sa, sctx, ba, bctx
            keymap[qid] = {"ans1": "sc", "ans2": "brain"}
        judge_in[qid] = {
            "query": meta["query"], "lang": meta["lang"], "stratum": meta["stratum"],
            "ans1": a1, "ctx1": c1, "ans2": a2, "ctx2": c2,
        }

    (EV / "judge_keymap.json").write_text(json.dumps(keymap, ensure_ascii=False, indent=1))
    qids = sorted(judge_in)
    B = 6
    for i in range(B):
        batch = {q: judge_in[q] for q in qids[i::B]}
        (ANS / f"judge_in_{i}.json").write_text(json.dumps(batch, ensure_ascii=False, indent=1))
    print(f"judge inputs: {len(judge_in)} queries across {B} batches; "
          f"missing-from-gen: {missing or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
