"""Free-tier Gemini backend: native video understanding.

The default pipeline samples a few JPEG frames per block, transcribes audio with
Whisper, and captions each block with Claude. This module is an alternative
engine: it uploads each recorded segment to Gemini, which watches the video and
listens to the audio *together*, and asks for the same structured activity
blocks the rest of insights.py already expects. The daily reduce can also run on
Gemini, giving a fully free analysis path.

Enable it with two environment variables (set them yourself):

    setx ANALYSIS_BACKEND gemini
    setx GEMINI_API_KEY  <your AI Studio key>

then restart the recorder. If the SDK or key is missing, insights.py silently
falls back to the Claude/local pipeline.

Cost control on the free tier
------------------------------
Recordings are stored as hour-long segments. A whole hour at Gemini's default
1 fps sampling would blow past the free-tier 250K tokens/minute limit, so we
analyze in WINDOW_SECONDS-long pieces at media_resolution=LOW.

What we upload is never the raw segment but a lightweight **proxy** built by
_make_upload_clip: PROXY_FPS fps + mono audio, resolution kept. Gemini samples
~1 fps at LOW anyway, so this is ~13x smaller (17.8 MB -> 1.4 MB measured) with
no loss of analysis quality — important in live mode, where the upload happens
during recording and a full-size upload can fall behind. The proxy also doubles
as our windowing tool: the API rejects clip-offset windowing on Files-API
uploads ("video_metadata parameter is not supported"), so:
  * a segment that fits in one window becomes one whole-file proxy, one call;
  * a longer segment becomes one proxy *per* window (each cut to [w0, w1], so the
    model sees it from t=0).
Each request then costs well under the per-minute cap, audio stays aligned, and
we space requests apart to respect the requests-per-minute limit.

Privacy note: Google's *free* tier may use uploaded content to improve their
products, including human review — don't point this at sensitive recordings
unless you're on a paid key (EEA/UK/CH free keys get paid-tier privacy).
"""

import os
import re
import json
import glob
import time

import analyze

# --------------------------------------------------------------------------
# Configuration (all overridable via environment)
# --------------------------------------------------------------------------
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
REDUCE_MODEL = os.environ.get("GEMINI_REDUCE_MODEL", MODEL)

# Free-tier-friendly, video-capable models (fastest/cheapest first). The recorder
# UI offers these in a dropdown — handy when one model's daily quota is used up —
# and you can type any other model name too. Daily free limits differ per model
# (e.g. 2.5-flash ~250 req/day, 2.5-flash-lite ~1000), so flipping to a lighter
# model keeps analysis going after the first is exhausted.
KNOWN_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-2.5-pro",
]


def set_model(model, reduce_model=None):
    """Switch the Gemini model used for analysis at runtime.

    Handy when one model's free-tier daily quota is exhausted: pick another and
    subsequent map (``analyze_*``) and reduce calls use it immediately — no
    restart needed. ``reduce_model`` defaults to the same model. Safe to call
    from the GUI thread while the worker is mid-session (just a string swap;
    each generate_content reads MODEL at call time)."""
    global MODEL, REDUCE_MODEL
    model = (model or "").strip()
    if not model:
        return
    MODEL = model
    REDUCE_MODEL = (reduce_model or model).strip()

# Seconds of footage per request. ~15 min at media_resolution LOW is far under
# the free-tier 250K tokens/minute ceiling while keeping few requests per hour.
WINDOW_SECONDS = int(os.environ.get("GEMINI_WINDOW_SECONDS", "900"))
THUMB_SCALE = int(os.environ.get("RECORDER_THUMB_SCALE", "480"))

# Upload proxy. Gemini samples video at ~1 fps and downsamples to LOW internally,
# so uploading the full-resolution 15 fps capture wastes upload time — which in
# live mode happens *during* recording and can fall behind. We instead upload a
# lightweight proxy: PROXY_FPS fps + mono audio, resolution kept (at 1 fps the
# video is tiny regardless, so res barely affects size but keeps text legible).
# Measured ~13x smaller (17.8 MB -> 1.4 MB) with no loss of analysis quality.
PROXY_FPS = float(os.environ.get("GEMINI_PROXY_FPS", "1"))
PROXY_MAX_WIDTH = int(os.environ.get("GEMINI_PROXY_MAX_WIDTH", "1920"))
PROXY_CRF = int(os.environ.get("GEMINI_PROXY_CRF", "32"))
PROXY_AUDIO_KBPS = int(os.environ.get("GEMINI_PROXY_AUDIO_KBPS", "48"))

# Free tier is ~10 requests/min (flash) / 15 (flash-lite); keep ~6.5s spacing.
MIN_INTERVAL = float(os.environ.get("GEMINI_MIN_REQUEST_INTERVAL", "6.5"))
MAX_RETRIES = int(os.environ.get("GEMINI_MAX_RETRIES", "4"))
RETRY_BACKOFF = float(os.environ.get("GEMINI_RETRY_BACKOFF", "12"))

