"""Text embeddings for the knowledge base — pick LOCAL or CLOUD.

Two interchangeable backends:

* ``local``  — fastembed (ONNX, CPU, **offline, free**; the default). The model
  ``BAAI/bge-small-en-v1.5`` (384-dim) runs on your machine; no data leaves it.
* ``gemini`` — Google's hosted embedding API (``gemini-embedding-*``). Higher
  quality, but each call **sends your KB text** (session summaries, to-dos,
  topics, and your search queries) to Google and is subject to free-tier quota.

Selection precedence (highest first): an explicit env var, then ``_settings.json``
(written by the recorder GUI), then the built-in default. Both the recorder and
``serve.py`` import this module, so choosing a backend in the GUI makes the
dashboard search use the same one automatically.

Switching backend/model changes :func:`signature`, which the indexer folds into
its per-session content hash — so a switch transparently re-embeds new sessions
and a ``reindex`` rebuilds the rest, while search only ever compares vectors of
the same dimension (see ``kb.search``).

Env overrides: ``RECORDER_EMBED_BACKEND`` (local|gemini),
``RECORDER_EMBED_MODEL`` (local model), ``RECORDER_GEMINI_EMBED_MODEL``,
``RECORDER_GEMINI_EMBED_DIM`` (output dims, default 768).
"""

import os
import json

import numpy as np

# --------------------------------------------------------------------------
# Configuration (env  >  _settings.json  >  default)
# --------------------------------------------------------------------------
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_SETTINGS_PATH = os.path.join(_APP_DIR, "_settings.json")

DEFAULT_LOCAL_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_GEMINI_MODEL = "gemini-embedding-001"
DEFAULT_GEMINI_DIM = 768

# Known fastembed model dimensions (used only for signature/label; search reads
# the real dimension off each stored vector, so an unknown model is harmless).
_LOCAL_DIMS = {
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "BAAI/bge-large-en-v1.5": 1024,
}

# Gemini embedding requests are batched; the API accepts many inputs per call.
_GEMINI_BATCH = 100
# Defensive per-item truncation (chars). gemini-embedding-001 caps at 2048 input
# tokens; our docs are short, but a long summary shouldn't error the whole batch.
_GEMINI_MAX_CHARS = 8000


