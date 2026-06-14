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