# When the chosen model keeps returning a retryable error (503 overload /
# 429 quota / 5xx), automatically fail over to the next model here instead of
# giving up. Ordered cheapest+fastest first, and deliberately EXCLUDES
# gemini-2.5-pro (tiny free quota, and pricey on a paid key — never silently
# switch onto it). On a *quota* (429) failure the switch is made permanent for
# the session so later chunks skip the exhausted model. Disable with
# GEMINI_AUTO_FAILOVER=0.
AUTO_FAILOVER = (os.environ.get("GEMINI_AUTO_FAILOVER", "1").strip().lower()
                 not in ("0", "false", "no", "off", ""))
FAILOVER_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
]
# Attempts per model while a failover chain is in play — kept small so we reach
# a healthy model fast. A lone model (failover off / nothing to fall back to)
# keeps the full MAX_RETRIES budget instead.
FAILOVER_RETRIES = int(os.environ.get("GEMINI_FAILOVER_RETRIES", "2"))
UPLOAD_TIMEOUT = float(os.environ.get("GEMINI_UPLOAD_TIMEOUT", "900"))
POLL_INTERVAL = 2.0
# Each upload is retried this many times before the window is skipped — a flaky
# network often times out / resets the first connection but succeeds on a retry.
UPLOAD_ATTEMPTS = max(1, int(os.environ.get("GEMINI_UPLOAD_ATTEMPTS", "3")))
MAX_TOKENS_SEG = int(os.environ.get("GEMINI_MAX_TOKENS_SEG", "2048"))
MAX_TOKENS_REDUCE = int(os.environ.get("GEMINI_MAX_TOKENS_REDUCE", "1500"))

_MEDIA_RES = {
    "low": "MEDIA_RESOLUTION_LOW",
    "medium": "MEDIA_RESOLUTION_MEDIUM",
    "high": "MEDIA_RESOLUTION_HIGH",
}.get(os.environ.get("GEMINI_MEDIA_RESOLUTION", "low").lower(), "MEDIA_RESOLUTION_LOW")


SEG_PROMPT = """This is a {secs}-second clip of a screen recording (video plus
its audio). Identify the distinct activities the user worked on, in time order.
Reply with ONE JSON object and no prose:
{{"blocks":[
  {{"start": <seconds from the start of THIS clip>,
    "end": <seconds from the start of THIS clip>,
    "activity": "<=5 word label of the main task",
    "app": "main app or website",
    "detail": "one sentence on what happened",
    "speech": "brief note of anything said aloud in this span, else empty",
    "todos": ["a task the user explicitly said they still need to do"]}}
]}}
Use 1-6 blocks. Only state what the video/audio support; use "" or [] when unknown."""

REDUCE_PROMPT = """Below is a chronological list of activity blocks from a
user's day at their computer (time | activity | app | detail | todos).

Synthesize it into ONE JSON object (no prose):
{{"summary":"3-5 sentences: what the day was mostly about and what got done",
  "accomplishments":["concrete things the user completed or made progress on"],
  "action_items":["things the user still needs to do / follow up on"],
  "topics":["canonical project or topic names, 2-6 of them"]}}
Merge duplicates. Be specific and refer to real apps/files/topics seen.

Activity blocks:
{blocks}"""


# --------------------------------------------------------------------------
# Availability / client
# --------------------------------------------------------------------------
def _api_key():
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def available():
    """True if the Gemini path can run: SDK importable and a key is present."""
    if not _api_key():
        return False
    try:
        import google.genai  # noqa: F401
        return True
    except Exception:
        return False


_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_SETTINGS_PATH = os.path.join(_APP_DIR, "_settings.json")


