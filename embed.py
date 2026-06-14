"""Local, free text embeddings via fastembed (ONNX, CPU — no API key).

These power semantic search over the knowledge base. If fastembed isn't
installed the module degrades gracefully: ``available()`` returns False and the
dashboard falls back to its plain substring filter.

The default model (BAAI/bge-small-en-v1.5, 384-dim) is small and downloaded
once on first use. Override with RECORDER_EMBED_MODEL.
"""

import os

import numpy as np

MODEL_NAME = os.environ.get("RECORDER_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
DIM = 384  # bge-small-en-v1.5

_MODEL = None


def available():
    """True if fastembed can be imported (the model may still download lazily)."""
    try:
        import fastembed  # noqa: F401
        return True
    except Exception:
        return False


def _model():
    global _MODEL
    if _MODEL is None:
        from fastembed import TextEmbedding
        _MODEL = TextEmbedding(model_name=MODEL_NAME)
    return _MODEL


def _to_f32(gen):
    return [np.asarray(v, dtype="float32") for v in gen]


def _embed(method_names, texts):
    """Run the first available embedding method; fall back to plain .embed().

    bge-style models do better with separate query/passage instructions, but not
    every model exposes those methods — so we try them in order and fall back.
    list() forces the (lazy) generator so failures are caught here, not later.
    """
    m = _model()
    for name in method_names:
        fn = getattr(m, name, None)
        if fn is None:
            continue
        try:
            return _to_f32(fn(texts))
        except Exception:
            continue
    return _to_f32(m.embed(texts))


def embed_documents(texts):
    """Embed stored documents (knowledge-base entries). Returns float32 vectors."""
    texts = [str(t) for t in texts]
    if not texts:
        return []
    return _embed(["passage_embed", "embed"], texts)


def embed_query(text):
    """Embed a single search query. Returns one float32 vector (or None)."""
    out = _embed(["query_embed", "embed"], [str(text or "")])
    return out[0] if out else None
