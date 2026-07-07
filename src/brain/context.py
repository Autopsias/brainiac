"""UPG-04 — Contextual Retrieval (Anthropic-style, index-time).

For each note, generate a ≤1-sentence document-level context that situates the
note within the corpus, and prepend it to every chunk before embedding (and to
the BM25 text). This is the technique Anthropic published (Sep 2024) as
"Contextual Retrieval": they reported a −49% retrieval-failure-rate reduction
for contextual embeddings alone, and −67% stacked with a cross-encoder reranker.

The context is generated ONCE per note at INDEX TIME by a small local CPU LLM
(Qwen2.5-0.5B, GGUF, via llama-cpp-python — no PyTorch, no GPU, no network).
Zero added query latency. Negligible index-size growth (one sentence per note).

This targets brain's documented failure modes (S10): atomic markdown notes and
cross-lingual entity-only chunks that are reachable by EN queries but lose the
note-level context that disambiguates them. The template-based prefix in
``brain.chunk`` already does a lighter version of this (title + zone + heading);
UPG-04 adds the LLM's semantic summary of what the note is ABOUT.

GATE (revert if not met): retrieval-failure-rate drops on the 135-q blind set
with NO regression on the identifier/temporal strata (those queries are already
lexical-exact and should not be perturbed by added context). Gate-off if it
regresses.

OFFLINE / NO-LLM FALLBACK: if ``llama-cpp-python`` is not importable or no model
is bundled, ``doc_context`` returns "" and the index degrades cleanly to the
template-prefix-only path (the S10 behaviour). Contextual Retrieval is an
OPT-IN upgrade via ``$BRAIN_CONTEXTUAL_LLM`` (path to a GGUF); without it, the
feature is inert.
"""
from __future__ import annotations

import os
from functools import lru_cache

# The prompt asks the LLM for a single-sentence summary of what the note is about,
# in the note's own language. The summary is prepended to every chunk at embed time.
# Kept deliberately short (Anthropic found 50-100 tokens optimal; we cap tighter).
_PROMPT_TMPL = (
    "<|im_start|>system\nYou write a single concise sentence (max 30 words) "
    "summarising what this document is about, for retrieval. Reply in the "
    "document's language. Output ONLY the summary sentence, nothing else."
    "<|im_end|>\n<|im_start|>user\nDocument title: {title}\nDocument zone: {zone}\n\n"
    "Document content:\n{body}<|im_end|>\n<|im_start|>assistant\n"
)


@lru_cache(maxsize=1)
def _llm():
    """Lazily load the local CPU LLM. Returns None if unavailable (clean fallback).

    Resolves the model path from ``$BRAIN_CONTEXTUAL_LLM`` (a GGUF file). Without
    it, contextual retrieval is inert (returns "") and the index degrades to the
    template-prefix-only S10 path — never an error.
    """
    model_path = os.environ.get("BRAIN_CONTEXTUAL_LLM")
    if not model_path:
        return None
    try:
        from llama_cpp import Llama
    except ImportError:
        return None
    try:
        return Llama(
            model_path=model_path,
            n_ctx=2048,           # notes are chunked; 2K ctx is enough for the body slice
            n_gpu_layers=0,       # CPU-only by design
            verbose=False,
            n_threads=max(1, (os.cpu_count() or 4) // 2),  # don't saturate all cores
        )
    except Exception:
        return None


def doc_context(title: str, zone: str, body: str, *, max_body_chars: int = 1600) -> str:
    """Generate a ≤1-sentence document-level context for ``Contextual Retrieval``.

    Returns "" when the LLM is unavailable (the clean no-op fallback). The body
    is truncated to ``max_body_chars`` to bound the LLM prompt — the first ~1.6K
    chars (title + abstract + first sections) carry enough signal for a one-line
    summary, and this keeps the per-note generation cost to a few seconds on CPU.
    """
    llm = _llm()
    if llm is None:
        return ""
    body_slice = (body or "")[:max_body_chars]
    prompt = _PROMPT_TMPL.format(
        title=(title or "(untitled)")[:120],
        zone=(zone or "brain"),
        body=body_slice,
    )
    try:
        out = llm(
            prompt,
            max_tokens=60,        # ~one sentence
            temperature=0.0,      # deterministic summary
            stop=["<|im_end|>", "\n\n", "."],  # stop at first sentence end
        )
        text = out["choices"][0]["text"].strip().strip('"').strip()
        # collapse to a single line and cap length
        text = " ".join(text.split())[:200]
        return text + "." if text and not text.endswith(".") else text
    except Exception:
        return ""


def contextual_available() -> bool:
    """True iff the contextual LLM is configured and loadable."""
    return _llm() is not None