def _read_settings():
    try:
        with open(_SETTINGS_PATH, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def proxy_url():
    """Outbound proxy for reaching Google, or '' for a direct connection.

    Many networks can't open a direct connection to
    ``generativelanguage.googleapis.com`` — a corporate firewall, or a region
    where Google itself is blocked. The signature is every upload / generate /
    embed failing with ``WinError 10060`` (connection timed out) or ``10013``
    (socket forbidden). The fix is to tunnel through a local proxy / VPN, e.g. a
    Clash / V2Ray client listening on ``http://127.0.0.1:7890``.

    Resolution, first non-empty wins: ``RECORDER_PROXY``, then the standard
    ``HTTPS_PROXY`` / ``ALL_PROXY`` env vars, then the ``"proxy"`` key in
    ``_settings.json`` (set from the recorder GUI)."""
    for k in ("RECORDER_PROXY", "HTTPS_PROXY", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        v = os.environ.get(k)
        if v and v.strip():
            return v.strip()
    v = _read_settings().get("proxy")
    return v.strip() if isinstance(v, str) and v.strip() else ""


def _http_timeout():
    """httpx timeout for Google calls: fail a blocked CONNECT fast (so we retry
    or report instead of hanging for minutes — we saw an 18-minute hang) while
    still allowing a generous window to upload a proxy clip / stream a reply."""
    import httpx
    return httpx.Timeout(connect=20.0, read=300.0, write=600.0, pool=20.0)


def _ssl_verify():
    """What httpx should use to verify Google's TLS certificate.

    Corporate VPNs / firewalls (Xgate, Zscaler, …) and some antivirus do HTTPS
    inspection: they terminate TLS and re-sign it with their *own* root CA.
    Browsers trust that CA because IT installed it in the Windows certificate
    store, but Python ships its own bundle (certifi) and ignores the OS store, so
    every upload fails with
    ``CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain``.

    Resolution (first that applies wins):
      1. ``RECORDER_SSL_VERIFY`` = 0/false/no/off, or settings ``ssl_verify``
         false -> verification OFF (INSECURE; last resort).
      2. ``RECORDER_CA_BUNDLE`` env / settings ``ca_bundle`` -> use that PEM file.
      3. ``truststore`` over the OS trust store -> trusts the same CAs the browser
         already does (the usual fix for VPN/firewall HTTPS inspection).
      4. otherwise -> default (certifi) verification.
    """
    s = _read_settings()
    v = os.environ.get("RECORDER_SSL_VERIFY", "").strip().lower()
    if v in ("0", "false", "no", "off") or s.get("ssl_verify") is False:
        return False
    ca = os.environ.get("RECORDER_CA_BUNDLE", "").strip()
    if not ca and isinstance(s.get("ca_bundle"), str):
        ca = s["ca_bundle"].strip()
    if ca and os.path.isfile(ca):
        return ca
    try:
        import ssl
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        return True


_CLIENT = None
_INSECURE_WARNED = False


def _client():
    global _CLIENT
    if _CLIENT is None:
        from google import genai
        from google.genai import types
        args = {"timeout": _http_timeout()}
        prox = proxy_url()
        if prox:
            args["proxy"] = prox
        verify = _ssl_verify()
        args["verify"] = verify
        if verify is False:
            global _INSECURE_WARNED
            if not _INSECURE_WARNED:
                _INSECURE_WARNED = True
                print("[gemini] WARNING: TLS certificate verification is DISABLED "
                      "(RECORDER_SSL_VERIFY=0) — uploads are not protected against "
                      "interception.", flush=True)
        # client_args flow straight to the underlying httpx client; setting both
        # sync + async keeps a future async path on the same proxy/timeout/verify.
        http_options = types.HttpOptions(client_args=args,
                                         async_client_args=dict(args))
        _CLIENT = genai.Client(api_key=_api_key(), http_options=http_options)
    return _CLIENT


def reset_client():
    """Drop the cached client so the next call rebuilds it — call after changing
    the proxy (or key) so the new value takes effect without a restart."""
    global _CLIENT
    _CLIENT = None


# Names that pass the 1M-token filter but aren't general video-analysis chat
# models (image-gen, speech, robotics, etc.) — drop them from the picker.
_MODEL_DENY = ("tts", "-image", "image-", "embedding", "robotic",
               "computer-use", "deep-research", "audio", "vision")
_MODELS_CACHE = None


def _is_video_model(m):
    """True for the large-context multimodal Gemini chat models suitable for
    analyzing screen video. Uses the 1M input-token limit as the key signal
    (image/TTS/embedding variants are far smaller) plus a small denylist."""
    name = (getattr(m, "name", "") or "").replace("models/", "")
    if not name.startswith("gemini-"):
        return False
    if "generateContent" not in (getattr(m, "supported_actions", None) or []):
        return False
    if (getattr(m, "input_token_limit", 0) or 0) < 1_000_000:
        return False
    if re.search(r"-\d{3}$", name):   # drop version-pinned dupes like -001
        return False
    return not any(d in name for d in _MODEL_DENY)


def list_models(force=False):
    """Live list of video-capable Gemini model names from the API (cached).

    The familiar KNOWN_MODELS come first (in their curated order), then any
    newer/extra models the key has access to, sorted. Never raises: falls back
    to KNOWN_MODELS if there's no key or the API can't be reached, and only
    caches a real (non-empty) API result so a transient failure isn't sticky."""
    global _MODELS_CACHE
    if _MODELS_CACHE is not None and not force:
        return list(_MODELS_CACHE)
    names = []
    if available():
        try:
            for m in _client().models.list():
                if _is_video_model(m):
                    names.append((m.name or "").replace("models/", ""))
        except Exception:
            names = []
    live = set(names)
    if not live:
        return list(KNOWN_MODELS)
    known = [m for m in KNOWN_MODELS if m in live]
    extra = sorted(m for m in live if m not in KNOWN_MODELS)
    _MODELS_CACHE = known + extra
    return list(_MODELS_CACHE)


# Embedding models the key can use for semantic search (separate from the
# video-analysis models above). Verified live: an AI-Studio key exposes
# gemini-embedding-001 (2048-tok input, 3072-dim default) plus the newer
# gemini-embedding-2 / -2-preview (8192-tok input). Output dim is configurable
# (we default to 768 — see embed.GEMINI_DIM).
KNOWN_EMBED_MODELS = [
    "gemini-embedding-001",
    "gemini-embedding-2",
    "gemini-embedding-2-preview",
]
_EMBED_MODELS_CACHE = None


def _is_embed_model(m):
    """True for models that support the embedContent action."""
    return "embedContent" in (getattr(m, "supported_actions", None) or [])


def list_embed_models(force=False):
    """Live list of embedding-capable model names from the API (cached).

    Mirrors :func:`list_models`: KNOWN_EMBED_MODELS first, then any extras the
    key has access to, sorted. Never raises — falls back to KNOWN_EMBED_MODELS
    offline / without a key, and only caches a real (non-empty) API result."""
    global _EMBED_MODELS_CACHE
    if _EMBED_MODELS_CACHE is not None and not force:
        return list(_EMBED_MODELS_CACHE)
    names = []
    if available():
        try:
            for m in _client().models.list():
                if _is_embed_model(m):
                    names.append((m.name or "").replace("models/", ""))
        except Exception:
            names = []
    live = set(names)
    if not live:
        return list(KNOWN_EMBED_MODELS)
    known = [m for m in KNOWN_EMBED_MODELS if m in live]
    extra = sorted(m for m in live if m not in KNOWN_EMBED_MODELS)
    _EMBED_MODELS_CACHE = known + extra
    return list(_EMBED_MODELS_CACHE)


# --------------------------------------------------------------------------
# Parsing / timestamp helpers (pure — unit-testable without the network)
# --------------------------------------------------------------------------
def _num(x):
    """Coerce a model time value to float seconds. Accepts numbers and the
    'SS', 'MM:SS' or 'HH:MM:SS' string forms Gemini sometimes emits."""
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            pass
        if ":" in s:
            try:
                sec = 0.0
                for p in s.split(":"):
                    sec = sec * 60 + float(p)
                return sec
            except ValueError:
                return None
    return None


def _clean(x):
    return str(x).strip() if x is not None else ""


def _abs_time(x, w0, w1, base):
    """Map a model-reported time to a session-absolute second.

    The model is asked for clip-relative seconds, but some replies use
    file-absolute times instead. Disambiguate: a value already inside the
    window (with slack) is treated as file-absolute; otherwise it's relative to
    the window start. Result is clamped to the window and offset by ``base``
    (the segment's position within the whole session)."""
    if x is None:
        return None
    a = x if (w0 - 2.0 <= x <= w1 + 2.0) else (w0 + x)
    a = min(max(a, w0), w1)
    return base + a


def _coerce_blocks(raw, w0, w1, base):
    """Normalize the model's per-window blocks into the shape insights.py uses
    (minus the thumbnail, added later): {t0,t1,activity,app,detail,speech,todos}
    with session-absolute t0/t1. Missing/degenerate times are filled by evenly
    dividing the window so the timeline and time-breakdown still make sense."""
    wlen = max(1e-6, w1 - w0)
    items = []
    for b in (raw or []):
        if not isinstance(b, dict):
            continue
        s, e = _num(b.get("start")), _num(b.get("end"))
        items.append({
            "t0": _abs_time(s, w0, w1, base),
            "t1": _abs_time(e, w0, w1, base),
            "activity": _clean(b.get("activity")) or "Activity",
            "app": _clean(b.get("app")),
            "detail": _clean(b.get("detail")),
            "speech": _clean(b.get("speech")),
            "todos": [t for t in (_clean(t) for t in (b.get("todos") or [])) if t],
        })
    if not items:
        return []
    n = len(items)
    for i, it in enumerate(items):
        if it["t0"] is None:
            it["t0"] = base + w0 + wlen * i / n
        if it["t1"] is None or it["t1"] <= it["t0"]:
            it["t1"] = base + w0 + wlen * (i + 1) / n
    items.sort(key=lambda x: x["t0"])
    lo, hi = base + w0, base + w1
    for it in items:
        it["t0"] = min(max(it["t0"], lo), hi)
        it["t1"] = min(max(it["t1"], it["t0"] + 1.0), hi)
    return items


def _parse(txt):
    """Pull a JSON value out of a model reply.

    With response_mime_type=application/json the reply is usually pure JSON, so
    try a whole-string load first (preserves the top-level type — dict OR list).
    Fall back to a ```json fence, then to a bracket scan for prose-wrapped JSON
    (analyze.extract_json, which prefers the first object)."""
    if not txt:
        return None
    t = txt.strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        inner = m.group(1).strip()
        try:
            return json.loads(inner)
        except Exception:
            t = inner
    return analyze.extract_json(t)


def _windows(dur):
    """List of (start, end) second-pairs tiling a segment of length ``dur``."""
    if dur <= 0:
        return [(0.0, float(WINDOW_SECONDS))]
    out, t = [], 0.0
    while t < dur - 0.5:
        out.append((t, min(dur, t + WINDOW_SECONDS)))
        t += WINDOW_SECONDS
    return out or [(0.0, dur)]


# --------------------------------------------------------------------------
# Network plumbing (Files API + generation, throttled & retried)
# --------------------------------------------------------------------------
_LAST_CALL = [0.0]


def _throttle():
    gap = MIN_INTERVAL - (time.time() - _LAST_CALL[0])
    if gap > 0:
        time.sleep(gap)
    _LAST_CALL[0] = time.time()


def _state(f):
    s = getattr(f, "state", None)
    return getattr(s, "name", str(s)) if s is not None else "UNKNOWN"


def _upload_once(client, path):
    """One upload attempt: send the file and block until ACTIVE (or raise)."""
    f = client.files.upload(file=path)
    waited = 0.0
    while _state(f) == "PROCESSING":
        time.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL
        if waited > UPLOAD_TIMEOUT:
            raise TimeoutError("file processing timed out")
        f = client.files.get(name=f.name)
    st = _state(f)
    if st != "ACTIVE":
        raise RuntimeError(f"upload not ACTIVE (state={st})")
    return f


def _upload_active(client, path, log):
    """Upload a file and block until ACTIVE, retrying on failure.

    On flaky / restricted networks the first connection often times out or is
    reset (WinError 10060 / 10013); a retry with a short backoff frequently
    succeeds. Raises the last error only after every attempt fails."""
    last = None
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        try:
            return _upload_once(client, path)
        except Exception as e:
            last = e
            if attempt < UPLOAD_ATTEMPTS:
                back = 2.0 * attempt
                log(f"      upload attempt {attempt}/{UPLOAD_ATTEMPTS} failed "
                    f"({e}); retrying in {back:.0f}s…")
                time.sleep(back)
    raise last


_NET_WARNED = False


def _is_cert_error(e):
    """True for a TLS trust failure — the hallmark of a VPN/firewall doing HTTPS
    inspection with its own root CA (vs. the network simply being unreachable)."""
    s = str(e)
    return ("CERTIFICATE_VERIFY_FAILED" in s
            or "certificate verify failed" in s.lower()
            or "self-signed certificate" in s
            or "self signed certificate" in s)


def _warn_network_blocked(log, err=None):
    """Print one clear, actionable hint when every upload fails. A TLS trust
    error (HTTPS inspection) gets cert-specific advice; otherwise it's almost
    always the network not reaching Google. Guarded so live mode doesn't repeat
    it every chunk."""
    global _NET_WARNED
    if _NET_WARNED:
        return
    _NET_WARNED = True
    if _is_cert_error(err):
        log("  ! Every upload failed TLS verification (self-signed certificate "
            "in chain). Your VPN/firewall is inspecting HTTPS and presenting its "
            "own certificate. Fix: let the recorder trust the Windows certificate "
            "store (which your browser already uses) by installing it once —  "
            "pip install truststore  — then restart; no other setup needed. "
            "Or point RECORDER_CA_BUNDLE at your corporate root-CA .pem. Last "
            "resort:  setx RECORDER_SSL_VERIFY 0  (disables certificate checking "
            "— insecure).")
        return
    prox = proxy_url()
    if prox:
        log(f"  ! Every upload to Google failed even via the proxy ({prox}). "
            "Check the proxy/VPN is running and allows "
            "generativelanguage.googleapis.com.")
    else:
        log("  ! Every upload to Google FAILED — this network can't reach "
            "generativelanguage.googleapis.com (a firewall, or Google is "
            "blocked in your region). Route through a local proxy/VPN: put its "
            "address in the recorder's Proxy box (e.g. http://127.0.0.1:7890), "
            "or run  setx RECORDER_PROXY http://127.0.0.1:7890  and restart. "
            "Alternatively switch analysis to the Claude backend.")


def _gen_config(max_tokens):
    from google.genai import types
    return types.GenerateContentConfig(
        response_mime_type="application/json",
        media_resolution=_MEDIA_RES,
        temperature=0.2,
        max_output_tokens=max_tokens,
    )


def _video_part(f):
    """A video Part referencing an uploaded file. We deliberately do NOT set
    video_metadata(start/end offset): the Gemini API rejects clip-offset
    windowing on Files-API uploads ("video_metadata parameter is not supported"),
    so a sub-range is cut physically (see _make_upload_clip) and uploaded as its
    own file, which the model then sees from t=0."""
    from google.genai import types
    return types.Part(
        file_data=types.FileData(
            file_uri=f.uri, mime_type=getattr(f, "mime_type", None) or "video/mp4"),
    )


def _make_upload_clip(ffmpeg, seg, w0, w1, outpath, whole):
    """Build the lightweight proxy we actually upload for one window.

    Re-encodes ``seg`` (or its [w0, w1] sub-range when not ``whole``) down to
    PROXY_FPS fps with mono audio, resolution capped at PROXY_MAX_WIDTH. The
    result is ~13x smaller than the original with no loss of analysis quality
    (Gemini samples ~1 fps at LOW anyway), so live-mode uploads don't fall behind
    the recording. Re-encoding also makes the clip start exactly at w0, so the
    model's reported times are genuinely clip-relative. Returns True on success."""
    vf = f"scale='min({PROXY_MAX_WIDTH},iw)':-2"
    cmd = [ffmpeg, "-hide_banner", "-y"]
    if not whole:
        cmd += ["-ss", f"{w0:.2f}"]
    cmd += ["-i", seg]
    if not whole:
        cmd += ["-t", f"{max(0.1, w1 - w0):.2f}"]
    cmd += ["-r", f"{PROXY_FPS:g}", "-vf", vf,
            "-c:v", "libx264", "-crf", str(PROXY_CRF), "-preset", "veryfast",
            "-ac", "1", "-c:a", "aac", "-b:a", f"{PROXY_AUDIO_KBPS}k",
            "-movflags", "+faststart", outpath]
    try:
        analyze._run(cmd)
    except Exception:
        return False
    return os.path.isfile(outpath) and os.path.getsize(outpath) > 0


def _safe_rm(path):
    try:
        os.remove(path)
    except Exception:
        pass


def _retryable(msg):
    msg = msg.upper()
    return any(s in msg for s in ("429", "RESOURCE_EXHAUSTED", "503",
                                  "UNAVAILABLE", "500", "INTERNAL"))


def _is_quota(msg):
    """Daily-quota exhaustion (429) — won't clear until the quota resets, unlike
    a 503 overload which usually recovers on a retry a few seconds later."""
    msg = msg.upper()
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg


def _failover_chain(model):
    """The chosen model first, then the cheaper fallbacks (minus itself)."""
    if not AUTO_FAILOVER:
        return [model]
    return [model] + [m for m in FAILOVER_MODELS if m != model]


def _persist_model(old, new, log):
    """Make a quota-driven failover stick for the rest of the session so later
    calls skip the exhausted model. Touches only whichever global matched the
    abandoned model — session-only, since the daily quota resets, so we never
    rewrite the saved GUI preference."""
    global MODEL, REDUCE_MODEL
    changed = False
    if old == MODEL:
        MODEL = new
        changed = True
    if old == REDUCE_MODEL:
        REDUCE_MODEL = new
        changed = True
    if changed:
        log(f"  Gemini: {old} quota exhausted for today — using {new} for the "
            f"rest of this session.")


def _generate(client, model, contents, cfg, log, what="request"):
    """generate_content with throttling, backoff, and automatic model failover.

    The chosen model is tried first; on a retryable error (503 overload /
    429 quota / 5xx) we fail over through FAILOVER_MODELS (cheapest first,
    excluding 2.5-pro) and return the first success. If the primary is abandoned
    because its daily quota is exhausted (429), the switch is persisted for the
    session so subsequent chunks don't re-pay its retry cost on every call."""
    chain = _failover_chain(model)
    # A lone model keeps the full retry budget; in a chain each model gets fewer
    # tries so we reach a healthy one quickly.
    per_model = MAX_RETRIES if len(chain) == 1 else max(1, FAILOVER_RETRIES)
    last = None
    primary_quota_hit = False
    for ci, m in enumerate(chain):
        for attempt in range(per_model):
            _throttle()
            try:
                resp = client.models.generate_content(
                    model=m, contents=contents, config=cfg)
                if ci > 0:
                    log(f"  Gemini {what}: succeeded on fallback model {m}.")
                    if primary_quota_hit:
                        _persist_model(model, m, log)
                return resp
            except Exception as e:  # SDK raises various error subclasses
                last = e
                msg = str(e)
                if not _retryable(msg):
                    raise
                if ci == 0 and _is_quota(msg):
                    primary_quota_hit = True
                if attempt < per_model - 1:
                    wait = RETRY_BACKOFF * (attempt + 1)
                    log(f"  Gemini {what}: retrying in {wait:.0f}s ({msg[:90]})")
                    time.sleep(wait)
                    continue
                if ci < len(chain) - 1:
                    log(f"  Gemini {what}: {m} unavailable — trying "
                        f"{chain[ci + 1]} ({msg[:70]})")
        # this model's attempts are spent; the loop advances to the next one
    raise last


def _resp_text(resp):
    """Best-effort text out of a response. ``resp.text`` raises (not just None)
    when a reply is empty or safety-blocked, so guard it and fall back to
    walking the candidate parts."""
    try:
        t = resp.text
        if t:
            return t
    except Exception:
        pass
    try:
        for c in (getattr(resp, "candidates", None) or []):
            parts = getattr(getattr(c, "content", None), "parts", None) or []
            txt = "".join(getattr(p, "text", "") or "" for p in parts)
            if txt:
                return txt
    except Exception:
        pass
    return None


def _grab_frame(ffmpeg, seg, t, outpath):
    """Extract a single thumbnail at second ``t`` of ``seg`` (relative to the
    segment). Returns True on success."""
    analyze._run([ffmpeg, "-hide_banner", "-y", "-ss", f"{max(0.0, t):.2f}",
                  "-i", seg, "-frames:v", "1",
                  "-vf", f"scale={THUMB_SCALE}:-1", "-q:v", "5", outpath])
    return os.path.isfile(outpath) and os.path.getsize(outpath) > 0


# --------------------------------------------------------------------------
# Public API — mirrors the (_collect + _map_blocks) and _reduce stages
# --------------------------------------------------------------------------
def analyze_one(seg, base, ffmpeg, log=print, client=None, thumbdir=None):
    """Analyze a single finished segment with Gemini.

    Uploads a lightweight proxy of the segment (see _make_upload_clip), issues
    windowed generate_content calls, and returns
    ``(blocks, transcript, dur, ok)`` with session-absolute times — every time is
    offset by ``base`` (the segment's start position within the whole session),
    so a caller can run one finished chunk at a time and keep a running ``base``.
    Thumbnails go in ``thumbdir`` (defaults to ``_gem_thumbs`` beside the
    segment) and are named after the segment stem so they never collide across
    chunks.

      blocks     -> [{t0,t1,thumb,activity,app,detail,todos}]
      transcript -> [(t0, t1, speech)]
      ok         -> True if no upload failed (safe to checkpoint); False when a
                    network upload failed, so the caller can leave the segment
                    pending and retry it later. (Local proxy-build skips do NOT
                    set ok=False — they would never self-heal on retry.)
    """
    client = client or _client()
    name = os.path.basename(seg)
    stem = os.path.splitext(name)[0]
    if thumbdir is None:
        thumbdir = os.path.join(os.path.dirname(seg), "_gem_thumbs")
    os.makedirs(thumbdir, exist_ok=True)

    dur = analyze.get_duration(seg, ffmpeg) or 0.0
    if dur <= 0:
        log(f"  {name}: unknown duration — analyzing as a single window.")
    wins = _windows(dur)
    whole = len(wins) <= 1

    blocks, transcript = [], []
    tidx = 0
    upload_fails = 0
    last_upload_err = None
    for wi, (w0, w1) in enumerate(wins):
        # We always upload a lightweight proxy (low-fps, mono audio), never the
        # full-res original — see _make_upload_clip. For a multi-window segment
        # each window is its own proxy (the API can't clip an upload by offset).
        # The proxy is a temp file we delete after the window.
        upload_path = os.path.join(thumbdir, f"{stem}_up{wi:03d}.mp4")
        if whole:
            log(f"  preparing + uploading {name} "
                f"({analyze.fmt_clock(dur)}, proxy)…")
        else:
            log(f"  window {wi + 1}/{len(wins)} "
                f"{analyze.fmt_clock(base + w0)}–{analyze.fmt_clock(base + w1)}: "
                f"preparing + uploading…")
        if not _make_upload_clip(ffmpeg, seg, w0, w1, upload_path, whole):
            log("      proxy build failed; skipping window.")
            continue
        try:
            f = _upload_active(client, upload_path, log)
        except Exception as e:
            upload_fails += 1
            last_upload_err = e
            log(f"  upload failed for {os.path.basename(upload_path)}: {e}; skipping.")
            _safe_rm(upload_path)
            continue
        try:
            contents = [_video_part(f),
                        SEG_PROMPT.format(secs=int(round(w1 - w0)))]
            try:
                resp = _generate(client, MODEL, contents,
                                 _gen_config(MAX_TOKENS_SEG), log, "segment")
                data = _parse(_resp_text(resp))
            except Exception as e:
                log(f"      window error: {e}")
                continue
            raw = (data.get("blocks") if isinstance(data, dict)
                   else data if isinstance(data, list) else [])
            for blk in _coerce_blocks(raw, w0, w1, base):
                mid_rel = (blk["t0"] + blk["t1"]) / 2.0 - base
                thumb = os.path.join(thumbdir, f"{stem}_{tidx:04d}.jpg")
                tidx += 1
                if not _grab_frame(ffmpeg, seg, mid_rel, thumb):
                    thumb = None
                blocks.append({"t0": blk["t0"], "t1": blk["t1"], "thumb": thumb,
                               "activity": blk["activity"], "app": blk["app"],
                               "detail": blk["detail"], "todos": blk["todos"]})
                if blk["speech"]:
                    transcript.append((blk["t0"], blk["t1"], blk["speech"]))
                log(f"      {analyze.fmt_clock(blk['t0'])}: {blk['activity']}")
        finally:
            try:
                client.files.delete(name=f.name)  # don't leave it on their servers
            except Exception:
                pass
            _safe_rm(upload_path)  # the proxy was a temp file

    if wins and upload_fails == len(wins):
        _warn_network_blocked(log, last_upload_err)

    blocks.sort(key=lambda b: b["t0"])
    transcript.sort(key=lambda t: t[0])
    ok = (upload_fails == 0)
    return blocks, transcript, dur, ok


def analyze_video(session_dir, ffmpeg, log=print):
    """Native-video analysis of a whole session (batch, at stop).

    Returns (blocks, transcript, total_dur, tmp_dirs) with the same shapes the
    Claude pipeline produces:
      blocks     -> [{t0,t1,thumb,activity,app,detail,todos}]
      transcript -> [(t0, t1, speech)]  (one entry per block that had speech)
      tmp_dirs   -> dirs for the caller to clean up afterwards
    """
    segs = sorted(glob.glob(os.path.join(session_dir, "seg_*.mp4"))) or \
        sorted(glob.glob(os.path.join(session_dir, "*.mp4")))
    if not segs:
        log("Gemini: no segments found.")
        return [], [], 0.0, []

    client = _client()
    thumbdir = os.path.join(session_dir, "_gem_thumbs")
    os.makedirs(thumbdir, exist_ok=True)
    tmp_dirs = [thumbdir]
    blocks, transcript = [], []
    base = 0.0

    for seg in segs:
        b, t, dur, _ok = analyze_one(seg, base, ffmpeg, log=log,
                                     client=client, thumbdir=thumbdir)
        blocks += b
        transcript += t
        base += dur

    blocks.sort(key=lambda b: b["t0"])
    transcript.sort(key=lambda t: t[0])
    log(f"Gemini produced {len(blocks)} activity block(s).")
    return blocks, transcript, base, tmp_dirs


def _fallback_reduce(blocks):
    """Local synthesis when the Gemini reduce call fails — still collect to-dos."""
    todos = []
    for blk in blocks:
        todos += blk.get("todos") or []
    total = sum((b["t1"] - b["t0"]) for b in blocks) if blocks else 0
    return {
        "summary": f"Recorded {analyze.fmt_clock(total)} across {len(blocks)} "
                   f"block(s). Gemini synthesis was unavailable.",
        "accomplishments": [],
        "action_items": analyze.clean_todos(todos),
        "topics": [],
    }


def reduce(blocks, log=print):
    """Daily synthesis on Gemini. Returns the insights dict the dashboard wants:
    {summary, accomplishments, action_items, topics}."""
    if not blocks:
        return {"summary": "", "accomplishments": [], "action_items": [], "topics": []}

    lines = []
    for blk in blocks:
        todos = "; ".join(blk.get("todos") or []) or "-"
        lines.append(f"{analyze.fmt_clock(blk['t0'])}-{analyze.fmt_clock(blk['t1'])} | "
                     f"{blk.get('activity', '')} | {blk.get('app', '')} | "
                     f"{blk.get('detail', '')} | {todos}")
    prompt = REDUCE_PROMPT.format(blocks="\n".join(lines)[:15000])
    try:
        resp = _generate(_client(), REDUCE_MODEL, [prompt],
                         _gen_config(MAX_TOKENS_REDUCE), log, "reduce")
        data = _parse(_resp_text(resp))
    except Exception as e:
        log(f"Gemini reduce error: {e}")
        data = None

    if not isinstance(data, dict):
        return _fallback_reduce(blocks)

    # Fold in every per-block to-do, then tidy + fuzzy-de-duplicate the whole
    # set so reworded variants and cue-prefixed dupes collapse to one clean item.
    merged = list(data.get("action_items") or [])
    for blk in blocks:
        merged += blk.get("todos") or []
    data["action_items"] = analyze.clean_todos(merged)
    data.setdefault("summary", "")
    data.setdefault("accomplishments", [])
    data.setdefault("topics", [])
    return data
