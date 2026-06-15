"""Low-level helpers: frame extraction, transcription, and Claude calls.

Higher-level orchestration (chunked insights, reports, knowledge base) lives
in insights.py and kb.py.
"""

import os
import re
import json
import base64
import tempfile
import subprocess

MAP_MODEL = os.environ.get("MAP_MODEL", "claude-haiku-4-5-20251001")   # per-block, cheap/fast
REDUCE_MODEL = os.environ.get("REDUCE_MODEL", "claude-opus-4-7")        # daily synthesis
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")                 # tiny|base|small|medium


def _run(cmd):
    return subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def have_key():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def fmt_clock(sec):
    sec = int(sec or 0)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# --------------------------------------------------------------------------
# To-do hygiene (shared by insights.py and gemini.py reduce steps)
#
# Raw to-dos arrive three ways and each is messy in its own way:
#   * the AI synthesis sometimes keeps reworded variants ("Meet Jay" /
#     "Meet with Jay") or conversational lead-ins ("I need to change to Toyama");
#   * per-block to-dos folded in afterwards duplicate the synthesis with slight
#     wording changes (old dedup was exact-lowercase only, so they slipped through);
#   * the no-key local fallback dumps raw transcript sentences, so questions and
#     rambling fragments leak in as "tasks".
# clean_todos() tidies, filters, and fuzzy-de-duplicates a list so the report
# and knowledge base show clean, distinct items. Run it on every action_items
# list a reduce/fallback produces.
# --------------------------------------------------------------------------

# A single leading conversational cue to strip ("I need to ...", "Let's ...",
# "TODO: ...") so the surviving text is the bare task.
_TODO_CUE_RE = re.compile(
    r"^\s*(?:"
    r"i\s+will\s+|i'?ll\s+|i\s+am\s+going\s+to\s+|i'?m\s+going\s+to\s+|"
    r"i\s+need\s+to\s+|i\s+have\s+to\s+|i\s+want\s+to\s+|i\s+would\s+like\s+to\s+|"
    r"i\s+should\s+|i\s+plan\s+to\s+|i\s+need\s+to\s+go\s+and\s+|"
    r"we\s+need\s+to\s+|we\s+should\s+|we\s+have\s+to\s+|we'?ll\s+|"
    r"need\s+to\s+|have\s+to\s+|got\s+to\s+|gotta\s+|going\s+to\s+|"
    r"let'?s\s+|remember\s+to\s+|don'?t\s+forget\s+to\s+|"
    r"make\s+sure\s+to\s+|be\s+sure\s+to\s+|"
    r"follow[\s-]*up\s+(?:on|with)\s+|"
    r"action\s+item\s*:?\s*|to[\s-]*do\s*:?\s*|todo\s*:?\s*|task\s*:?\s*|"
    r"must\s+|please\s+"
    r")",
    re.IGNORECASE,
)

# Common words ignored when comparing two to-dos for "same task". Keeping verbs
# and nouns means "publish library github" still matches "publish desktop
# recorder library on github" by token containment.
_TODO_STOP = {
    "a", "an", "the", "this", "that", "these", "those", "to", "of", "on",
    "in", "for", "with", "and", "or", "my", "our", "your", "their", "his",
    "her", "its", "at", "by", "from", "into", "is", "are", "be", "am", "it",
    "i", "we", "you", "do", "did", "done", "some", "any", "out", "up", "all",
}


def _todo_core(s):
    """Lowercased task text with the leading cue and outer punctuation removed."""
    t = (str(s) if s is not None else "").lower()
    t = _TODO_CUE_RE.sub("", t, count=1)
    t = re.sub(r"\s+", " ", t).strip(" \t.;,:-•*")
    return t


def _todo_tokens(s):
    """Set of meaningful word tokens for similarity comparison."""
    core = re.sub(r"[^a-z0-9\s]", " ", _todo_core(s))
    return {w for w in core.split() if w and w not in _TODO_STOP}


def todos_similar(a, b):
    """Heuristic: do two to-do strings describe the same task?

    Catches reworded variants ("Meet Jay" / "Meet with Jay"), cue-prefixed
    dupes ("Need to change to Toyama" / "Change travel plans to Toyama"), and
    high-overlap rephrasings ("Publish this library on GitHub" / "Publish
    Desktop Recorder library on GitHub")."""
    ca, cb = _todo_core(a), _todo_core(b)
    if not ca or not cb:
        return ca == cb
    if ca == cb:
        return True
    ta, tb = _todo_tokens(a), _todo_tokens(b)
    if not ta or not tb:
        return ca == cb
    smaller, larger = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    # one task's key words fully contained in the other -> same task, more detail
    if len(smaller) >= 2 and smaller <= larger:
        return True
    # otherwise require strong word overlap (Jaccard) for a reworded match
    inter = len(ta & tb)
    union = len(ta | tb)
    return bool(union) and inter / union >= 0.6


def clean_one_todo(s):
    """Tidy a single to-do for display: drop list bullets, strip a leading
    conversational cue, trim trailing punctuation, and capitalize. Returns ''
    when nothing meaningful is left."""
    t = (str(s) if s is not None else "").strip().lstrip("-*•·●◦▪>").strip()
    stripped = _TODO_CUE_RE.sub("", t, count=1).strip()
    if stripped:                      # don't blank out a to-do that was ALL cue
        t = stripped
    t = t.strip(" \t.;,").strip()
    if t:
        t = t[0].upper() + t[1:]
    return t


