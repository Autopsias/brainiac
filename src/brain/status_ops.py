"""Assemble the cross-surface BrainCore status report."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import __version__, config
from .embed import ONNX_MODEL_SIZE_HINT, model_cache_ready
from .index import SCHEMA_VERSION
from .snapshot import snapshot_status


def _schema_is_newer(stored: Any) -> bool:
    """Return whether stored state was produced by a newer schema."""
    if stored is None:
        return False
    try:
        return int(stored) > SCHEMA_VERSION
    except (TypeError, ValueError):
        return False


def _index_status(core: Any) -> dict[str, Any]:
    """Read index statistics without making status itself fragile."""
    try:
        return core.index.stats()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _add_embedder_status(core: Any, out: dict[str, Any]) -> None:
    """Surface the live embedder beside the model recorded in index metadata."""
    try:
        live_id = core.index.embedder.model_id
        recorded = out.get("index", {}).get("embed_model")
        matches = recorded is None or recorded == live_id
        out["live_embedder"] = {
            "model_id": live_id,
            "is_hash_fallback": live_id == "hash-v1",
            "matches_index_metadata": matches,
        }
        explicit_hash = os.environ.get("BRAIN_EMBEDDER", "").strip().lower() == "hash"
        cached = model_cache_ready(core.index.embedder)
        pending = not explicit_hash and (not matches or cached is False)
        out["embedder"] = {
            "state": "pending" if pending else "ready",
            "model_id": live_id,
            "cached": cached,
            "index_matches": matches,
        }
        if pending and cached is False:
            out["embedder"]["download_size_hint"] = ONNX_MODEL_SIZE_HINT
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        out["live_embedder"] = {"error": error}
        out["embedder"] = {"state": "error", "error": error}


def _query_capture_status(core: Any) -> dict[str, Any]:
    """Read the role-contained raw-query capture status."""
    try:
        from . import querylog

        return querylog.status(core.vault, role=core.role)
    except Exception as exc:
        return {
            "enabled": False,
            "state": "error",
            "reason": f"{type(exc).__name__}",
        }


def _cos_status(core: Any) -> dict[str, Any]:
    """Read COS queue observability without crashing the status surface."""
    try:
        from . import cos

        return cos.status_block(core.vault, core.role)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


class StatusOpsMixin:
    """Provide BrainCore's host/VM status operation."""

    def status(
        self, snapshot_dest: str | Path | None = None, today: Any = None
    ) -> dict[str, Any]:
        """Report index, snapshot, maintenance, graph, and COS state."""
        destination = (
            Path(snapshot_dest) if snapshot_dest else config.snapshot_dir(self.vault)
        )
        index_status = _index_status(self)
        stored_schema = index_status.get("schema_version")
        out: dict[str, Any] = {
            "vault": str(self.vault),
            "role": self.role,
            "index": index_status,
            "version": {
                "package_version": __version__,
                "index_schema_version": stored_schema,
                "binary_schema_version": SCHEMA_VERSION,
                "index_newer_than_binary": _schema_is_newer(stored_schema),
            },
        }
        _add_embedder_status(self, out)
        out["snapshot"] = snapshot_status(destination)
        snapshot_schema = out["snapshot"].get("schema_version")
        out["version"]["snapshot_schema_version"] = snapshot_schema
        out["version"]["snapshot_newer_than_binary"] = _schema_is_newer(snapshot_schema)
        out["pending_drafts"] = self._count_pending_drafts()
        out["query_capture"] = _query_capture_status(self)
        out["maintain_heartbeat"] = self._maintain_heartbeat_summary(today=today)
        out["graph"] = self._graph_status()
        out["cos"] = _cos_status(self)
        return out
