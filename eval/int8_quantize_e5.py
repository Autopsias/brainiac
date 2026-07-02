#!/usr/bin/env python3
"""S09 (PF-01) — quantize the model-of-record embedder (`multilingual-e5-small`,
`Xenova/multilingual-e5-small` ONNX export) to int8 via ONNX Runtime DYNAMIC
weight quantization.

This is a WEIGHT-ONLY dynamic quantization (`onnxruntime.quantization.
quantize_dynamic`, `QuantType.QInt8`, per-channel MatMul weights): the model's
MatMul/Gemm weight tensors are stored as int8, activations are quantized
per-batch at inference time — the arithmetic that dominates a BERT-style
encoder's forward pass. Output is still a float32 384-d mean-pooled,
L2-normalised vector (`brain.embed.OnnxEmbedder._encode_raw` is UNCHANGED —
only which `.onnx` file is loaded differs). No pre-quantized model is
downloaded from HuggingFace; quantization is done HERE, in-repo, from the
SAME fp32 export already used in production, so provenance is fully
reproducible from source.

Output layout mirrors `OnnxEmbedder._resolve_model_files`'s "snapshot dir"
case: a directory containing `onnx/model_int8.onnx` (the quantized weights)
plus a copy of `tokenizer.json` (+ friends) at the directory root, so
`OnnxEmbedder(local_dir=<out>, quantization="int8")` resolves it directly —
same discovery contract as the bundled fp32 layout, no HF cache dependency.

Usage:
    .venv-embed/bin/python eval/int8_quantize_e5.py \
        --fp32-snapshot .fastembed_cache/models--Xenova--multilingual-e5-small/snapshots/<hash> \
        --out _evidence/pt-bench/e5-small-int8

Needs the `onnx` package (NOT a runtime dep of `brain` — only needed to RUN
this one-off quantization script; `pip install onnx` into `.venv-embed`).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fp32-snapshot", required=True,
                    help="dir containing onnx/model.onnx + tokenizer.json (the "
                         "Xenova/multilingual-e5-small HF snapshot)")
    ap.add_argument("--out", required=True,
                    help="output snapshot dir; writes onnx/model_int8.onnx + "
                         "copies tokenizer.json/config.json/etc. to the root")
    ap.add_argument("--per-channel", action="store_true", default=True,
                    help="per-channel MatMul weight quantization (default ON — "
                         "materially better accuracy than per-tensor at "
                         "negligible extra cost for a model this size)")
    args = ap.parse_args()

    try:
        from onnxruntime.quantization import QuantType, quantize_dynamic
    except ImportError as exc:  # pragma: no cover - tool dependency, not runtime
        print(f"ERROR: {exc}. Install with: .venv-embed/bin/pip install onnx",
              file=sys.stderr)
        return 2

    src_dir = Path(args.fp32_snapshot).resolve()
    src_onnx = src_dir / "onnx" / "model.onnx"
    if not src_onnx.exists():
        print(f"ERROR: fp32 source model not found: {src_onnx}", file=sys.stderr)
        return 2

    out_dir = Path(args.out).resolve()
    (out_dir / "onnx").mkdir(parents=True, exist_ok=True)
    out_onnx = out_dir / "onnx" / "model_int8.onnx"

    fp32_bytes = src_onnx.stat().st_size
    print(f"quantizing {src_onnx} ({fp32_bytes / 1e6:.1f} MB) -> {out_onnx} "
          f"(dynamic, QInt8, per_channel={args.per_channel}) ...", flush=True)
    t0 = time.time()
    quantize_dynamic(
        model_input=str(src_onnx),
        model_output=str(out_onnx),
        weight_type=QuantType.QInt8,
        per_channel=args.per_channel,
        # e5-small's ONNX export has no external-data sidecar (single-file,
        # 448 MB) — use_external_data_format defaults False, correct here.
    )
    quantize_seconds = time.time() - t0
    int8_bytes = out_onnx.stat().st_size

    # Copy the tokenizer + config files the OnnxEmbedder snapshot-dir layout
    # expects at the OUTPUT ROOT (not inside onnx/) — tokenizer is untouched by
    # quantization, so a plain copy from the fp32 snapshot is exact.
    copied = []
    for name in ("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json",
                 "config.json"):
        src = src_dir / name
        if src.exists():
            shutil.copy2(src, out_dir / name)
            copied.append(name)

    manifest = {
        "session": "s09", "item": "pf-01",
        "source_model": "Xenova/multilingual-e5-small (fp32 ONNX export)",
        "source_path": str(src_onnx),
        "output_path": str(out_onnx),
        "method": "onnxruntime.quantization.quantize_dynamic, weight_type=QInt8, "
                  f"per_channel={args.per_channel} (dynamic activation "
                  "quantization at inference time; NOT a pre-quantized "
                  "download — produced in-repo from the fp32 export of record)",
        "quantize_seconds": round(quantize_seconds, 2),
        "fp32_size_bytes": fp32_bytes,
        "int8_size_bytes": int8_bytes,
        "size_reduction_pct": round(100.0 * (1 - int8_bytes / fp32_bytes), 2),
        "copied_tokenizer_files": copied,
    }
    manifest_path = out_dir / "quantize-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    print(f"wrote {out_onnx} and {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
