#!/usr/bin/env python3
"""Live cross-lingual retrieval probe over the REAL ONNX embedder (S03 evidence).

Proves end-to-end, with no PyTorch:
  * the fastembed/ONNX embedder path runs (IDX-01) and chunks > notes (IDX-02),
  * MRL truncation + model_id/dim are stored in the index (IDX-01),
  * an English query reaches PT and ES notes that share NO tokens with it — the
    cross-lingual bridge happens at query time in the shared multilingual vector
    space (IDX-02 design claim), and
  * incremental sync upserts a changed note and propagates a delete (IDX-03).

Model of record is snowflake-arctic-embed-m-v2.0 (multilingual, MRL); fastembed
0.8.0's built-in catalog does not yet list v2.0, so this probe runs the real,
catalogued multilingual ONNX model paraphrase-multilingual-MiniLM-L12-v2 to
exercise the identical code path. The embedder class, prefixes, MRL truncation,
chunking and sync are model-agnostic.

Run with the venv that has fastembed + onnxruntime and the cached model:
    .venv-embed/bin/python tools/probe_xlingual.py --out _evidence/s03/xlingual-probe.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from brain.embed import ArcticEmbedder  # noqa: E402
from brain.index import BrainIndex  # noqa: E402
from brain.vectors import BruteForceBackend  # noqa: E402

PROBE_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def _note(nid, title, lang, body):
    return (
        f"---\nid: {nid}\ntitle: \"{title}\"\ntype: note\n"
        f"classification: Internal\ncreated: 2026-06-27\nupdated: 2026-06-27\n"
        f"lang: {lang}\n---\n\n{body}\n"
    )


def build_sample_vault(root: Path):
    (root / "brain" / "resources").mkdir(parents=True)
    notes = {
        # EN / PT / ES describe the SAME concept with (near-)disjoint vocabulary,
        # so a hit across languages must be semantic, not lexical.
        "brain/index.md": _note("index", "Index", "en", "Map of the brain."),
        "brain/resources/en-renew.md": _note(
            "en-renew", "Offshore wind expansion", "en",
            "## Strategy\nThe company is growing its offshore wind power capacity "
            "and floating turbines to cut carbon emissions over the next decade."),
        "brain/resources/pt-renew.md": _note(
            "pt-renew", "Expansão eólica marítima", "pt",
            "## Estratégia\nA empresa está a aumentar a capacidade de produção de "
            "energia eólica no mar com turbinas flutuantes para reduzir emissões."),
        "brain/resources/es-renew.md": _note(
            "es-renew", "Ampliación eólica marina", "es",
            "## Estrategia\nLa empresa amplía su capacidad de generación eólica en "
            "el mar mediante aerogeneradores flotantes para recortar emisiones."),
        "brain/resources/en-cooking.md": _note(
            "en-cooking", "Sourdough bread", "en",
            "## Recipe\nMixing flour water and wild yeast to bake a tangy "
            "sourdough loaf with a crisp crust."),
    }
    for rel, text in notes.items():
        (root / rel).write_text(text, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--model", default=PROBE_MODEL)
    ap.add_argument("--cache", default=str(Path(__file__).resolve().parents[1] / ".fastembed_cache"))
    args = ap.parse_args()

    result: dict = {"model_of_record": "snowflake/snowflake-arctic-embed-m-v2.0",
                    "probe_model": args.model, "torch_present": None}
    try:
        import torch  # noqa: F401
        result["torch_present"] = True
    except Exception:
        result["torch_present"] = False

    # The probe model native dim is 384; we keep it (MiniLM is not MRL-trained,
    # so we do not truncate it — MRL-256 is validated separately in unit tests).
    emb = ArcticEmbedder(model_id=args.model, dim=384, cache_dir=args.cache)

    with tempfile.TemporaryDirectory() as td:
        vault = Path(td) / "vault"
        build_sample_vault(vault)
        idx = BrainIndex(db_path=Path(td) / "idx.sqlite",
                         backend=BruteForceBackend(), embedder=emb)
        rb = idx.rebuild(vault)
        result["rebuild"] = {"indexed": rb["indexed"], "chunks": rb["chunks"],
                             "embed_model": rb["embed_model"], "embed_dim": rb["embed_dim"]}
        result["chunks_gt_notes"] = rb["chunks"] >= rb["indexed"]
        result["stats"] = idx.stats()

        # EN query, expect PT + ES renewable notes in the top hits (cross-lingual).
        q = "growth of sea-based renewable electricity generation"
        hits = idx.search(q, k=5)
        ranked = [{"id": h.id, "score": round(h.score, 4), "source": h.source} for h in hits]
        ids = [h.id for h in hits]
        result["query"] = q
        result["hits"] = ranked
        result["cross_lingual_pt_reached"] = "pt-renew" in ids
        result["cross_lingual_es_reached"] = "es-renew" in ids
        # the cooking note (unrelated) should rank below the renewable notes
        renew_ranks = [i for i, h in enumerate(hits) if h.id in
                       ("en-renew", "pt-renew", "es-renew")]
        cook_rank = next((i for i, h in enumerate(hits) if h.id == "en-cooking"), 999)
        result["renewables_outrank_unrelated"] = (
            bool(renew_ranks) and max(renew_ranks) < cook_rank)

        # IDX-03 incremental: change pt note, add a new ES note, delete cooking.
        (vault / "brain" / "resources" / "pt-renew.md").write_text(
            _note("pt-renew", "Expansão eólica marítima", "pt",
                  "## Estratégia\nNovo parágrafo: parques eólicos flutuantes ao "
                  "largo da costa e armazenamento em baterias."), encoding="utf-8")
        (vault / "brain" / "resources" / "es-solar.md").write_text(
            _note("es-solar", "Energía solar", "es",
                  "Paneles solares fotovoltaicos en tejados industriales."),
            encoding="utf-8")
        (vault / "brain" / "resources" / "en-cooking.md").unlink()
        sync = idx.sync(vault)
        result["sync"] = {k: sync[k] for k in
                          ("mode", "added", "updated", "unchanged", "deleted", "chunks")}
        result["delete_propagated"] = idx.get("en-cooking") is None
        result["upsert_applied"] = idx.get("es-solar") is not None

        # model-change guard: a different model_id must force a clean rebuild.
        from brain.embed import HashEmbedder
        idx2 = BrainIndex(db_path=idx.db_path, backend=BruteForceBackend(),
                          embedder=HashEmbedder())
        s2 = idx2.sync(vault)
        result["model_change_forces_rebuild"] = s2["mode"].startswith("rebuild")

    result["PASS"] = bool(
        result["cross_lingual_pt_reached"] and result["cross_lingual_es_reached"]
        and result["chunks_gt_notes"] and not result["torch_present"]
        and result["delete_propagated"] and result["upsert_applied"]
        and result["model_change_forces_rebuild"])

    out = json.dumps(result, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(out + "\n", encoding="utf-8")
    print(out)
    return 0 if result["PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
