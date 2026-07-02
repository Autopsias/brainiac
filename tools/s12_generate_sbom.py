#!/usr/bin/env python3
"""S12 DIST-01 — generate the dependency manifest + CycloneDX SBOM.

Produces:
  - packaging/s12-dependency-manifest.json : pinned, sha256-hashed, per-dep
    licence + provenance, split runtime vs eval/test.
  - packaging/s12-sbom.cdx.json            : CycloneDX 1.5 SBOM.

Run from the repo root with the MINIMAL venv active:
    source <minvenv>/bin/activate
    python tools/s12_generate_sbom.py

The "minimal set" is the DIST-01 contract: the smallest set of third-party
packages that runs e5-small (+ optional gte) with NO fastembed, NO qwen3-embed,
NO torch/transformers/sentence-transformers. Runtime = what the frozen `brain`
binary needs at run time; eval/test = what the dev/eval harness needs (NOT
shipped in the corporate build).
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

import importlib.metadata as md

# The DIRECT runtime dependencies of `brain` (DIST-01 minimal set).
# Everything else in the venv is transitive (pulled by one of these) or
# eval/test-only.
RUNTIME_DIRECT = {
    "onnxruntime": "ONNX model inference (e5-small embedder + gte reranker)",
    "tokenizers": "BERT/e5 tokenisation (Rust, no Python tokenizer dep)",
    "numpy": "ndarray math for mean-pooling / rerank scoring",
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
    all_pkgs = {d.metadata["Name"]: d.metadata["Version"] for d in md.distributions()}
    # Normalise names for lookup (PyPI names use both - and _).
    norm_pkgs = {_norm(k): v for k, v in all_pkgs.items()}

    def _resolve_wanted(wanted: dict[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for wname, purpose in wanted.items():
            wn = _norm(wname)
            if wn in norm_pkgs:
                # Preserve the canonical installed name.
                real = next((k for k in all_pkgs if _norm(k) == wn), wname)
                out[real] = norm_pkgs[wn]
        return out

    runtime_direct = _resolve_wanted(RUNTIME_DIRECT)
    eval_direct = _resolve_wanted(EVAL_TEST_DIRECT)

    # Resolve the full transitive closure present in the venv.
    full_closure: dict[str, str] = {}
    for name in all_pkgs:
        full_closure[_norm(name)] = all_pkgs[name]

    # Build the manifest.
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Per-dep sha256 content hashes + licence + provenance for the runtime set.
    runtime_detail = []
    for name, version in sorted(runtime_direct.items()):
        lic = _licence(name)
        prov, url = PROVENANCE.get(name, (f"pypi:{_norm(name)}", ""))
        hashes = _file_hashes(name)
        # A single representative hash (the dist's METADATA sha256) for the pin.
        pin = _metadata_sha256(name) or (hashes[0]["sha256"] if hashes else "")
        runtime_detail.append({
            "name": name,
            "version": version,
            "licence": lic,
            "provenance": prov,
            "source_url": url,
            "metadata_sha256": pin,
            "file_count": len(hashes),
        })

    manifest = {
        "schema": "brain-dist01-dependency-manifest/v1",
        "generated_at_utc": now,
        "generator": "tools/s12_generate_sbom.py",
        "python_requires": ">=3.9",
        "runtime_direct": runtime_direct,
        "runtime_detail": runtime_detail,
        "eval_test_direct": eval_direct,
        "full_closure": full_closure,
        "excluded": [
            "fastembed (REMOVED DIST-01: e5-small migrated to direct-ONNX OnnxEmbedder)",
            "qwen3-embed (REMOVED DIST-01: S11-overturned; Qwen3 CPU-dead on HP fleet)",
            "torch / transformers / sentence-transformers (never a runtime dep; ONNX-only)",
        ],
        "notes": [
            "The corporate frozen build bundles ONLY the runtime_direct set +",
            "their transitive deps. eval_test_direct are dev/CI-only and are NOT",
            "shipped. The e5-small ONNX model (~120MB) is bundled inline as a data",
            "asset, not a pip dep; the gte reranker (~1.1GB) is default-OFF and is",
            "NOT bundled (opt-in: pre-seed / vendor / HF-allowlist — see DIST-02).",
            "runtime_detail[].metadata_sha256 is the sha256 of the dist's METADATA",
            "file (a content pin); full per-file hashes are in the dist RECORD.",
        ],
    }

    out_dir = pathlib.Path("packaging")
    out_dir.mkdir(exist_ok=True)
    mf_path = out_dir / "s12-dependency-manifest.json"
    mf_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {mf_path} ({len(runtime_direct)} runtime direct, {len(full_closure)} total)")

    # Resolve the app version (from pyproject.toml — brain isn't pip-installed
    # in the minimal venv; it runs via PYTHONPATH).
    app_version = "0.2.0"
    pp = pathlib.Path("pyproject.toml")
    if pp.exists():
        for line in pp.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("version") and "=" in line:
                app_version = line.split("=", 1)[1].strip().strip('"').strip("'")
                break

    # CycloneDX SBOM.
    components = []
    for name, version in sorted(full_closure.items()):
        dist_name = next((n for n in all_pkgs if _norm(n) == name), name)
        lic = _licence(dist_name)
        prov, url = PROVENANCE.get(dist_name, (f"pypi:{name}", ""))
        comp = {
            "type": "library",
            "bom-ref": _purl(dist_name, version),
            "name": dist_name,
            "version": version,
            "purl": _purl(dist_name, version),
            "licenses": [{"license": {"id": lic}}] if lic != "UNKNOWN" else [],
            "externalReferences": [{"type": "website", "url": url}] if url else [],
            "properties": [
                {"name": "brain:scope", "value": "runtime" if name in {_norm(k) for k in RUNTIME_DIRECT} else "transitive"},
            ],
        }
        components.append(comp)

    # Mark eval/test direct.
    eval_norm = {_norm(k) for k in EVAL_TEST_DIRECT}
    for c in components:
        if _norm(c["name"]) in eval_norm:
            c["properties"] = [{"name": "brain:scope", "value": "eval-test"}]

    # The bundled model as a data component (not a pip package).
    components.append({
        "type": "data",
        "bom-ref": "model:intfloat/multilingual-e5-small",
        "name": "intfloat/multilingual-e5-small (ONNX)",
        "version": "Xenova/multilingual-e5-small snapshot",
        "licenses": [{"license": {"id": "Apache-2.0"}}],
        "properties": [
            {"name": "brain:scope", "value": "runtime (bundled inline, offline-first)"},
            {"name": "brain:model_role", "value": "embedder"},
        ],
    })
    components.append({
        "type": "data",
        "bom-ref": "model:Alibaba-NLP/gte-multilingual-reranker-base",
        "name": "Alibaba-NLP/gte-multilingual-reranker-base (ONNX)",
        "version": "onnx-community/gte-multilingual-reranker-base snapshot",
        "licenses": [{"license": {"id": "Apache-2.0"}}],
        "properties": [
            {"name": "brain:scope", "value": "optional (default OFF; not bundled — opt-in via pre-seed/vendor/HF-allowlist)"},
            {"name": "brain:model_role", "value": "reranker"},
        ],
    })

    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": "urn:uuid:" + hashlib.uuid4().hex if hasattr(hashlib, "uuid4") else "",
        "version": 1,
        "metadata": {
            "timestamp": now,
            "component": {
                "type": "application",
                "name": "profile-a-brain",
                "version": app_version,
            },
            "tools": [{"vendor": "brain", "name": "s12_generate_sbom.py", "version": "1.0"}],
        },
        "components": components,
    }
    # uuid4 is in the `uuid` stdlib, not hashlib.
    import uuid
    sbom["serialNumber"] = f"urn:uuid:{uuid.uuid4()}"

    sbom_path = out_dir / "s12-sbom.cdx.json"
    sbom_path.write_text(json.dumps(sbom, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {sbom_path} ({len(components)} components)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
