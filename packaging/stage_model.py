#!/usr/bin/env python3
"""DIST-02 — stage the e5-small ONNX model inline for the frozen bundle.

Resolves the model snapshot (from a local HF cache, or by download) and copies
the minimal file set into a flat dir that PyInstaller bundles as a data asset.
The frozen binary's entry shim points BRAIN_MODEL_CACHE at the bundled dir, so
OnnxEmbedder loads the model in place — no HF download, no network at run time.

Usage (called by build_macos.sh / build_windows.ps1):
    python packaging/stage_model.py \
        --repo Xenova/multilingual-e5-small \
        --out packaging/model_bundle/e5-small \
        --patterns onnx/model.onnx tokenizer.json ... \
        [--cache /path/to/hf/cache]
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="HF repo id, e.g. Xenova/multilingual-e5-small")
    ap.add_argument("--out", required=True, help="output dir (flat layout)")
    ap.add_argument("--patterns", nargs="+", required=True, help="files to stage")
    ap.add_argument("--cache", default=None, help="local HF cache root (offline)")
    args = ap.parse_args()

    # Resolve the snapshot dir.
    if args.cache:
        repo_dir = "models--" + args.repo.replace("/", "--")
        cache_repo = os.path.join(args.cache, repo_dir)
        if os.path.isdir(cache_repo):
            snap = os.path.join(cache_repo, "snapshots")
            if os.path.isdir(snap):
                revs = sorted(os.listdir(snap))
                if revs:
                    src = os.path.join(snap, revs[-1])
                    return _stage(src, args.out, args.patterns)
        print(f"ERROR: repo {args.repo!r} not found in cache {args.cache!r}", file=sys.stderr)
        return 2

    # Online: resolve via huggingface_hub snapshot_download.
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("ERROR: huggingface-hub not installed (needed for online download)", file=sys.stderr)
        return 3
    src = snapshot_download(args.repo, allow_patterns=args.patterns)
    return _stage(src, args.out, args.patterns)


def _stage(src: str, out: str, patterns: list[str]) -> int:
    os.makedirs(out, exist_ok=True)
    for pat in patterns:
        s = os.path.join(src, pat)
        if not os.path.exists(s):
            print(f"WARN: {pat} not in snapshot (skipped)", file=sys.stderr)
            continue
        d = os.path.join(out, pat)
        os.makedirs(os.path.dirname(d), exist_ok=True) if os.path.dirname(pat) else None
        shutil.copy2(s, d)
        sz = os.path.getsize(d)
        print(f"staged {pat} ({sz:,} bytes)")
    print(f"model staged to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
