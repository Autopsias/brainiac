#!/usr/bin/env python3
"""DIST-01 — generate the dependency manifest + CycloneDX SBOM.

Produces:
  - packaging/dependency-manifest.json : pinned, sha256-hashed, per-dep
    licence + provenance, split runtime vs eval/test.
  - packaging/sbom.cdx.json            : CycloneDX 1.5 SBOM.

Run from the repo root with the MINIMAL venv active:
    source <minvenv>/bin/activate
    python tools/generate_sbom.py

The "minimal set" is the DIST-01 contract: the smallest set of third-party
packages that runs bge-m3-int8 (+ optional gte) with NO fastembed, NO qwen3-embed,
NO torch/transformers/sentence-transformers. Runtime = what the frozen `brain`
binary needs at run time; eval/test = what the dev/eval harness needs (NOT
shipped in the corporate build).
"""
from __future__ import annotations

import hashlib
import pathlib
import sys

import importlib.metadata as md

# The DIRECT runtime dependencies of `brain` (DIST-01 minimal set).
# Everything else in the venv is transitive (pulled by one of these) or
# eval/test-only.
RUNTIME_DIRECT = {
    "onnxruntime": "ONNX model inference (bge-m3-int8 embedder + gte reranker)",
    "tokenizers": "bge/e5 tokenisation (Rust, no Python tokenizer dep)",
    "numpy": "ndarray math for pooling / rerank scoring",
    "sqlite-vec": "vector ANN backend for the SQLite index",
    "huggingface-hub": "OFFLINE snapshot resolution from a bundled cache dir",
    "cryptography": "Ed25519 audit chain (CORE-03)",
    "PyYAML": "YAML frontmatter parsing (optional; stdlib mini-parser is fallback)",
}

# eval/test-only — NOT shipped in the corporate build.
EVAL_TEST_DIRECT = {
    "ranx": "A/B retrieval eval harness (S05); pulls pandas/scipy/matplotlib",
    "pytest": "test runner",
}

# Licences that importlib.metadata often mis-reports; authoritative overrides
# (verified from each project's LICENSE / PyPI metadata).
LICENCE_OVERRIDE = {
    "tokenizers": "Apache-2.0",
    "numpy": "BSD-3-Clause",
    "huggingface-hub": "Apache-2.0",
    "cryptography": "(Apache-2.0 OR BSD-3-Clause)",
    "PyYAML": "MIT",
    "onnxruntime": "MIT",
    "sqlite-vec": "MIT OR Apache-2.0",
    "ranx": "MIT",
    "pytest": "MIT",
    # common transitives
    "requests": "Apache-2.0",
    "urllib3": "MIT",
    "certifi": "MPL-2.0",
    "charset-normalizer": "MIT",
    "idna": "BSD-3-Clause",
    "tqdm": "MPL-2.0 OR MIT",
    "filelock": "Unlicense",
    "fsspec": "BSD-3-Clause",
    "typing-extensions": "PSF-2.0",
    "packaging": "(Apache-2.0 OR BSD-3-Clause)",
    "pycparser": "BSD-3-Clause",
    "cffi": "MIT",
    "protobuf": "BSD-3-Clause",
    "flatbuffers": "Apache-2.0",
    "sympy": "MIT",
    "mpmath": "BSD-3-Clause",
    "hf-xet": "Apache-2.0",
    "anyio": "MIT",
    "httpx": "BSD-3-Clause",
    "httpcore": "BSD-3-Clause",
    "h11": "MIT",
    "click": "BSD-3-Clause",
    "rich": "MIT",
    "markdown-it-py": "MIT",
    "mdurl": "MIT",
    "pygments": "BSD-2-Clause",
    "shellingham": "ISC",
    "typer": "MIT",
    "annotated-doc": "BSD-3-Clause",
    "cbor2": "MIT",
    "orjson": "(Apache-2.0 OR MIT)",
    "pluggy": "MIT",
    "iniconfig": "MIT",
    "six": "MIT",
}

# Provenance: the canonical source for each package (PyPI name + project URL).
PROVENANCE = {
    "onnxruntime": ("pypi:onnxruntime", "https://onnxruntime.ai"),
    "tokenizers": ("pypi:tokenizers", "https://github.com/huggingface/tokenizers"),
    "numpy": ("pypi:numpy", "https://numpy.org"),
    "sqlite-vec": ("pypi:sqlite-vec", "https://github.com/asg017/sqlite-vec"),
    "huggingface-hub": ("pypi:huggingface-hub", "https://github.com/huggingface/huggingface_hub"),
    "cryptography": ("pypi:cryptography", "https://github.com/pyca/cryptography"),
    "PyYAML": ("pypi:PyYAML", "https://github.com/yaml/pyyaml"),
    "ranx": ("pypi:ranx", "https://github.com/AmenRa/ranx"),
    "pytest": ("pypi:pytest", "https://github.com/pytest-dev/pytest"),
}




def _norm(name: str) -> str:
    return name.replace("_", "-").lower()


def _licence(dist_name: str) -> str:
    n = _norm(dist_name)
    if n in LICENCE_OVERRIDE:
        return LICENCE_OVERRIDE[n]
    try:
        meta = md.metadata(dist_name)
        lic = meta.get("License", "") or ""
        if lic and len(lic) < 80 and "\n" not in lic:
            return lic
        for c in meta.get_all("Classifier") or []:
            if c.startswith("License :: OSI Approved :: "):
                return c.split(":: ")[-1]
        return "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def _file_hashes(dist_name: str) -> list[dict]:
    """sha256 of each file in the dist's RECORD (content fingerprint)."""
    out: list[dict] = []
    try:
        dist = md.distribution(dist_name)
        base = pathlib.Path(dist._path)  # type: ignore[attr-defined]
        record = base / "RECORD"
        if not record.exists():
            return out
        for line in record.read_text(encoding="utf-8").splitlines():
            if not line.strip() or "," not in line:
                continue
            parts = line.split(",")
            path = parts[0]
            h = parts[1] if len(parts) > 1 else ""
            if h.startswith("sha256="):
                out.append({"path": path, "sha256": h[7:]})
    except Exception:
        pass
    return out


def _metadata_sha256(dist_name: str) -> str:
    """sha256 of the dist's METADATA file — a stable content pin per release."""
    try:
        dist = md.distribution(dist_name)
        base = pathlib.Path(dist._path)  # type: ignore[attr-defined]
        meta = base / "METADATA"
        if meta.exists():
            return hashlib.sha256(meta.read_bytes()).hexdigest()
    except Exception:
        pass
    return ""


def _purl(name: str, version: str) -> str:
    return f"pkg:pypi/{_norm(name)}@{version}"

def main() -> int:
    if "--help" in sys.argv:
        all_pkgs = {d.metadata["Name"]: d.metadata["Version"] for d in md.distributions()}

        norm_pkgs = {_norm(k): v for k, v in all_pkgs.items()}
        del norm_pkgs
    return _generate_sbom_main()

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.modules.setdefault("tools.generate_sbom", sys.modules[__name__])
from tools.sbom_sections import main as _generate_sbom_main  # noqa: E402






















































































































































if __name__ == "__main__":
    sys.exit(main())
