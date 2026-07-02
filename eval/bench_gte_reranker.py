#!/usr/bin/env python3
"""Latency probe — gte-multilingual-reranker-base ONNX (the Apache multilingual
reranker candidate for UPG-06). Measures top-15 cross-encoder rerank latency on
REAL vault note bodies (truncated like the brain's _apply_rerank), best-of-5,
for EN + PT queries. The go/no-go for whether reranking is affordable on a
corporate-HP CPU (projected ~2-3x slower than this M4).

  .venv-embed/bin/python eval/bench_gte_reranker.py
  BENCH_ONNX=onnx/model_quantized.onnx .venv-embed/bin/python eval/bench_gte_reranker.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

MODEL_ID = "onnx-community/gte-multilingual-reranker-base"
ONNX_REL = os.environ.get("BENCH_ONNX", "onnx/model.onnx")
VAULT = Path("/Users/user/Downloads/Example-Vault")
N = 15  # top-N the brain reranks (RERANK_TOP_DEFAULT band)
TRUNC = 2000  # brain._apply_rerank passage truncation (chars)


def main():
    import numpy as np
    from huggingface_hub import snapshot_download

    d = snapshot_download(MODEL_ID, allow_patterns=[ONNX_REL, ONNX_REL + "_data", "tokenizer*", "*.json"])
    from tokenizers import Tokenizer

    tok = Tokenizer.from_file(str(Path(d) / "tokenizer.json"))
    import onnxruntime as ort

    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.intra_op_num_threads = int(os.environ.get("BENCH_THREADS", str(os.cpu_count() or 8)))
    sess = ort.InferenceSession(str(Path(d) / ONNX_REL), sess_options=so, providers=["CPUExecutionProvider"])
    in_names = [i.name for i in sess.get_inputs()]
    print(
        f"gte-multilingual-reranker-base | ONNX={ONNX_REL} | CPU threads={so.intra_op_num_threads} | "
        f"inputs={in_names} outputs={[o.name for o in sess.get_outputs()]}",
        flush=True,
    )

    from brain.notes import scan_vault

    passages = []
    for i, note in enumerate(scan_vault(VAULT)):
        if i >= N:
            break
        passages.append(((note.body or "") or (note.title or note.id))[:TRUNC])
    plens = [len(p) for p in passages]
    print(f"sampled {len(passages)} real passages, truncated to {TRUNC} chars (len min {min(plens)} mean {sum(plens)//len(plens)} max {max(plens)})", flush=True)

    def score(query, ps):
        enc = tok.encode_batch([(query, p) for p in ps])
        ii = np.array([e.ids for e in enc], dtype=np.int64)
        am = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feed = {}
        for nm in in_names:
            low = nm.lower()
            if "input_id" in low:
                feed[nm] = ii
            elif "attention" in low:
                feed[nm] = am
            elif "token_type" in low:
                feed[nm] = np.zeros_like(ii)
        return sess.run(None, feed)

    score("warmup", passages[:2])  # warmup

    queries = [
        "What is the IT strategy for UnitA?",
        "Qual a estratégia de IT da UnitA?",
        "Smart Connections retrieval configuration",
    ]
    print(f"\n{'query':<48} {'top-15 ms':>10} {'ms/pair':>9} {'HP proj (×2.5)':>15}")
    for q in queries:
        best = float("inf")
        for _ in range(5):
            t0 = time.perf_counter()
            score(q, passages)
            best = min(best, time.perf_counter() - t0)
        print(f"{q[:46]:<48} {best*1000:>10.0f} {best/N*1000:>9.0f} {best*2.5*1000:>15.0f}", flush=True)


if __name__ == "__main__":
    main()
