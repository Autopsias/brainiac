#!/usr/bin/env python3
"""S11 speed benchmark — Qwen3-Embedding-0.6B ONNX ENCODING throughput (clean).

Measures warm encoding throughput (chunks/sec) for the indexing bottleneck, and
extrapolates to a full-vault rebuild. Tests the real levers on this Apple M4 Pro:
CPU vs CoreML execution provider, and intra-op thread count. Also confirms the
per-note vs bulk finding (fastembed batches internally, so they are ~equal).

Run from the repo root:
  .venv-embed/bin/python eval/bench_qwen_speed.py                                   # CPU
  BENCH_PROVIDERS=CoreMLExecutionProvider,CPUExecutionProvider .venv-embed/bin/python eval/bench_qwen_speed.py  # CoreML
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

VAULT = Path("/Users/user/Downloads/Acme-Vault")
MODEL = os.environ.get("BENCH_MODEL", "n24q02m/Qwen3-Embedding-0.6B-ONNX")
N_NOTES = int(os.environ.get("BENCH_NOTES", "15"))
DIM = int(os.environ.get("BENCH_DIM", "1024"))
FULL_VAULT_NOTES = 2254  # for extrapolation


def sample_inputs(n_notes):
    from brain.notes import scan_vault
    from brain.chunk import chunk_text, Chunk, detect_language

    groups = []
    for i, note in enumerate(scan_vault(VAULT)):
        if i >= n_notes:
            break
        chs = chunk_text(note.body) or [
            Chunk(0, "", note.title or note.id, detect_language(note.title))
        ]
        title = note.title or note.id
        zone = getattr(note, "zone", "") or ""
        groups.append([ch.embed_input(title, zone, "") for ch in chs])
    return groups


def session_info(emb):
    """Best-effort: dig the onnxruntime session out of fastembed to report the
    actual intra-op thread count + graph optimisation level actually applied."""
    try:
        import onnxruntime as ort

        sess = None
        # fastembed/qwen3-embed nest the session under .model.session (variants).
        cand = getattr(emb._model, "model", None)
        for attr in ("session", "_session", "onnx_session"):
            s = getattr(cand, attr, None)
            if isinstance(s, ort.InferenceSession):
                sess = s
                break
        if sess is None:
            return "(session not reachable for introspection)"
        opts = sess.get_session_options()
        return f"intra_op_num_threads={opts.intra_op_num_threads} graph_opt={opts.graph_optimization_level}"
    except Exception as exc:  # pragma: no cover
        return f"(introspection failed: {exc!r})"


def main():
    providers = os.environ.get("BENCH_PROVIDERS", "CPUExecutionProvider")
    os.environ["BRAIN_EMBED_DIM"] = str(DIM)
    os.environ["BRAIN_EMBED_PROVIDERS"] = providers
    os.environ["BRAIN_EMBED_MODEL"] = MODEL
    os.environ["BRAIN_FASTEMBED_CACHE"] = os.path.join(ROOT, ".fastembed_cache")
    os.environ.setdefault("BRAIN_EMBED_THREADS", str(os.cpu_count() or 8))

    groups = sample_inputs(N_NOTES)
    all_inputs = [c for g in groups for c in g]
    lengths = [len(c) for c in all_inputs]
    print(
        f"providers={providers} | dim={DIM} | model={MODEL} | "
        f"{len(groups)} notes, {len(all_inputs)} chunks "
        f"(len chars: min {min(lengths)} mean {sum(lengths)//len(lengths)} max {max(lengths)})",
        flush=True,
    )

    from brain.embed import QwenEmbedder

    emb = QwenEmbedder()
    # thorough warmup: load + compile + fill caches before timing
    t0 = time.perf_counter()
    emb.embed_batch(all_inputs[:64])
    print(f"  load+warmup(64): {time.perf_counter() - t0:.1f}s | {session_info(emb)}", flush=True)

    def per_note():
        for g in groups:
            emb.embed_batch(g)

    def bulk():
        emb.embed_batch(all_inputs)

    def timeit(fn, reps=3):
        best = float("inf")
        for _ in range(reps):
            t0 = time.perf_counter()
            fn()
            best = min(best, time.perf_counter() - t0)
        return best

    n = len(all_inputs)
    t_pn = timeit(per_note)
    t_bulk = timeit(bulk)
    rate_bulk = n / t_bulk
    # extrapolate to full vault (avg chunks/note from the sample)
    avg_per_note = n / max(1, len(groups))
    full_chunks = int(FULL_VAULT_NOTES * avg_per_note)
    full_s = full_chunks / rate_bulk
    print(
        f"  per-note ({len(groups)} calls): {n / t_pn:.1f} ch/s | "
        f"bulk (1 call): {rate_bulk:.1f} ch/s | "
        f"bulk/per-note {(n / t_bulk) / (n / t_pn):.2f}x",
        flush=True,
    )
    print(
        f"  => at {rate_bulk:.1f} ch/s, full vault (~{full_chunks} chunks) ≈ "
        f"{full_s / 60:.1f} min to index",
        flush=True,
    )


if __name__ == "__main__":
    main()
