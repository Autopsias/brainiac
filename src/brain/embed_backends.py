"""Embedder backend selection."""
from __future__ import annotations

from typing import Any, Callable, Optional


EmbedderFactory = Callable[..., Any]
AvailabilityProbe = Callable[[], bool]
ModelProbe = Callable[[str], bool]


def _select_explicit_embedder(
    prefer: str,
    *,
    model_name: Optional[str],
    hash_factory: EmbedderFactory,
    onnx_factory: EmbedderFactory,
    arctic_factory: EmbedderFactory,
    catalog_factory: EmbedderFactory,
    qwen_factory: EmbedderFactory,
    qwen_model: ModelProbe,
    unavailable_error: type[Exception],
) -> tuple[bool, Any]:
    """Resolve a caller-requested backend, if one was specified."""
    if prefer == "hash":
        return True, hash_factory()
    if prefer in ("onnx", "onnx-int8"):
        return True, onnx_factory()
    if prefer == "arctic":
        return True, arctic_factory()
    if prefer == "catalog":
        if not model_name:
            raise unavailable_error("prefer='catalog' needs $BRAIN_EMBED_MODEL")
        if qwen_model(model_name):
            return True, qwen_factory(model_name)
        return True, catalog_factory(model_name)
    if prefer == "qwen":
        return True, qwen_factory(model_name or "n24q02m/Qwen3-Embedding-0.6B-ONNX")
    return False, None


def _select_automatic_embedder(
    *,
    model_name: Optional[str],
    onnx_factory: EmbedderFactory,
    arctic_factory: EmbedderFactory,
    catalog_factory: EmbedderFactory,
    qwen_factory: EmbedderFactory,
    onnx_available: AvailabilityProbe,
    arctic_available: AvailabilityProbe,
    catalog_available: AvailabilityProbe,
    qwen_available: AvailabilityProbe,
    direct_model: ModelProbe,
    qwen_model: ModelProbe,
    implicit_hash_fallback: Callable[[], Any],
) -> Any:
    """Choose the first available backend for automatic selection."""
    if model_name and qwen_model(model_name) and qwen_available():
        return qwen_factory(model_name)
    if model_name and direct_model(model_name) and onnx_available():
        return onnx_factory(model_id=model_name)
    if model_name and catalog_available():
        return catalog_factory(model_name)
    if onnx_available():
        return onnx_factory()
    if arctic_available():
        return arctic_factory()
    return implicit_hash_fallback()


def select_embedder(
    prefer: str,
    *,
    model_name: Optional[str],
    hash_factory: EmbedderFactory,
    onnx_factory: EmbedderFactory,
    arctic_factory: EmbedderFactory,
    catalog_factory: EmbedderFactory,
    qwen_factory: EmbedderFactory,
    onnx_available: AvailabilityProbe,
    arctic_available: AvailabilityProbe,
    catalog_available: AvailabilityProbe,
    qwen_available: AvailabilityProbe,
    direct_model: ModelProbe,
    qwen_model: ModelProbe,
    unavailable_error: type[Exception],
    implicit_hash_fallback: Callable[[], Any],
) -> Any:
    """Select an embedder while preserving explicit and automatic paths."""
    handled, selected = _select_explicit_embedder(
        prefer,
        model_name=model_name,
        hash_factory=hash_factory,
        onnx_factory=onnx_factory,
        arctic_factory=arctic_factory,
        catalog_factory=catalog_factory,
        qwen_factory=qwen_factory,
        qwen_model=qwen_model,
        unavailable_error=unavailable_error,
    )
    if handled:
        return selected
    return _select_automatic_embedder(
        model_name=model_name,
        onnx_factory=onnx_factory,
        arctic_factory=arctic_factory,
        catalog_factory=catalog_factory,
        qwen_factory=qwen_factory,
        onnx_available=onnx_available,
        arctic_available=arctic_available,
        catalog_available=catalog_available,
        qwen_available=qwen_available,
        direct_model=direct_model,
        qwen_model=qwen_model,
        implicit_hash_fallback=implicit_hash_fallback,
    )