def _read_settings():
    try:
        with open(_SETTINGS_PATH, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _cfg(env_key, settings_key, default):
    v = os.environ.get(env_key)
    if v is not None and v.strip():
        return v.strip()
    s = _read_settings().get(settings_key)
    if isinstance(s, str) and s.strip():
        return s.strip()
    if s is not None and not isinstance(s, str):
        return s
    return default


BACKEND = str(_cfg("RECORDER_EMBED_BACKEND", "embed_backend", "local")).lower()
LOCAL_MODEL = _cfg("RECORDER_EMBED_MODEL", "embed_local_model", DEFAULT_LOCAL_MODEL)
GEMINI_MODEL = _cfg("RECORDER_GEMINI_EMBED_MODEL", "embed_gemini_model", DEFAULT_GEMINI_MODEL)
# Gemini embedding model to fall back to when the primary fails (429/503/
# unavailable). "" disables embedding failover. GUI "Embedding fallback" /
# RECORDER_GEMINI_EMBED_FALLBACK.
GEMINI_FALLBACK_MODEL = _cfg("RECORDER_GEMINI_EMBED_FALLBACK", "embed_gemini_fallback_model", "")
try:
    GEMINI_DIM = int(_cfg("RECORDER_GEMINI_EMBED_DIM", "embed_gemini_dim", DEFAULT_GEMINI_DIM))
except (TypeError, ValueError):
    GEMINI_DIM = DEFAULT_GEMINI_DIM

# Back-compat aliases (older code referenced these module globals).
MODEL_NAME = LOCAL_MODEL
DIM = _LOCAL_DIMS.get(LOCAL_MODEL, 384)

_MODEL = None       # cached fastembed model instance
_MODEL_KEY = None   # the LOCAL_MODEL the cache was built for


# --------------------------------------------------------------------------
# Backend selection / identity
# --------------------------------------------------------------------------
def set_backend(backend=None, model=None, dim=None):
    """Switch backend/model live. GUI-thread safe: just rebinds globals, which
    the next embed call reads. Resets the cached local model so a model change
    takes effect. ``model`` applies to whichever backend is active afterwards."""
    global BACKEND, LOCAL_MODEL, GEMINI_MODEL, GEMINI_DIM, _MODEL, _MODEL_KEY
    if backend:
        BACKEND = str(backend).strip().lower()
    if model:
        model = str(model).strip()
        if BACKEND == "gemini":
            GEMINI_MODEL = model
        else:
            LOCAL_MODEL = model
    if dim:
        try:
            GEMINI_DIM = int(dim)
        except (TypeError, ValueError):
            pass
    _MODEL = None
    _MODEL_KEY = None


def set_gemini_fallback(model):
    """Set the Gemini embedding model to fall back to when the primary fails
    (429/503/unavailable). ``""``/None disables embedding failover. Live + GUI-
    thread safe; read at the next embed call."""
    global GEMINI_FALLBACK_MODEL
    GEMINI_FALLBACK_MODEL = (model or "").strip()


def current():
    """Return ``(backend, model, dim)`` for the active configuration."""
    if BACKEND == "gemini":
        return ("gemini", GEMINI_MODEL, GEMINI_DIM)
    return ("local", LOCAL_MODEL, _LOCAL_DIMS.get(LOCAL_MODEL, 0))


def signature():
    """Compact identity of the active embedder. Folded into the indexer's
    content hash so changing it forces a re-embed and keeps dims consistent."""
    b, m, d = current()
    return f"{b}:{m}:{d}"


def label():
    """Human-friendly name for the GUI badge/logs (e.g. 'Gemini · ...')."""
    b, m, d = current()
    if b == "gemini":
        return f"Gemini · {m}"
    return f"Local · {m.split('/')[-1]}"


def available():
    """True if the *active* backend can actually run right now."""
    if BACKEND == "gemini":
        try:
            import gemini
            return gemini.available()
        except Exception:
            return False
    try:
        import fastembed  # noqa: F401
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# Local backend (fastembed)
# --------------------------------------------------------------------------
def _local_model():
    global _MODEL, _MODEL_KEY
    if _MODEL is None or _MODEL_KEY != LOCAL_MODEL:
        from fastembed import TextEmbedding
        _MODEL = TextEmbedding(model_name=LOCAL_MODEL)
        _MODEL_KEY = LOCAL_MODEL
    return _MODEL


def _to_f32(gen):
    return [np.asarray(v, dtype="float32") for v in gen]


def _local_embed(method_names, texts):
    """Run the first available embedding method; fall back to plain .embed().

    bge-style models do better with separate query/passage instructions, but not
    every model exposes those methods — so we try them in order and fall back.
    """
    m = _local_model()
    for name in method_names:
        fn = getattr(m, name, None)
        if fn is None:
            continue
        try:
            return _to_f32(fn(texts))
        except Exception:
            continue
    return _to_f32(m.embed(texts))


# --------------------------------------------------------------------------
# Gemini backend (hosted API)
# --------------------------------------------------------------------------
def _gemini_embed(texts, task_type):
    """Embed via Google's embedding API. Reuses gemini._client() for the key
    (we never read/echo it ourselves). Batched + light retry on transient
    429/503 so a reindex doesn't fall over on a blip."""
    import time
    import gemini
    from google.genai import types

    client = gemini._client()
    cfg = types.EmbedContentConfig(task_type=task_type)
    if GEMINI_DIM:
        cfg.output_dimensionality = GEMINI_DIM

    # Try the primary embedding model, then the user's fallback (if set). Same
    # output_dimensionality is requested for both, so stored vectors stay a
    # consistent dimension regardless of which model answered.
    models = [GEMINI_MODEL]
    if GEMINI_FALLBACK_MODEL and GEMINI_FALLBACK_MODEL != GEMINI_MODEL:
        models.append(GEMINI_FALLBACK_MODEL)

    out = []
    for i in range(0, len(texts), _GEMINI_BATCH):
        batch = [t[:_GEMINI_MAX_CHARS] for t in texts[i:i + _GEMINI_BATCH]]
        vecs, last = None, None
        for model in models:
            for attempt in range(3):
                try:
                    resp = client.models.embed_content(
                        model=model, contents=batch, config=cfg)
                    vecs = [np.asarray(e.values, dtype="float32")
                            for e in resp.embeddings]
                    break
                except Exception as e:  # noqa: BLE001
                    last = e
                    retry = False
                    try:
                        retry = gemini._retryable(str(e))
                    except Exception:
                        retry = False
                    if attempt < 2 and retry:
                        time.sleep(2 * (attempt + 1))
                        continue
                    break   # out of retries (or non-retryable) — try next model
            if vecs is not None:
                break
        if vecs is None:
            raise last if last is not None else RuntimeError("embedding failed")
        out.extend(vecs)
    return out


# --------------------------------------------------------------------------
# Public API (backend-agnostic)
# --------------------------------------------------------------------------
def embed_documents(texts):
    """Embed stored documents (knowledge-base entries). Returns float32 vectors."""
    texts = [str(t) for t in texts]
    if not texts:
        return []
    if BACKEND == "gemini":
        return _gemini_embed(texts, "RETRIEVAL_DOCUMENT")
    return _local_embed(["passage_embed", "embed"], texts)


def embed_query(text):
    """Embed a single search query. Returns one float32 vector (or None)."""
    text = str(text or "")
    if BACKEND == "gemini":
        out = _gemini_embed([text], "RETRIEVAL_QUERY")
    else:
        out = _local_embed(["query_embed", "embed"], [text])
    return out[0] if out else None
