"""Assemble dependency SBOM sections."""
from __future__ import annotations

import json
import pathlib
import uuid
from datetime import datetime, timezone

import importlib.metadata as md

from tools.generate_sbom import (
    EVAL_TEST_DIRECT,
    PROVENANCE,
    RUNTIME_DIRECT,
    _file_hashes,
    _licence,
    _metadata_sha256,
    _norm,
    _purl,
)


def _installed_packages() -> tuple[dict[str, str], dict[str, str]]:
    packages = {distribution.metadata["Name"]: distribution.metadata["Version"]
                for distribution in md.distributions()}
    normalised = {_norm(name): version for name, version in packages.items()}
    return packages, normalised


def _resolve_wanted(
    wanted: dict[str, str], packages: dict[str, str], normalised: dict[str, str]
) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for wanted_name in wanted:
        normalised_name = _norm(wanted_name)
        if normalised_name not in normalised:
            continue
        real_name = next(
            (name for name in packages if _norm(name) == normalised_name), wanted_name
        )
        resolved[real_name] = normalised[normalised_name]
    return resolved


def _runtime_details(runtime_direct: dict[str, str]) -> list[dict]:
    details: list[dict] = []
    for name, version in sorted(runtime_direct.items()):
        licence = _licence(name)
        provenance, url = PROVENANCE.get(name, (f"pypi:{_norm(name)}", ""))
        hashes = _file_hashes(name)
        metadata_hash = _metadata_sha256(name) or (
            hashes[0]["sha256"] if hashes else ""
        )
        details.append({
            "name": name,
            "version": version,
            "licence": licence,
            "provenance": provenance,
            "source_url": url,
            "metadata_sha256": metadata_hash,
            "file_count": len(hashes),
        })
    return details


def _manifest(
    now: str,
    runtime_direct: dict[str, str],
    runtime_details: list[dict],
    eval_direct: dict[str, str],
    full_closure: dict[str, str],
) -> dict:
    return {
        "schema": "brain-dist01-dependency-manifest/v1",
        "generated_at_utc": now,
        "generator": "tools/generate_sbom.py",
        "python_requires": ">=3.9",
        "runtime_direct": runtime_direct,
        "runtime_detail": runtime_details,
        "eval_test_direct": eval_direct,
        "full_closure": full_closure,
        "excluded": [
            "fastembed (REMOVED DIST-01: embedder migrated to direct-ONNX OnnxEmbedder)",
            "qwen3-embed (REMOVED DIST-01: S11-overturned; Qwen3 CPU-dead on HP fleet)",
            "torch / transformers / sentence-transformers (never a runtime dep; ONNX-only)",
        ],
        "notes": [
            "The corporate frozen build bundles ONLY the runtime_direct set +",
            "their transitive deps. eval_test_direct are dev/CI-only and are NOT",
            "shipped. The bge-m3-int8 ONNX model (~563MB) is bundled inline as a data",
            "asset, not a pip dep; the gte reranker (~1.1GB) is default-OFF and is",
            "NOT bundled (opt-in: pre-seed / vendor / HF-allowlist — see DIST-02).",
            "runtime_detail[].metadata_sha256 is the sha256 of the dist's METADATA",
            "file (a content pin); full per-file hashes are in the dist RECORD.",
        ],
    }


def _app_version() -> str:
    version = "0.2.0"
    pyproject = pathlib.Path("pyproject.toml")
    if pyproject.exists():
        for line in pyproject.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("version") and "=" in line:
                version = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    return version


def _components(
    full_closure: dict[str, str], packages: dict[str, str]
) -> list[dict]:
    runtime_names = {_norm(name) for name in RUNTIME_DIRECT}
    eval_names = {_norm(name) for name in EVAL_TEST_DIRECT}
    components: list[dict] = []
    for name, version in sorted(full_closure.items()):
        distribution_name = next(
            (candidate for candidate in packages if _norm(candidate) == name), name
        )
        licence = _licence(distribution_name)
        provenance, url = PROVENANCE.get(distribution_name, (f"pypi:{name}", ""))
        components.append({
            "type": "library",
            "bom-ref": _purl(distribution_name, version),
            "name": distribution_name,
            "version": version,
            "purl": _purl(distribution_name, version),
            "licenses": [{"license": {"id": licence}}] if licence != "UNKNOWN" else [],
            "externalReferences": [{"type": "website", "url": url}] if url else [],
            "properties": [{
                "name": "brain:scope",
                "value": "runtime" if name in runtime_names else "transitive",
            }],
        })
    for component in components:
        if _norm(component["name"]) in eval_names:
            component["properties"] = [{"name": "brain:scope", "value": "eval-test"}]
    components.extend([
        {
            "type": "data",
            "bom-ref": "model:BAAI/bge-m3-int8",
            "name": "BAAI/bge-m3-int8 (ONNX)",
            "version": "Xenova/bge-m3 model_int8.onnx snapshot",
            "licenses": [{"license": {"id": "Apache-2.0"}}],
            "properties": [
                {"name": "brain:scope", "value": "runtime (bundled inline, offline-first)"},
                {"name": "brain:model_role", "value": "embedder"},
            ],
        },
        {
            "type": "data",
            "bom-ref": "model:Alibaba-NLP/gte-multilingual-reranker-base",
            "name": "Alibaba-NLP/gte-multilingual-reranker-base (ONNX)",
            "version": "onnx-community/gte-multilingual-reranker-base snapshot",
            "licenses": [{"license": {"id": "Apache-2.0"}}],
            "properties": [
                {"name": "brain:scope", "value": "optional (default OFF; not bundled — opt-in via pre-seed/vendor/HF-allowlist)"},
                {"name": "brain:model_role", "value": "reranker"},
            ],
        },
    ])
    return components


def _write_manifest(out_dir: pathlib.Path, manifest: dict, runtime_count: int,
                    closure_count: int) -> None:
    manifest_path = out_dir / "dependency-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {manifest_path} ({runtime_count} runtime direct, {closure_count} total)")


def main() -> int:
    packages, normalised = _installed_packages()
    runtime_direct = _resolve_wanted(RUNTIME_DIRECT, packages, normalised)
    eval_direct = _resolve_wanted(EVAL_TEST_DIRECT, packages, normalised)
    full_closure = {_norm(name): version for name, version in packages.items()}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    runtime_details = _runtime_details(runtime_direct)
    out_dir = pathlib.Path("packaging")
    out_dir.mkdir(exist_ok=True)
    _write_manifest(
        out_dir,
        _manifest(now, runtime_direct, runtime_details, eval_direct, full_closure),
        len(runtime_direct),
        len(full_closure),
    )
    components = _components(full_closure, packages)
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": now,
            "component": {
                "type": "application",
                "name": "profile-a-brain",
                "version": _app_version(),
            },
            "tools": [{"vendor": "brain", "name": "generate_sbom.py", "version": "1.0"}],
        },
        "components": components,
    }
    sbom_path = out_dir / "sbom.cdx.json"
    sbom_path.write_text(json.dumps(sbom, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {sbom_path} ({len(components)} components)")
    return 0