def _looks_like_todo(t):
    """Reject conversational transcript fragments masquerading as to-dos.

    Keeps short, actionable items; drops questions, filler, and rambling
    sentences. Clean AI-produced to-dos pass; raw transcript dumps don't."""
    t = (t or "").strip()
    if not t or "?" in t:                       # questions aren't tasks
        return False
    words = t.split()
    if len(words) < 2 or len(words) > 16:       # fragment / transcript ramble
        return False
    if len(t) > 120:
        return False
    low = t.lower()
    bad_starts = (
        "so ", "and ", "but ", "because ", "well ", "yeah", "yes ", "no ",
        "okay", "ok ", "i think", "i guess", "i mean", "i feel", "maybe ",
        "you know", "like ", "that's ", "it's ", "there's ", "this is ",
        "that is ", "what ", "why ", "how ", "when ", "where ", "who ",
    )
    return not low.startswith(bad_starts)


def clean_todos(items):
    """Tidy, filter, and fuzzy-de-duplicate a list of to-do strings.

    Order is preserved (first occurrence keeps its position); within a cluster
    of near-duplicates the most descriptive (longest) phrasing is kept."""
    cleaned = []
    for raw in (items or []):
        t = clean_one_todo(raw)
        if _looks_like_todo(t):
            cleaned.append(t)
    kept = []
    for t in cleaned:
        hit = next((i for i, k in enumerate(kept) if todos_similar(t, k)), None)
        if hit is None:
            kept.append(t)
        elif len(t) > len(kept[hit]):
            kept[hit] = t            # prefer the more descriptive variant
    return kept


def get_duration(path, ffmpeg):
    ffprobe = ffmpeg.replace("ffmpeg.exe", "ffprobe.exe").replace("ffmpeg", "ffprobe")
    if os.path.isfile(ffprobe) or ffprobe == "ffprobe":
        p = _run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                  "-of", "default=noprint_wrappers=1:nokey=1", path])
        try:
            return float(p.stdout.strip())
        except ValueError:
            pass
    p = _run([ffmpeg, "-hide_banner", "-i", path])
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", p.stderr)
    if m:
        h, mm, ss = m.groups()
        return int(h) * 3600 + int(mm) * 60 + float(ss)
    return 0.0


def extract_frames(path, ffmpeg, fps, scale=480, outdir=None, log=print):
    """Extract frames at `fps` frames/second. Returns [(path, t_seconds), ...]."""
    if outdir is None:
        outdir = tempfile.mkdtemp(prefix="frames_", dir=os.path.dirname(path) or None)
    os.makedirs(outdir, exist_ok=True)
    pattern = os.path.join(outdir, "f_%05d.jpg")
    _run([ffmpeg, "-hide_banner", "-y", "-i", path,
          "-vf", f"fps={fps},scale={scale}:-1", "-q:v", "5", pattern])
    files = sorted(f for f in os.listdir(outdir) if f.endswith(".jpg"))
    interval = 1.0 / fps if fps else 1.0
    return [(os.path.join(outdir, f), i * interval) for i, f in enumerate(files)]


def transcribe_audio(path, ffmpeg, log=print):
    """Return [(start, end, text), ...] ([] if no audio / unavailable)."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log("faster-whisper not installed — skipping transcript.")
        return []

    wav = os.path.splitext(path)[0] + "_a16.wav"
    _run([ffmpeg, "-hide_banner", "-y", "-i", path, "-vn",
          "-ac", "1", "-ar", "16000", wav])
    if not os.path.isfile(wav) or os.path.getsize(wav) < 1000:
        return []
    try:
        model = _whisper(WHISPER_MODEL)
        segments, info = model.transcribe(wav, vad_filter=True)
        return [(s.start, s.end, s.text.strip()) for s in segments]
    except Exception as e:
        log(f"Transcription error: {e}")
        return []
    finally:
        try:
            os.remove(wav)
        except Exception:
            pass


_WHISPER_CACHE = {}


def _whisper(name):
    if name not in _WHISPER_CACHE:
        from faster_whisper import WhisperModel
        _WHISPER_CACHE[name] = WhisperModel(name, device="cpu", compute_type="int8")
    return _WHISPER_CACHE[name]


def img_block(path):
    with open(path, "rb") as f:
        b64 = base64.standard_b64encode(f.read()).decode()
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}}


def img_data_uri(path):
    with open(path, "rb") as f:
        return "data:image/jpeg;base64," + base64.standard_b64encode(f.read()).decode()


def extract_json(text):
    """Pull the first JSON object/array out of a model response."""
    for op, cl in (("{", "}"), ("[", "]")):
        try:
            i, j = text.index(op), text.rindex(cl)
            return json.loads(text[i:j + 1])
        except (ValueError, json.JSONDecodeError):
            continue
    return None


_CLIENT = None


def get_client():
    global _CLIENT
    if _CLIENT is None:
        import anthropic
        _CLIENT = anthropic.Anthropic()
    return _CLIENT


def call_json(model, content, max_tokens=1500, log=print):
    """Call Claude and parse a JSON object/array from the reply (None on failure)."""
    resp = get_client().messages.create(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": content}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    return extract_json(text)
