"""Embedder backend selection: the hash stand-in and the direct-ONNX embedder."""
from __future__ import annotations

from typing import Any, Callable, Optional


EmbedderFactory = Callable[..., Any]
AvailabilityProbe = Callable[[], bool]
ModelProbe = Callable[[str], bool]

# Backends removed with the old A/B experiments; named so a stale
# ``$BRAIN_EMBEDDER`` fails closed instead of silently selecting something else.
REMOVED_BACKENDS = ("arctic", "catalog", "qwen")


def select_embedder(
    prefer: str,
    *,
    model_name: Optional[str],
    hash_factory: EmbedderFactory,
    onnx_factory: EmbedderFactory,
    onnx_available: AvailabilityProbe,
    direct_model: ModelProbe,
    unavailable_error: type[Exception],
    implicit_hash_fallback: Callable[[], Any],
) -> Any:
    """Select an embedder: explicit ``hash``/``onnx``, else automatic.

    Automatic never drops a named model to the hash embedder: with
    ``$BRAIN_EMBED_MODEL`` set and no ONNX runtime it raises.
    """
    if prefer == "hash":
        return hash_factory()
    if prefer in ("onnx", "onnx-int8"):
        return onnx_factory()
    if prefer in REMOVED_BACKENDS:
        raise unavailable_error(
            f"embedder backend {prefer!r} was removed; use 'onnx' or 'hash'"
        )
    if onnx_available():
        if model_name and direct_model(model_name):
            return onnx_factory(model_id=model_name)
        return onnx_factory()
    if model_name:
        raise unavailable_error(
            f"$BRAIN_EMBED_MODEL={model_name!r} needs the ONNX runtime, which is "
            "not available; refusing to degrade to the non-semantic HashEmbedder"
        )
    return implicit_hash_fallback()
