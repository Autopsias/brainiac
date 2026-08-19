"""Build the derived graph-discovery artifact."""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import classification, config, egress
from . import graphify as graphify_model
from .graph import build_graph
from .progress import progress_note


@dataclass(frozen=True)
class GraphBuildPaths:
    """Runtime paths touched by one graphify build."""

    manifest: Path
    graph: Path
    failure_marker: Path


def _graph_build_paths(core: Any) -> GraphBuildPaths:
    """Create the graph runtime directory and resolve its artifact paths."""
    config.graph_dir(core.vault).mkdir(parents=True, exist_ok=True)
    return GraphBuildPaths(
        manifest=config.graph_manifest_path(core.vault),
        graph=config.graph_json_path(core.vault),
        failure_marker=config.graph_build_failed_marker_path(core.vault),
    )


def _load_graph_manifest(path: Path) -> dict[str, Any]:
    """Read a prior graph manifest, treating absence or damage as empty."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _record_graph_failure(
    core: Any,
    path: Path,
    *,
    status: str,
    attempted_at: str,
    detail: str,
    extra: dict[str, Any],
) -> None:
    """Persist a visible graph-build failure without touching the published graph."""
    path.write_text(
        json.dumps({"status": status, **extra, "attempted_at": attempted_at}, indent=2),
        encoding="utf-8",
    )
    core._note_graphify_build_outcome(
        status=status,
        detail=detail,
        attempted_at=attempted_at,
    )


def _build_graph_artifact(
    core: Any,
    *,
    day: dt.date,
    generation: int,
    note_count: int,
    json_mode: bool,
    failure_marker: Path,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Build the in-memory artifact and convert build errors into result documents."""
    started = time.monotonic()
    progress_note(
        f"graphify: building discovery graph ({note_count} notes)...",
        json_mode=json_mode,
        verb="graphify",
    )
    try:
        link_graph = build_graph(core.index.conn)
        progress_note(
            "graphify: link graph built, computing PageRank + candidates...",
            json_mode=json_mode,
            verb="graphify",
        )
        built = graphify_model.build_graph_artifact(
            core.index.conn,
            core.index.backend,
            link_graph,
            today=day,
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        _record_graph_failure(
            core,
            failure_marker,
            status="build_failed",
            attempted_at=day.isoformat(),
            detail=error,
            extra={"error": error},
        )
        return None, {
            "ritual": "graphify",
            "status": "build_failed",
            "published": False,
            "error": error,
            "marker": str(failure_marker),
        }
    duration = time.monotonic() - started
    progress_note(
        f"graphify: built in {duration:.1f}s", json_mode=json_mode, verb="graphify"
    )
    artifact = {
        "schema_version": graphify_model.GRAPH_SCHEMA_VERSION,
        "generation": generation,
        "built_at": day.isoformat(),
        "authoritative": False,
        "provenance": graphify_model.PROVENANCE,
        **built,
        "build": {
            "duration_seconds": round(duration, 3),
            "budget_seconds": graphify_model.DEFAULT_BUDGET_SECONDS,
            "action_required_seconds": graphify_model.ACTION_REQUIRED_SECONDS,
            "action_required": duration > graphify_model.ACTION_REQUIRED_SECONDS,
        },
    }
    return artifact, None


def _validate_graph_artifact(
    core: Any, artifact: dict[str, Any], day: dt.date, failure_marker: Path
) -> dict[str, Any] | None:
    """Reject an invalid in-memory artifact without replacing published state."""
    valid, problems = graphify_model.validate_artifact(artifact)
    if valid:
        return None
    _record_graph_failure(
        core,
        failure_marker,
        status="invalid_artifact",
        attempted_at=day.isoformat(),
        detail="; ".join(problems),
        extra={"problems": problems},
    )
    return {
        "ritual": "graphify",
        "status": "invalid_artifact",
        "published": False,
        "problems": problems,
        "marker": str(failure_marker),
    }


def _gate_graph_candidates(
    artifact: dict[str, Any], *, candidate_limit: int, max_tier: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply the classification egress gate to surfaced graph candidates."""
    candidates = graphify_model.top_candidates(artifact["edges"], limit=candidate_limit)
    node_lookup = {node["id"]: node for node in artifact["nodes"]}
    touched_ids = {item["from"] for item in candidates} | {
        item["to"] for item in candidates
    }
    touched_nodes = [
        node_lookup[note_id] for note_id in touched_ids if note_id in node_lookup
    ]
    surfaced_nodes, report = egress.apply_gate(touched_nodes, max_tier=max_tier)
    surfaced_ids = {node["id"] for node in surfaced_nodes}
    gated = [
        item
        for item in candidates
        if item["from"] in surfaced_ids and item["to"] in surfaced_ids
    ]
    return gated, report


def _publish_graph_artifact(
    core: Any,
    paths: GraphBuildPaths,
    *,
    artifact: dict[str, Any],
    manifest: dict[str, Any],
    day: dt.date,
) -> None:
    """Atomically publish validated graph and manifest files."""
    temporary_graph = paths.graph.with_suffix(paths.graph.suffix + ".tmp")
    temporary_graph.write_text(
        json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8"
    )
    os.replace(temporary_graph, paths.graph)
    paths.failure_marker.unlink(missing_ok=True)
    new_state = {
        "generation": artifact["generation"],
        "built_at": day.isoformat(),
        "notes": manifest,
    }
    temporary_manifest = paths.manifest.with_suffix(paths.manifest.suffix + ".tmp")
    temporary_manifest.write_text(
        json.dumps(new_state, indent=2, sort_keys=True), encoding="utf-8"
    )
    os.replace(temporary_manifest, paths.manifest)
    core._note_graphify_build_outcome(status="ok", attempted_at=day.isoformat())
    try:
        core.graph_report(today=day)
    except Exception:
        pass


def _default_graph_builder(core: Any) -> Callable[..., dict[str, Any]]:
    """Bind the production graph builder at the host's full-vault tier."""

    def build(*, force: bool, dry_run: bool, today: Any) -> dict[str, Any]:
        return core.graphify(
            force=force,
            dry_run=dry_run,
            today=today,
            max_tier=classification.DEFAULT_MAX_TIER,
        )

    return build


def _bump_graphify_marker(
    core: Any,
    state: dict[str, Any],
    marker: dict[str, Any],
    *,
    today: Any,
    dry_run: bool,
) -> None:
    """Persist one failed bounded-build attempt for exponential backoff."""
    marker["consecutive_overruns"] = int(marker.get("consecutive_overruns", 0)) + 1
    marker["last_overrun"] = today.isoformat()
    state["_graphify_drift"] = marker
    if not dry_run:
        core._save_maintain_state(state)


class GraphOpsMixin:
    """Provide BrainCore's graph build operations."""

    def graphify(
        self,
        *,
        force: bool = False,
        dry_run: bool = False,
        today: Any = None,
        max_tier: str = classification.VM_DEFAULT_MAX_TIER,
        candidate_limit: int = 20,
        json_mode: bool = False,
    ) -> dict[str, Any]:
        """Build GRF-01's validated, non-authoritative discovery graph.

        This host-only operation preserves the drift gate, vector reuse, atomic
        publication, failure marker, and candidate egress gate from ADR-0003
        Ruling 6/(a).
        """
        self._require_host("build the graphify discovery graph")
        day = today if today is not None else dt.date.today()
        paths = _graph_build_paths(self)
        manifest = graphify_model.corpus_manifest(self.index.conn)
        old_state = _load_graph_manifest(paths.manifest)
        if not force and graphify_model.manifest_unchanged(old_state, manifest):
            return {
                "ritual": "graphify",
                "skipped": "unchanged",
                "generation": old_state.get("generation"),
                "built_at": old_state.get("built_at"),
                "note_count": len(manifest),
                "published": False,
            }
        generation = int(old_state.get("generation") or 0) + 1
        artifact, failure = _build_graph_artifact(
            self,
            day=day,
            generation=generation,
            note_count=len(manifest),
            json_mode=json_mode,
            failure_marker=paths.failure_marker,
        )
        if failure is not None or artifact is None:
            return failure or {"ritual": "graphify", "published": False}
        invalid = _validate_graph_artifact(self, artifact, day, paths.failure_marker)
        if invalid is not None:
            return invalid
        candidates, report = _gate_graph_candidates(
            artifact, candidate_limit=candidate_limit, max_tier=max_tier
        )
        if dry_run:
            return {
                "ritual": "graphify",
                "dry_run": True,
                "published": False,
                "generation": generation,
                "corpus": artifact["corpus"],
                "build": artifact["build"],
                "candidates": candidates,
                "egress": report,
            }
        _publish_graph_artifact(
            self, paths, artifact=artifact, manifest=manifest, day=day
        )
        return {
            "ritual": "graphify",
            "dry_run": False,
            "published": True,
            "generation": generation,
            "path": str(paths.graph),
            "corpus": artifact["corpus"],
            "build": artifact["build"],
            "candidates": candidates,
            "egress": report,
        }

    def _graph_status(self) -> dict[str, Any]:
        """Read the published graph generation and age without mutating it."""
        state = _load_graph_manifest(config.graph_manifest_path(self.vault))
        if not state:
            return {"status": "never_built"}
        built_at = state.get("built_at")
        age_days = None
        if built_at:
            try:
                age_days = (dt.date.today() - dt.date.fromisoformat(built_at)).days
            except ValueError:
                age_days = None
        return {
            "status": "ok",
            "generation": state.get("generation"),
            "built_at": built_at,
            "age_days": age_days,
            "note_count": len(state.get("notes") or {}),
        }

    def _run_bounded_graphify(
        self,
        *,
        force: bool,
        dry_run: bool,
        today: Any,
        state: dict[str, Any],
        reason: str,
        builder: Any = None,
    ) -> dict[str, Any]:
        """Run FRESH-01's single in-process, attempt-bounded graph build."""
        build = builder or _default_graph_builder(self)
        marker = dict(state.get("_graphify_drift") or {})
        marker["last_attempt"] = today.isoformat()
        marker["last_reason"] = reason
        state["_graphify_drift"] = marker
        if not dry_run:
            self._save_maintain_state(state)
        try:
            result = build(force=force, dry_run=dry_run, today=today)
        except Exception as exc:
            _bump_graphify_marker(self, state, marker, today=today, dry_run=dry_run)
            return {
                "ritual": "graphify",
                "invoked": True,
                "published": False,
                "reason": reason,
                "status": "build_error",
                "error": f"{type(exc).__name__}: {exc}",
                "build": {"action_required": True},
            }
        if not isinstance(result, dict):
            _bump_graphify_marker(self, state, marker, today=today, dry_run=dry_run)
            return {
                "ritual": "graphify",
                "invoked": True,
                "published": False,
                "reason": reason,
                "status": "bad_result",
                "build": {"action_required": True},
            }
        result["invoked"] = True
        result["reason"] = reason
        if dry_run or result.get("dry_run"):
            return result
        if result.get("published") or result.get("skipped"):
            marker["consecutive_overruns"] = 0
            marker["last_success"] = today.isoformat()
            state["_graphify_drift"] = marker
            self._save_maintain_state(state)
            return result
        _bump_graphify_marker(self, state, marker, today=today, dry_run=dry_run)
        result.setdefault("build", {})["action_required"] = True
        result.setdefault("status", "build_not_published")
        return result
