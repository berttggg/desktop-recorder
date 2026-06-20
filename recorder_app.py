"""Desktop Recorder + Analyzer.

A small Tkinter app that records the desktop (screen + optional microphone)
using ffmpeg, then asks Claude to summarize what you did during the session.
"""

import os
import re
import sys
import glob
import json
import time
import shutil
import threading
import subprocess
import datetime as dt
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

import analyze
import audio_capture
import insights
import process


def _refresh_env_from_registry():
    """Make freshly ``setx``-ed settings visible without a full re-login.

    On Windows, ``setx`` writes to the user/system *environment registry*, but a
    process only inherits the environment captured when its parent shell (usually
    Explorer) launched. An app started from a stale shell therefore won't see a
    just-set ANALYSIS_BACKEND or GEMINI_API_KEY — it silently falls back to the
    default backend. Re-read the persisted values for a small allowlist so the
    app behaves as if freshly launched (machine then user, so user wins, matching
    a fresh shell). PATH and everything else are left untouched. Never raises."""
    if os.name != "nt":
        return
    names = ("ANALYSIS_BACKEND", "GEMINI_API_KEY", "GOOGLE_API_KEY",
             "ANTHROPIC_API_KEY", "GEMINI_MODEL", "GEMINI_REDUCE_MODEL")
    try:
        import winreg
    except Exception:
        return
    for root, sub in (
        (winreg.HKEY_LOCAL_MACHINE,
         r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    ):
        try:
            key = winreg.OpenKey(root, sub)
        except OSError:
            continue
        try:
            for name in names:
                try:
                    val, _t = winreg.QueryValueEx(key, name)
                except OSError:
                    continue
                if val not in (None, ""):
                    os.environ[name] = str(val)
        finally:
            try:
                winreg.CloseKey(key)
            except Exception:
                pass


_refresh_env_from_registry()

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# Recording always captures straight into short *finalized* chunks so every
# completed seg_NNN.mp4 is a valid, analyzable file: a crash or shutdown can lose
# at most the single chunk being written, never the whole session. Nothing is
# uploaded during capture — the user processes accumulated recordings later with
# the Process button (see process.py). Shorter chunks = less lost on a crash.
CAPTURE_SEGMENT_SECONDS = int(os.environ.get("RECORDER_CAPTURE_SEGMENT_SECONDS", "600"))
# How often the background muxer checks for a newly-finalized chunk to fold in.
MUX_POLL_SECONDS = float(os.environ.get("RECORDER_MUX_POLL_SECONDS", "2"))
# How often (minutes) to quietly re-check which models/embeddings the key can use
# so the dropdowns stay current as Google adds/removes models. 0 disables.
MODEL_REFRESH_MIN = int(os.environ.get("RECORDER_MODEL_REFRESH_MIN", "30"))


def _default_rec_dir():
    # Keep large recordings off the (nearly full) C: drive.
    env = os.environ.get("RECORDER_OUTPUT_DIR")
    if env:
        return env
    if os.path.isdir("D:\\"):
        return r"D:\DesktopRecordings"
    return os.path.join(APP_DIR, "recordings")


REC_DIR = _default_rec_dir()
os.makedirs(REC_DIR, exist_ok=True)

# Small persisted UI prefs (currently just the chosen Gemini model) so a switch
# sticks across restarts — useful when a model's daily quota is used up and you
# want to stay on the lighter one for the rest of the day. Best-effort; never
# fatal if the file can't be read/written.
SETTINGS_PATH = os.path.join(APP_DIR, "_settings.json")


def _load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_settings(d):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass


def find_ffmpeg():
    """Return path to ffmpeg: bundled copy first, then PATH, then winget."""
    bundled = os.path.join(APP_DIR, "ffmpeg", "ffmpeg.exe")
    if os.path.isfile(bundled):
        return bundled
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    # winget installs Gyan.FFmpeg under Links or a versioned package dir.
    candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
    ]
    pkgs = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    if os.path.isdir(pkgs):
        for root, _dirs, files in os.walk(pkgs):
            if "ffmpeg.exe" in files:
                candidates.append(os.path.join(root, "ffmpeg.exe"))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def list_audio_devices(ffmpeg):
    """Return a list of DirectShow audio input device names."""
    if not ffmpeg:
        return []
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except Exception:
        return []
    out = (proc.stderr or "") + (proc.stdout or "")
    devices = []
    for line in out.splitlines():
        if "Alternative name" in line or "(audio)" not in line:
            continue
        m = re.search(r'"([^"]+)"', line)
        if m and m.group(1) not in devices:
            devices.append(m.group(1))
    return devices


# Color cues for the status line / backend badge.
_OK = "#2e7d32"      # green  — ready / good config
_BUSY = "#c0392b"    # red    — recording in progress
_WARN = "#b9770e"    # amber  — degraded (no key, local only, …)
_INFO = "#2e6fb0"    # blue   — working (saving / analyzing)


class _ToolTip:
    """Lightweight hover tooltip for any Tk/ttk widget (best-effort, never raises)."""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _e=None):
        if self.tip or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 18
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
            self.tip = tk.Toplevel(self.widget)
            self.tip.wm_overrideredirect(True)
            self.tip.wm_geometry(f"+{x}+{y}")
            tk.Label(self.tip, text=self.text, justify="left",
                     background="#1b1e27", foreground="#e6e8ee",
                     relief="solid", borderwidth=1, wraplength=320,
                     font=("Segoe UI", 9), padx=8, pady=5).pack()
        except Exception:
            self.tip = None

    def _hide(self, _e=None):
        if self.tip is not None:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


class RecorderApp:
    def __init__(self, root):
        self.root = root
        self.ffmpeg = find_ffmpeg()
        self.proc = None
        self.session_dir = None  # folder holding seg_*.mp4 for this session
        self.has_mic = False
        self.sys_rec = None      # SystemAudioRecorder instance
        self.start_time = None
        self.timer_job = None
        self.session_started_iso = None
        self.session_ended_iso = None
        self.mux_thread = None         # background chunk muxer (no upload)
        self.mux_stop_event = None     # signals the muxer to drain & finish
        self.processing = False        # a Process run is in progress
        self.proc_stop_event = None    # signals the Process worker to stop (resumable)
        self._pending_sessions = 0     # recordings awaiting Process (button label)
        self._settings = _load_settings()
        self._applied_model = None    # last Gemini model pushed into gemini.MODEL

        root.title("Desktop Recorder + Analyzer")
        root.geometry("700x620")
        root.minsize(620, 540)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        try:
            ttk.Style().theme_use("vista")   # native look on Windows
        except Exception:
            pass

        # ---- Header: app name + live backend badge ------------------------
        header = ttk.Frame(root)
        header.pack(fill="x", padx=14, pady=(12, 2))
        ttk.Label(header, text="Desktop Recorder",
                  font=("Segoe UI", 15, "bold")).pack(side="left")
        self.backend_badge = tk.Label(header, text="", font=("Segoe UI", 9, "bold"),
                                      fg="white", padx=9, pady=2)
        self.backend_badge.pack(side="right")

        # ---- Capture settings --------------------------------------------
        cfg = ttk.LabelFrame(root, text="Capture settings")
        cfg.pack(fill="x", padx=14, pady=8)
        cfg.columnconfigure(1, weight=1)

        ttk.Label(cfg, text="Microphone:").grid(row=0, column=0, sticky="w",
                                                padx=(10, 4), pady=8)
        self.audio_var = tk.StringVar()
        self.audio_combo = ttk.Combobox(cfg, textvariable=self.audio_var, state="readonly")
        self.audio_combo.grid(row=0, column=1, sticky="we", padx=4, pady=8)
        self.mic_refresh_btn = ttk.Button(cfg, text="↻", width=3,
                                          command=self.refresh_devices)
        self.mic_refresh_btn.grid(row=0, column=2, sticky="w", padx=(4, 10), pady=8)

        ttk.Label(cfg, text="Frame rate:").grid(row=1, column=0, sticky="w", padx=(10, 4))
        fps_row = ttk.Frame(cfg)
        fps_row.grid(row=1, column=1, columnspan=2, sticky="w", padx=4)
        self.fps_var = tk.StringVar(value="15")
        self.fps_spin = ttk.Spinbox(fps_row, from_=5, to=60, textvariable=self.fps_var, width=6)
        self.fps_spin.pack(side="left")
        ttk.Label(fps_row, text="fps", foreground="#888").pack(side="left", padx=(6, 0))

        self.sysaudio_var = tk.BooleanVar(value=True)
        self.sysaudio_chk = ttk.Checkbutton(
            cfg, text="Capture system / desktop audio (what you hear)",
            variable=self.sysaudio_var)
        self.sysaudio_chk.grid(row=2, column=0, columnspan=3, sticky="w", padx=10, pady=(8, 10))

        # ---- Analysis (Gemini) -------------------------------------------
        try:
            import gemini as _gem
            model_ids = list(getattr(_gem, "KNOWN_MODELS", []))
            default_model = _gem.MODEL
        except Exception:
            model_ids = ["gemini-2.5-flash", "gemini-2.5-flash-lite",
                         "gemini-2.0-flash", "gemini-2.0-flash-lite"]
            default_model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        saved = self._settings.get("gemini_model")
        if saved:
            default_model = saved
        # Keep the current + saved-fallback ids in the list so their display
        # labels resolve even before the first live refresh.
        saved_fb = (self._settings.get("gemini_fallback_model") or "").strip()
        for _mid in (default_model, saved_fb):
            if _mid and _mid not in model_ids:
                model_ids.append(_mid)
        model_labels = self._build_model_options(model_ids)   # sets self._model_map

        ana = ttk.LabelFrame(root, text="Analysis")
        ana.pack(fill="x", padx=14, pady=(0, 8))
        ana.columnconfigure(1, weight=1)

        self.capture_note = ttk.Label(
            ana, foreground="#888",
            text="Recording only captures — nothing is uploaded until you click "
                 "Process recordings.")
        self.capture_note.grid(row=0, column=0, columnspan=3, sticky="w",
                               padx=10, pady=(8, 4))

        ttk.Label(ana, text="Gemini model:").grid(row=1, column=0, sticky="w",
                                                  padx=(10, 4), pady=(0, 10))
        self.model_var = tk.StringVar(value=self._model_label_for(default_model))
        self.model_combo = ttk.Combobox(ana, textvariable=self.model_var,
                                        values=model_labels)
        self.model_combo.grid(row=1, column=1, sticky="we", padx=(4, 4), pady=(0, 10))
        self.model_combo.bind("<<ComboboxSelected>>", self._apply_model)
        self.model_combo.bind("<Return>", self._apply_model)
        self.model_combo.bind("<FocusOut>", self._apply_model)
        self.model_refresh_btn = ttk.Button(ana, text="↻", width=3,
                                            command=self._refresh_models)
        self.model_refresh_btn.grid(row=1, column=2, sticky="w", padx=(0, 10), pady=(0, 10))
        self._applied_model = default_model

        # ---- Fallback model (tried when the main model is busy / quota'd) -
        ttk.Label(ana, text="Fallback model:").grid(row=2, column=0, sticky="w",
                                                    padx=(10, 4), pady=(0, 10))
        self.model_fallback_var = tk.StringVar(value=self._fallback_model_label())
        self.model_fallback_combo = ttk.Combobox(
            ana, textvariable=self.model_fallback_var,
            values=self._fallback_model_values(model_ids))
        self.model_fallback_combo.grid(row=2, column=1, columnspan=2, sticky="we",
                                       padx=(4, 10), pady=(0, 10))
        self.model_fallback_combo.bind("<<ComboboxSelected>>", self._apply_fallback_model)
        self.model_fallback_combo.bind("<Return>", self._apply_fallback_model)
        self.model_fallback_combo.bind("<FocusOut>", self._apply_fallback_model)
        self._applied_fallback_model = self.model_fallback_var.get()

        _is_gem = os.environ.get("ANALYSIS_BACKEND", "claude").strip().lower() == "gemini"
        if not _is_gem:
            self.model_combo.config(state="disabled")
            self.model_refresh_btn.config(state="disabled")
            self.model_fallback_combo.config(state="disabled")
        else:
            if saved:
                # An explicit prior in-app choice wins over the import-time default.
                self._apply_model(announce=False, force=True)
            self._apply_fallback_model(announce=False, force=True)

        # ---- Embedding (semantic-search) backend -------------------------
        # Independent of the analysis backend: you can analyze with Claude or
        # Gemini yet still choose how the knowledge base is embedded for search —
        # local (offline, free) or Gemini (cloud, higher quality, uses quota).
        try:
            import embed as _emb
            _eb, _em, _ = _emb.current()
        except Exception:
            _eb, _em = "local", "BAAI/bge-small-en-v1.5"
        embed_values = self._build_embed_options(cur_backend=_eb, cur_model=_em)
        default_embed = self._embed_label_for(_eb, _em)

        ttk.Label(ana, text="Embedding:").grid(row=3, column=0, sticky="w",
                                               padx=(10, 4), pady=(0, 10))
        self.embed_var = tk.StringVar(value=default_embed)
        self.embed_combo = ttk.Combobox(ana, textvariable=self.embed_var,
                                        values=embed_values, state="readonly")
        self.embed_combo.grid(row=3, column=1, sticky="we", padx=(4, 4), pady=(0, 10))
        self.embed_combo.bind("<<ComboboxSelected>>", self._apply_embed)
        self.embed_refresh_btn = ttk.Button(ana, text="↻", width=3,
                                            command=self._refresh_embed_models)
        self.embed_refresh_btn.grid(row=3, column=2, sticky="w", padx=(0, 10), pady=(0, 10))
        self._applied_embed = default_embed

        # ---- Embedding fallback (used only with the Gemini embedding backend)
        ttk.Label(ana, text="Embedding fallback:").grid(row=4, column=0, sticky="w",
                                                        padx=(10, 4), pady=(0, 10))
        self.embed_fallback_var = tk.StringVar(value=self._embed_fallback_label())
        self.embed_fallback_combo = ttk.Combobox(
            ana, textvariable=self.embed_fallback_var,
            values=self._embed_fallback_values())
        self.embed_fallback_combo.grid(row=4, column=1, columnspan=2, sticky="we",
                                       padx=(4, 10), pady=(0, 10))
        self.embed_fallback_combo.bind("<<ComboboxSelected>>", self._apply_embed_fallback)
        self.embed_fallback_combo.bind("<Return>", self._apply_embed_fallback)
        self.embed_fallback_combo.bind("<FocusOut>", self._apply_embed_fallback)
        self._applied_embed_fallback = self.embed_fallback_var.get()
        self._apply_embed_fallback(announce=False, force=True)

        # ---- Proxy (reach Google through a local VPN/proxy) ---------------
        ttk.Label(ana, text="Proxy:").grid(row=5, column=0, sticky="w",
                                           padx=(10, 4), pady=(0, 10))
        self.proxy_var = tk.StringVar(value=str(self._settings.get("proxy", "") or ""))
        self.proxy_entry = ttk.Entry(ana, textvariable=self.proxy_var)
        self.proxy_entry.grid(row=5, column=1, columnspan=2, sticky="we",
                              padx=(4, 10), pady=(0, 10))
        self.proxy_entry.bind("<Return>", self._apply_proxy)
        self.proxy_entry.bind("<FocusOut>", self._apply_proxy)
        self._applied_proxy = self.proxy_var.get().strip()

        # ---- Action buttons ----------------------------------------------
        btns = ttk.Frame(root)
        btns.pack(fill="x", padx=14, pady=(0, 8))
        self.rec_btn = ttk.Button(btns, text="●  Start Recording", command=self.toggle_record)
        self.rec_btn.pack(side="left")
        self.process_btn = ttk.Button(btns, text="Process recordings",
                                      command=self.process_recordings, state="disabled")
        self.process_btn.pack(side="left", padx=8)
        self.dash_btn = ttk.Button(btns, text="Dashboard", command=self._open_dashboard)
        self.dash_btn.pack(side="left")
        self.open_btn = ttk.Button(btns, text="Open folder",
                                   command=lambda: os.startfile(REC_DIR))
        self.open_btn.pack(side="left", padx=8)

        # ---- Prominent status line ---------------------------------------
        self.status = tk.StringVar(value="Ready.")
        self.status_lbl = ttk.Label(root, textvariable=self.status,
                                    font=("Segoe UI", 11, "bold"), foreground=_OK)
        self.status_lbl.pack(anchor="w", padx=14, pady=(0, 2))

        # ---- Activity log -------------------------------------------------
        ttk.Label(root, text="Activity log", foreground="#888").pack(
            anchor="w", padx=14, pady=(6, 0))
        self.log = scrolledtext.ScrolledText(root, height=16, wrap="word",
                                             font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, padx=14, pady=(2, 12))

        # Hover help.
        _ToolTip(self.audio_combo, "Microphone to record with the screen. "
                 "Pick '(no microphone)' to skip mic audio.")
        _ToolTip(self.mic_refresh_btn, "Re-scan for microphones (e.g. after plugging one in).")
        _ToolTip(self.fps_spin, "Frames per second to capture. 15 is smooth for screen "
                 "work and keeps files small.")
        _ToolTip(self.sysaudio_chk, "Also record system/desktop audio (loopback) — useful "
                 "for calls, videos and anything you hear.")
        _ToolTip(self.capture_note, "Recording writes crash-safe ~10-min segments to disk and "
                 "uploads nothing. When you're ready (e.g. the VPN is up), click 'Process "
                 "recordings' to analyze everything that's accumulated. Safe to do later.")
        _ToolTip(self.model_combo, "Which Gemini model analyzes your recording. If one model's "
                 "free daily quota runs out, switch to another here — it applies right away "
                 "(even mid-recording) and is remembered. You can also type any model name.")
        _ToolTip(self.model_refresh_btn, "Refresh this list from Google — pulls every model your "
                 "key can use right now (including new ones). Needs an internet connection.")
        _ToolTip(self.model_fallback_combo, "Model to switch to when the main model is overloaded "
                 "(503) or its free daily quota is used up (429). '(automatic)' uses the "
                 "built-in cheap-first chain. Applies right away and is remembered.")
        _ToolTip(self.embed_combo, "How your knowledge base is embedded for semantic search. "
                 "'Local' runs on your machine (offline, free, private). 'Gemini' is higher "
                 "quality but sends your KB text and search queries to Google (uses free-tier "
                 "quota). Switching re-embeds existing days automatically.")
        _ToolTip(self.embed_refresh_btn, "Refresh the Gemini embedding-model list from Google. "
                 "Needs a GEMINI_API_KEY and an internet connection.")
        _ToolTip(self.embed_fallback_combo, "Gemini embedding model to fall back to if the main "
                 "one fails (used only with the Gemini embedding backend). '(automatic)' = none.")
        _ToolTip(self.proxy_entry, "Optional. If this network can't reach Google directly "
                 "(uploads fail with 'connection timed out' / 'forbidden'), enter a local "
                 "proxy or VPN address such as http://127.0.0.1:7890. It's used for all "
                 "Google calls (analysis, reduce, embeddings), applies right away, and is "
                 "remembered. A RECORDER_PROXY or HTTPS_PROXY environment variable, if set, "
                 "overrides this box.")
        _ToolTip(self.process_btn, "Upload + analyze every recording not yet processed (across "
                 "days). Resumable: each segment is saved as it finishes, so a crash/shutdown "
                 "picks up where it left off, and failed uploads simply retry next time.")
        _ToolTip(self.dash_btn, "Open your cross-day knowledge base: summaries, to-dos and search.")
        _ToolTip(self.open_btn, "Open the folder where recordings and reports are saved.")

        self.refresh_devices()
        if not self.ffmpeg:
            self.write("ffmpeg not found. Install it (winget install Gyan.FFmpeg) and reopen the app.")
            self.rec_btn.config(state="disabled")
        else:
            self.write(f"Using ffmpeg: {self.ffmpeg}")
        self._log_backend_status()
        self._log_embed_status()
        self._update_backend_badge()
        # Quietly pull the live model list so the dropdown reflects what the key
        # can actually use (incl. newly released models), without blocking launch.
        if os.environ.get("ANALYSIS_BACKEND", "claude").strip().lower() == "gemini":
            self._refresh_models(announce=False, initial=True)
        # Embedding is independent of the analysis backend, so refresh its model
        # list whenever a Gemini key is present (even on the Claude backend).
        try:
            import gemini as _g
            if _g.available():
                self._refresh_embed_models(announce=False, initial=True)
        except Exception:
            pass

        # Recover any session interrupted by a crash/shutdown, then show how many
        # recordings are waiting to be processed on the Process button.
        if self.ffmpeg:
            self._recover_unfinished()
        self._update_process_button()

        # Keep the model + embedding dropdowns fresh over the session.
        if MODEL_REFRESH_MIN > 0:
            self.root.after(MODEL_REFRESH_MIN * 60 * 1000, self._auto_refresh_models)

    def _log_backend_status(self):
        """Print one clear backend line at startup so a misconfigured analysis
        backend is never silent. Distinguishes Gemini ready / no-key / no-SDK,
        local-only, and Claude with/without key. Env is already refreshed from the
        registry at import time, so this reflects the user's persisted setx values
        even if the launching shell was stale."""
        backend = os.environ.get("ANALYSIS_BACKEND", "claude").strip().lower()
        has_gem_key = bool(os.environ.get("GEMINI_API_KEY")
                           or os.environ.get("GOOGLE_API_KEY"))
        try:
            import google.genai  # noqa: F401
            gem_sdk = True
        except Exception:
            gem_sdk = False

        if backend == "gemini":
            if has_gem_key and gem_sdk:
                try:
                    import gemini
                    model = gemini.MODEL
                except Exception:
                    model = "gemini"
                self.write(f"Backend: Gemini ({model}) — free native video analysis. "
                           "Recording captures only; click 'Process recordings' to analyze.")
            elif not has_gem_key:
                self.write("Backend: Gemini selected, but no GEMINI_API_KEY found — "
                           "analysis will fall back to a basic local summary. Set the "
                           "key (setx GEMINI_API_KEY <key>) and reopen the app.")
            else:  # key present, SDK missing
                self.write("Backend: Gemini selected, but the google-genai SDK isn't "
                           "installed — run  pip install google-genai  and reopen. "
                           "Until then analysis falls back to a basic local summary.")
        elif backend == "local":
            self.write("Backend: local only — basic on-device summary, no AI. For free "
                       "AI analysis run 'Use Gemini (free).bat' and reopen the app.")
        else:  # claude (default)
            if os.environ.get("ANTHROPIC_API_KEY"):
                self.write("Backend: Claude — AI analysis enabled (uses your Anthropic key).")
            else:
                self.write("Backend: Claude (default), but no ANTHROPIC_API_KEY — analysis "
                           "will be a basic local summary. Tip: run 'Use Gemini (free).bat' "
                           "for free AI analysis, then reopen the app.")

    def _backend_info(self):
        """(label, color) for the header badge describing the active backend."""
        backend = os.environ.get("ANALYSIS_BACKEND", "claude").strip().lower()
        has_gem_key = bool(os.environ.get("GEMINI_API_KEY")
                           or os.environ.get("GOOGLE_API_KEY"))
        try:
            import google.genai  # noqa: F401
            gem_sdk = True
        except Exception:
            gem_sdk = False
        if backend == "gemini":
            if has_gem_key and gem_sdk:
                try:
                    import gemini
                    short = gemini.MODEL.replace("gemini-", "") or "free AI"
                except Exception:
                    short = "free AI"
                return (f"Gemini · {short}", _OK)
            return ("Gemini not ready — see log", _BUSY)
        if backend == "local":
            return ("Local only · no AI", _WARN)
        if os.environ.get("ANTHROPIC_API_KEY"):
            return ("Claude · AI", _OK)
        return ("Claude · no key", _WARN)

    def _update_backend_badge(self):
        try:
            label, color = self._backend_info()
            self.backend_badge.config(text=label, bg=color)
        except Exception:
            pass

    def _set_settings_enabled(self, enabled):
        """Lock the capture-settings widgets while recording (they can't change
        mid-capture) and re-enable them when idle."""
        combo = "readonly" if enabled else "disabled"
        st = "normal" if enabled else "disabled"
        for w, s in ((self.audio_combo, combo), (self.mic_refresh_btn, st),
                     (self.fps_spin, st), (self.sysaudio_chk, st),
                     (self.embed_combo, combo), (self.embed_refresh_btn, st),
                     (self.embed_fallback_combo, st), (self.proxy_entry, st)):
            try:
                w.config(state=s)
            except Exception:
                pass

    def _open_dashboard(self):
        path = os.path.join(REC_DIR, "dashboard.html")
        if os.path.isfile(path):
            os.startfile(path)
        else:
            messagebox.showinfo(
                "No dashboard yet",
                "Analyze a recording first — the dashboard is built from your "
                "analyzed sessions.")

    # ---- Model picker labels (dashboard display names <-> model ids) ------
    def _build_model_options(self, ids):
        """Build dropdown labels (exact dashboard display names; Gemma marked
        '(no video)') and the {label: id} map. Returns the list of labels."""
        try:
            import gemini
            label_fn = gemini.model_label
        except Exception:
            label_fn = lambda x: x
        mapping, labels = {}, []
        for mid in (ids or []):
            if not mid:
                continue
            lbl = label_fn(mid)
            if lbl in mapping and mapping[lbl] != mid:
                lbl = f"{lbl} [{mid}]"
            mapping[lbl] = mid
            labels.append(lbl)
        self._model_map = mapping
        return labels

    def _model_label_for(self, model_id):
        """Reverse-lookup the display label for a model id (raw id if unknown)."""
        for lbl, mid in (getattr(self, "_model_map", {}) or {}).items():
            if mid == model_id:
                return lbl
        try:
            import gemini
            return gemini.model_label(model_id)
        except Exception:
            return model_id

    def _apply_model(self, *_args, announce=True, force=False):
        """Push the chosen Gemini model into the backend so the next analysis uses
        it. The dropdown shows dashboard display names, so map the label back to
        the model id first. No restart needed; remembered across restarts."""
        label = (self.model_var.get() or "").strip()
        if not label:
            return
        model = (getattr(self, "_model_map", {}) or {}).get(label, label).strip()
        if not model:
            return
        if not force and model == self._applied_model:
            return
        try:
            import gemini
            gemini.set_model(model)
        except Exception as e:
            if announce:
                self.write(f"Couldn't switch Gemini model: {e}")
            return
        self._applied_model = model
        self._settings["gemini_model"] = model
        _save_settings(self._settings)
        if announce:
            self.write(f"Gemini analysis model → {gemini.display_name(model)}.")
        self._update_backend_badge()

    # ---- Fallback model (failover target) ---------------------------------
    def _fallback_model_label(self):
        v = (self._settings.get("gemini_fallback_model") or "").strip()
        if not v:
            return self._AUTO_FALLBACK
        return self._model_label_for(v)

    def _fallback_model_values(self, ids):
        """'(automatic)' + display-name labels for each id; registers the labels
        in the shared _model_map so they resolve back to ids on apply."""
        try:
            import gemini
            label_fn = gemini.model_label
        except Exception:
            label_fn = lambda x: x
        if not hasattr(self, "_model_map"):
            self._model_map = {}
        labels = [self._AUTO_FALLBACK]
        for mid in (ids or []):
            if not mid:
                continue
            lbl = label_fn(mid)
            self._model_map.setdefault(lbl, mid)
            if lbl not in labels:
                labels.append(lbl)
        return labels

    def _apply_fallback_model(self, *_args, announce=True, force=False):
        """Push the chosen fallback model into the backend. '(automatic)' clears
        it (use the built-in failover chain). Remembered across restarts."""
        label = (self.model_fallback_var.get() or "").strip()
        if not force and label == getattr(self, "_applied_fallback_model", None):
            return
        if not label or label == self._AUTO_FALLBACK:
            model = ""
        else:
            model = (getattr(self, "_model_map", {}) or {}).get(label, label)
        try:
            import gemini
            gemini.set_fallback_model(model)
        except Exception as e:
            if announce:
                self.write(f"Couldn't set fallback model: {e}")
            return
        self._applied_fallback_model = label
        if model:
            self._settings["gemini_fallback_model"] = model
        else:
            self._settings.pop("gemini_fallback_model", None)
        _save_settings(self._settings)
        if announce:
            if model:
                self.write(f"Fallback model → {gemini.display_name(model)} (used when "
                           "the main model is overloaded or its daily quota is spent).")
            else:
                self.write("Fallback model → automatic (built-in failover chain).")

    def _refresh_models(self, announce=True, initial=False):
        """Pull the list of models the key can actually use from Google and
        repopulate the dropdown (incl. newly released models). Runs in a
        background thread because it's a network call; falls back silently to
        the built-in list if offline / no key."""
        if os.environ.get("ANALYSIS_BACKEND", "claude").strip().lower() != "gemini":
            if announce:
                self.write("Model list needs the Gemini backend — run "
                           "'Use Gemini (free).bat', then reopen.")
            return
        try:
            self.model_refresh_btn.config(state="disabled")
        except Exception:
            pass
        if announce:
            self.write("Fetching available Gemini models…")

        def work():
            try:
                import gemini
                models = gemini.list_models(force=not initial)
                self.root.after(0, self._models_refreshed, models, None, announce)
            except Exception as e:
                self.root.after(0, self._models_refreshed, None, str(e), announce)

        threading.Thread(target=work, daemon=True).start()

    def _models_refreshed(self, models, err, announce):
        """Back on the GUI thread: apply the fetched model list to the combo."""
        try:
            self.model_refresh_btn.config(state="normal")
        except Exception:
            pass
        if err or not models:
            if announce:
                self.write(f"Couldn't fetch model list ({err or 'none returned'}) "
                           "— keeping the built-in list.")
            return
        ids = list(models)
        if self._applied_model and self._applied_model not in ids:
            ids.insert(0, self._applied_model)   # keep the current choice visible
        self.model_combo["values"] = self._build_model_options(ids)
        self.model_var.set(self._model_label_for(self._applied_model))
        try:    # keep the fallback dropdown in sync + show exact display names
            self.model_fallback_combo["values"] = self._fallback_model_values(ids)
            self.model_fallback_var.set(self._fallback_model_label())
            self._applied_fallback_model = self.model_fallback_var.get()
        except Exception:
            pass
        if announce:
            self.write(f"Found {len(models)} model(s) available to your key.")

    # Dropdown sentinel meaning "no explicit fallback — use the built-in chain".
    _AUTO_FALLBACK = "(automatic)"

    # ---- Embedding (semantic-search) backend ------------------------------
    _LOCAL_EMBED_LABEL = "Local · bge-small-en-v1.5 (offline)"
    _LOCAL_EMBED_MODEL = "BAAI/bge-small-en-v1.5"

    def _build_embed_options(self, gmodels=None, cur_backend=None, cur_model=None):
        """Build the dropdown choices and a {label: (backend, model)} map. The
        offline local model first, then each known/discovered Gemini embedding
        model. A current custom Gemini model is kept visible."""
        mapping = {self._LOCAL_EMBED_LABEL: ("local", self._LOCAL_EMBED_MODEL)}
        opts = [self._LOCAL_EMBED_LABEL]
        if gmodels is None:
            try:
                import gemini
                gmodels = list(getattr(gemini, "KNOWN_EMBED_MODELS", []))
            except Exception:
                gmodels = ["gemini-embedding-001"]
        gmodels = list(gmodels)
        if cur_backend == "gemini" and cur_model and cur_model not in gmodels:
            gmodels = [cur_model] + gmodels
        try:
            import gemini
            disp = gemini.display_name
        except Exception:
            disp = lambda x: x
        for m in gmodels:
            lbl = f"{disp(m)}  (cloud)"     # exact dashboard name, e.g. "Gemini Embedding 2"
            mapping[lbl] = ("gemini", m)
            opts.append(lbl)
        self._embed_map = mapping
        return opts

    def _embed_label_for(self, backend, model):
        """Reverse-lookup the dropdown label for a (backend, model) pair."""
        for lbl, (b, m) in (getattr(self, "_embed_map", {}) or {}).items():
            if b == backend and m == model:
                return lbl
        return self._LOCAL_EMBED_LABEL

    def _log_embed_status(self):
        """One startup line so the active embedding backend (and whether it sends
        data to the cloud) is never a surprise."""
        try:
            import embed
            local = embed.current()[0] == "local"
            where = ("on-device — nothing leaves your machine" if local else
                     "CLOUD — your KB text + search queries are sent to Google")
            self.write(f"Search embeddings: {embed.label()} ({where}).")
        except Exception:
            pass

    def _apply_embed(self, *_args, announce=True, force=False):
        """Switch the embedding backend/model used for indexing and search, save
        it, and re-embed the existing knowledge base so past days stay
        searchable under the new model."""
        label = (self.embed_var.get() or "").strip()
        if not label:
            return
        if not force and label == getattr(self, "_applied_embed", None):
            return
        backend, model = (getattr(self, "_embed_map", {}) or {}).get(
            label, ("local", self._LOCAL_EMBED_MODEL))
        try:
            import embed
            embed.set_backend(backend, model)
        except Exception as e:
            if announce:
                self.write(f"Couldn't switch embedding backend: {e}")
            return
        self._applied_embed = label
        self._settings["embed_backend"] = backend
        if backend == "gemini":
            self._settings["embed_gemini_model"] = model
        else:
            self._settings["embed_local_model"] = model
        _save_settings(self._settings)
        self._update_backend_badge()
        if announce:
            if backend == "gemini":
                self.write(f"Embedding → {label}.  ⚠ Cloud: your KB text (summaries, "
                           "to-dos, topics) and every search query will be sent to "
                           "Google to embed.")
            else:
                self.write(f"Embedding → {label}.  Runs on-device; nothing leaves "
                           "your machine.")
            self._reindex_async()

    def _embed_fallback_label(self):
        v = (self._settings.get("embed_gemini_fallback_model") or "").strip()
        if not v:
            return self._AUTO_FALLBACK
        try:
            import gemini
            return gemini.display_name(v)
        except Exception:
            return v

    def _embed_fallback_values(self, gmodels=None):
        """'(automatic)' + display-name labels for each Gemini embedding model;
        sets self._embed_fallback_map {label: id} for apply to resolve."""
        if gmodels is None:
            try:
                import gemini
                gmodels = list(getattr(gemini, "KNOWN_EMBED_MODELS", []))
            except Exception:
                gmodels = ["gemini-embedding-001"]
        try:
            import gemini
            disp = gemini.display_name
        except Exception:
            disp = lambda x: x
        ids = list(gmodels)
        saved = (self._settings.get("embed_gemini_fallback_model") or "").strip()
        if saved and saved not in ids:
            ids.insert(0, saved)
        mapping, labels = {}, [self._AUTO_FALLBACK]
        for m in ids:
            if not m:
                continue
            lbl = disp(m)
            mapping[lbl] = m
            if lbl not in labels:
                labels.append(lbl)
        self._embed_fallback_map = mapping
        return labels

    def _apply_embed_fallback(self, *_args, announce=True, force=False):
        """Set the Gemini *embedding* fallback model. '(automatic)' = none. Only
        used when the embedding backend is Gemini. Remembered across restarts."""
        label = (self.embed_fallback_var.get() or "").strip()
        if not force and label == getattr(self, "_applied_embed_fallback", None):
            return
        if not label or label == self._AUTO_FALLBACK:
            model = ""
        else:
            model = (getattr(self, "_embed_fallback_map", {}) or {}).get(label, label)
        try:
            import embed
            embed.set_gemini_fallback(model)
        except Exception as e:
            if announce:
                self.write(f"Couldn't set embedding fallback: {e}")
            return
        self._applied_embed_fallback = label
        if model:
            self._settings["embed_gemini_fallback_model"] = model
        else:
            self._settings.pop("embed_gemini_fallback_model", None)
        _save_settings(self._settings)
        if announce:
            if model:
                try:
                    import gemini
                    disp = gemini.display_name(model)
                except Exception:
                    disp = model
                self.write(f"Embedding fallback → {disp} (used if the main Gemini "
                           "embedding model fails).")
            else:
                self.write("Embedding fallback → none.")

    def _apply_proxy(self, event=None):
        """Persist the proxy address and rebuild the Gemini client so it takes
        effect immediately (no restart). Used for every Google call — analysis,
        reduce, and embeddings. Locked while recording (see
        _set_settings_enabled), so this can't race the live worker."""
        val = (self.proxy_var.get() or "").strip()
        if val == getattr(self, "_applied_proxy", None):
            return
        self._applied_proxy = val
        if val:
            self._settings["proxy"] = val
        else:
            self._settings.pop("proxy", None)
        _save_settings(self._settings)
        try:
            import gemini
            gemini.reset_client()
        except Exception:
            pass
        if val:
            self.write(f"Proxy set to {val} — all Google calls now route through it.")
        else:
            self.write("Proxy cleared — connecting to Google directly.")

    def _reindex_async(self):
        """Re-embed every analyzed session under the current model, off-thread.
        Needed after a backend switch because search only compares vectors of the
        matching dimension."""
        def log(m):
            self.root.after(0, self.write, m)

        def work():
            try:
                import embed
                import insights
                if not embed.available():
                    log("Re-embed skipped — the chosen embedding backend isn't "
                        "ready (for Gemini, set GEMINI_API_KEY and reopen).")
                    return
                log("Re-embedding the knowledge base with the new model… "
                    "(one-time, so existing days are searchable again).")
                n = insights.reindex(REC_DIR, force=True, log=log)
                if n == 0:
                    log("Nothing to re-embed yet (no analyzed sessions).")
            except Exception as e:
                log(f"Re-embed failed: {e}")

        threading.Thread(target=work, daemon=True).start()

    def _refresh_embed_models(self, announce=True, initial=False):
        """Pull embedding-capable models the key can use from Google and refresh
        the dropdown. Background thread; falls back to the built-in list."""
        try:
            import gemini
            if not gemini.available():
                if announce:
                    self.write("Embedding-model list needs a Gemini key — set "
                               "GEMINI_API_KEY (setx) and reopen.")
                return
        except Exception:
            if announce:
                self.write("google-genai not installed — can't refresh embedding models.")
            return
        try:
            self.embed_refresh_btn.config(state="disabled")
        except Exception:
            pass
        if announce:
            self.write("Fetching available Gemini embedding models…")

        def work():
            try:
                import gemini
                models = gemini.list_embed_models(force=not initial)
                self.root.after(0, self._embed_models_refreshed, models, None, announce)
            except Exception as e:
                self.root.after(0, self._embed_models_refreshed, None, str(e), announce)

        threading.Thread(target=work, daemon=True).start()

    def _embed_models_refreshed(self, models, err, announce):
        """Back on the GUI thread: apply the fetched embedding-model list."""
        try:
            self.embed_refresh_btn.config(state="normal")
        except Exception:
            pass
        if err or not models:
            if announce:
                self.write(f"Couldn't fetch embedding models "
                           f"({err or 'none returned'}) — keeping the built-in list.")
            return
        cur_label = (self.embed_var.get() or "").strip()
        cur_backend, cur_model = (getattr(self, "_embed_map", {}) or {}).get(
            cur_label, ("local", None))
        opts = self._build_embed_options(gmodels=models, cur_backend=cur_backend,
                                         cur_model=cur_model)
        self.embed_combo["values"] = opts
        try:    # keep the embedding-fallback dropdown in sync
            self.embed_fallback_combo["values"] = self._embed_fallback_values(models)
        except Exception:
            pass
        if announce:
            self.write(f"Found {len(models)} embedding model(s) available to your key.")

    def _auto_refresh_models(self):
        """Periodic quiet refresh so the model + embedding dropdowns reflect what
        the key can currently use. Skips while recording/processing, then reschedules."""
        try:
            if self.proc is None and not self.processing:
                if os.environ.get("ANALYSIS_BACKEND", "claude").strip().lower() == "gemini":
                    self._refresh_models(announce=False)
                try:
                    import gemini as _g
                    if _g.available():
                        self._refresh_embed_models(announce=False)
                except Exception:
                    pass
        finally:
            self.root.after(MODEL_REFRESH_MIN * 60 * 1000, self._auto_refresh_models)

    # ---- UI helpers -------------------------------------------------------
    def write(self, msg):
        ts = dt.datetime.now().strftime("%H:%M:%S")
        self.log.insert("end", f"[{ts}] {msg}\n")
        self.log.see("end")
        self.root.update_idletasks()

    def refresh_devices(self):
        devices = list_audio_devices(self.ffmpeg)
        values = ["(no microphone)"] + devices
        self.audio_combo["values"] = values
        # Prefer a real microphone if present.
        default = next((d for d in devices if "micro" in d.lower() or "mic" in d.lower()), None)
        self.audio_var.set(default or values[0])

    # ---- Recording --------------------------------------------------------
    def toggle_record(self):
        if self.proc is None:
            self.start_record()
        else:
            self.stop_record()

    def start_record(self):
        if not self.ffmpeg:
            return
        if self.processing:
            messagebox.showinfo("Busy", "Please wait for processing to finish "
                                "before starting a new recording.")
            return
        try:
            fps = int(self.fps_var.get())
        except ValueError:
            fps = 15
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_dir = os.path.join(REC_DIR, f"session_{ts}")
        os.makedirs(self.session_dir, exist_ok=True)
        self.session_started_iso = dt.datetime.now().isoformat(timespec="seconds")
        self.session_ended_iso = None

        audio = self.audio_var.get()
        self.has_mic = bool(audio and audio != "(no microphone)")

        cmd = [
            self.ffmpeg, "-hide_banner", "-y",
            "-f", "gdigrab", "-framerate", str(fps), "-i", "desktop",
        ]
        if self.has_mic:
            cmd += ["-f", "dshow", "-i", f"audio={audio}"]
        cmd += [
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            # Keyframe at least every ~10s so segments cut cleanly on a keyframe.
            "-g", str(max(2, fps) * 10),
        ]
        if self.has_mic:
            cmd += ["-c:a", "aac", "-b:a", "128k"]
        # Always capture into short *finalized* chunks (_vraw_000.mp4, …); the
        # muxer folds each into a complete, crash-safe seg_NNN.mp4. No upload
        # happens here — the user runs Process later.
        cmd += ["-f", "segment", "-segment_time", str(CAPTURE_SEGMENT_SECONDS),
                "-reset_timestamps", "1", "-segment_format", "mp4",
                os.path.join(self.session_dir, "_vraw_%03d.mp4")]

        try:
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception as e:
            messagebox.showerror("Recording failed", str(e))
            self.proc = None
            return

        self.sys_rec = None
        if self.sysaudio_var.get():
            try:
                self.sys_rec = audio_capture.SystemAudioRecorder(
                    os.path.join(self.session_dir, "_sraw_000.wav"))
                self.sys_rec.start()
                self.write("Capturing system audio (loopback).")
            except Exception as e:
                self.sys_rec = None
                self.write(f"System audio unavailable: {e}")

        # state.json — the source of truth for "this session needs processing".
        process.write_state(self.session_dir,
                            started=self.session_started_iso, ended=None,
                            recording=True, processed=False,
                            has_mic=self.has_mic,
                            sys_audio=bool(self.sys_rec is not None))

        self.start_time = time.time()
        self.rec_btn.config(text="■  Stop Recording")
        self.process_btn.config(state="disabled")
        self._set_settings_enabled(False)
        self.status_lbl.config(foreground=_BUSY)

        # Background muxer folds finalized chunks into seg_NNN.mp4 (no upload).
        self.mux_stop_event = threading.Event()
        self.mux_thread = threading.Thread(
            target=self._mux_loop,
            args=(self.session_dir, self.ffmpeg, self.has_mic,
                  self.sys_rec, self.mux_stop_event),
            daemon=True)
        self.mux_thread.start()
        self.write(f"Recording started → {os.path.basename(self.session_dir)}\\ "
                   f"(crash-safe {CAPTURE_SEGMENT_SECONDS // 60}-min segments; "
                   "click Process recordings later to analyze)")
        self._tick()

    def _tick(self):
        if self.proc is None:
            return
        elapsed = int(time.time() - self.start_time)
        self.status.set(f"● Recording   {elapsed // 60:02d}:{elapsed % 60:02d}")
        self.timer_job = self.root.after(500, self._tick)

    def stop_record(self):
        if self.proc is None:
            return
        self.session_ended_iso = dt.datetime.now().isoformat(timespec="seconds")
        self.write("Stopping… (finalizing video)")
        try:
            # 'q' tells ffmpeg to stop cleanly and write a valid file.
            self.proc.stdin.write(b"q")
            self.proc.stdin.flush()
        except Exception:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.terminate()
        self.proc = None
        if self.timer_job:
            self.root.after_cancel(self.timer_job)
        self.rec_btn.config(text="●  Start Recording")
        self.rec_btn.config(state="disabled")
        self.status_lbl.config(foreground=_INFO)
        self.status.set("Finalizing…")
        # The last _vraw chunk is now finalized; drain the muxer on a background
        # thread (it owns sys_rec and stops the loopback itself) so the GUI stays
        # responsive. No upload happens — that's the Process button's job.
        threading.Thread(target=self._drain_mux_worker, daemon=True).start()

    def _drain_mux_worker(self):
        """Wait for the muxer to fold in the final chunk(s), mark the session
        ready to process, and refresh the Process button. Off the GUI thread."""
        log = lambda m: self.root.after(0, self.write, m)
        session_dir = self.session_dir
        try:
            if self.mux_stop_event:
                self.mux_stop_event.set()
            if self.mux_thread:
                self.mux_thread.join()
            n = len(process.segments(session_dir)) if session_dir else 0
            if session_dir:
                process.write_state(session_dir, recording=False,
                                    ended=self.session_ended_iso)
            base = os.path.basename(session_dir) if session_dir else "?"
            log(f"Saved {n} segment(s) in {base}\\. Click 'Process recordings' "
                "to analyze (e.g. once your VPN is up).")
        except Exception as e:
            log(f"Finalize error: {e}")
        finally:
            self.root.after(0, lambda: self.status_lbl.config(foreground=_OK))
            self.root.after(0, lambda: self.status.set("Ready."))
            self.root.after(0, lambda: self.rec_btn.config(state="normal"))
            self.root.after(0, lambda: self._set_settings_enabled(True))
            self.root.after(0, self._update_process_button)

    def _audio_args(self, sys_wav, has_mic, sys_ok):
        """ffmpeg (extra_inputs, map/filter, codec) args for combining audio.
        Shared by the at-stop segmenter and the live per-chunk muxer. Video is
        always copied (no re-encode); audio is re-encoded to AAC only when
        system audio is mixed in."""
        if sys_ok and has_mic:
            fc = ("[0:a]aresample=48000[a0];[1:a]aresample=48000[a1];"
                  "[a0][a1]amix=inputs=2:duration=longest:normalize=0[a]")
            return (["-i", sys_wav],
                    ["-filter_complex", fc, "-map", "0:v", "-map", "[a]"],
                    ["-c:v", "copy", "-c:a", "aac", "-b:a", "160k"])
        if sys_ok:
            return (["-i", sys_wav],
                    ["-map", "0:v", "-map", "1:a"],
                    ["-c:v", "copy", "-c:a", "aac", "-b:a", "160k"])
        # Mic audio (if any) is already in the video file — copy as-is.
        return ([], [], ["-c", "copy"])

    # ---- Capture muxer (fold finalized chunks into seg_NNN.mp4; no upload) --
    def _mux_one(self, video, sys_wav, out, has_mic, sys_ok):
        """Mux ONE finalized video chunk with its system-audio WAV into ``out``.
        Mirrors _segment's audio handling but for a single (non-segmented) file.
        Runs on the worker thread, so it logs via root.after."""
        extra_in, maps, codec = self._audio_args(sys_wav, has_mic, sys_ok)
        cmd = ([self.ffmpeg, "-hide_banner", "-y", "-i", video]
               + extra_in + maps + codec + [out])
        try:
            r = subprocess.run(
                cmd, capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception as e:
            self.root.after(0, self.write, f"Mux error: {e}")
            return False
        return (r.returncode == 0 and os.path.isfile(out)
                and os.path.getsize(out) > 1000)

    def _mux_chunk(self, session_dir, ffmpeg, has_mic, idx, log):
        """Fold one finalized chunk (_vraw_{idx} + _sraw_{idx}) into a complete
        seg_{idx}.mp4. Mux only — nothing is uploaded. Idempotent: skips a chunk
        whose seg file already exists, so recovery/resume is safe."""
        seg = os.path.join(session_dir, f"seg_{idx:03d}.mp4")
        if os.path.isfile(seg) and os.path.getsize(seg) > 1000:
            return
        vraw = os.path.join(session_dir, f"_vraw_{idx:03d}.mp4")
        if not (os.path.isfile(vraw) and os.path.getsize(vraw) > 1000):
            return
        sraw = os.path.join(session_dir, f"_sraw_{idx:03d}.wav")
        sys_ok = os.path.isfile(sraw) and os.path.getsize(sraw) > 1000

        if self._mux_one(vraw, sraw, seg, has_mic, sys_ok):
            for f in (vraw, sraw):
                try:
                    if os.path.isfile(f):
                        os.remove(f)
                except Exception:
                    pass
            log(f"  saved {os.path.basename(seg)}.")
        else:
            # Mux failed — keep the raw video chunk (mic embedded, no sys audio)
            # so the chunk is still analyzable later.
            try:
                os.replace(vraw, seg)
                log(f"  chunk {idx}: mux failed; kept raw video as "
                    f"{os.path.basename(seg)}.")
            except Exception as e:
                log(f"  chunk {idx} unusable ({e}).")
                return
            try:
                if os.path.isfile(sraw):
                    os.remove(sraw)
            except Exception:
                pass

    def _mux_loop(self, session_dir, ffmpeg, has_mic, sys_rec, stop_event):
        """Background worker: while recording, fold each finalized chunk into a
        complete seg_NNN.mp4. NO Gemini calls — uploading happens later via
        Process.

        The segment muxer writes _vraw_000.mp4, _vraw_001.mp4, … ; chunk N is
        finalized once _vraw_{N+1}.mp4 appears (the muxer lags by one). For each
        finalized chunk we rotate the system-audio WAV to match and mux. Never
        raises into the app."""
        log = lambda m: self.root.after(0, self.write, m)
        rotated_to = 0   # index of the currently-open _sraw WAV (start opened _000)
        processed = 0    # next chunk index to mux
        rx = re.compile(r"_vraw_(\d+)\.mp4$")
        try:
            while True:
                idxs = []
                for v in glob.glob(os.path.join(session_dir, "_vraw_*.mp4")):
                    m = rx.search(os.path.basename(v))
                    if m:
                        idxs.append(int(m.group(1)))
                maxidx = max(idxs) if idxs else -1
                draining = stop_event.is_set()

                # Keep the open system-audio WAV's index equal to the chunk that
                # is currently recording, so each finalized WAV matches its video
                # chunk. (rotate() is a no-op once capture has stopped.)
                while sys_rec is not None and rotated_to < maxidx:
                    nxt = os.path.join(session_dir, f"_sraw_{rotated_to + 1:03d}.wav")
                    try:
                        sys_rec.rotate(nxt)
                    except Exception as e:
                        log(f"  system-audio rotate failed ({e}).")
                    rotated_to += 1

                # At stop, finalize the last open WAV so the last chunk has audio.
                if draining and sys_rec is not None:
                    try:
                        sys_rec.stop()
                    except Exception:
                        pass
                    sys_rec = None

                finalized_max = maxidx if draining else maxidx - 1
                while processed <= finalized_max:
                    try:
                        self._mux_chunk(session_dir, ffmpeg, has_mic, processed, log)
                    except Exception as e:
                        log(f"  chunk {processed} mux error ({e}).")
                    processed += 1

                if draining and processed > maxidx:
                    break
                stop_event.wait(MUX_POLL_SECONDS)
        except Exception as e:
            log(f"Muxer stopped on error: {e}")

    # ---- Processing (upload + analyze accumulated recordings) -------------
    def process_recordings(self):
        """Start a resumable batch over every recording that still needs it."""
        if self.proc is not None or self.processing:
            return
        n_sess, _ = process.pending_summary(REC_DIR)
        if n_sess == 0:
            messagebox.showinfo("Nothing to process",
                                "All recordings have already been analyzed.")
            return
        self.processing = True
        self.proc_stop_event = threading.Event()
        self.rec_btn.config(state="disabled")
        self.process_btn.config(text="Stop processing", command=self.stop_processing)
        self._set_settings_enabled(False)
        self.status_lbl.config(foreground=_INFO)
        self.status.set("Processing…")
        threading.Thread(target=self._process_worker, daemon=True).start()

    def stop_processing(self):
        """Ask the Process worker to stop after the current segment. Safe: each
        finished segment is already checkpointed, so it resumes next time."""
        if self.proc_stop_event:
            self.proc_stop_event.set()
        self.write("Stopping after the current segment… progress is saved.")
        self.process_btn.config(state="disabled")

    def _process_worker(self):
        log = lambda m: self.root.after(0, self.write, m)

        def progress(name, i, n):
            self.root.after(0, self.status.set,
                            f"Processing {name} · segment {i + 1}/{n}…")

        last_report = None
        try:
            res = process.process_all_pending(
                REC_DIR, self.ffmpeg, log=log,
                should_stop=lambda: bool(self.proc_stop_event
                                         and self.proc_stop_event.is_set()),
                progress=progress)
            for r in (res.get("results") or []):
                if r.get("report"):
                    last_report = r["report"]
            if last_report and os.path.isfile(last_report):
                log("Opening the latest report…")
                try:
                    os.startfile(last_report)
                except Exception:
                    pass
        except Exception as e:
            log(f"Processing error: {e}")
        finally:
            self.processing = False
            self.root.after(0, lambda: self.status_lbl.config(foreground=_OK))
            self.root.after(0, lambda: self.status.set("Ready."))
            self.root.after(0, lambda: self.rec_btn.config(state="normal"))
            self.root.after(0, lambda: self._set_settings_enabled(True))
            self.root.after(0, self._update_process_button)

    def _update_process_button(self):
        """Reflect how many recordings still need processing in the button label,
        and enable it only when there's work and we're idle."""
        try:
            n_sess, _ = process.pending_summary(REC_DIR)
        except Exception:
            n_sess = 0
        self._pending_sessions = n_sess
        busy = (self.proc is not None) or self.processing
        label = f"Process recordings ({n_sess})" if n_sess else "Process recordings"
        try:
            self.process_btn.config(
                text=label, command=self.process_recordings,
                state=("disabled" if (busy or not n_sess) else "normal"))
        except Exception:
            pass

    def _recover_unfinished(self):
        """On launch, tidy any session left mid-recording by a crash/shutdown:
        mux orphan _vraw/_sraw chunks into seg files and clear the stale
        'recording' flag so the session shows up as pending (not stuck)."""
        try:
            rx = re.compile(r"_vraw_(\d+)\.mp4$")
            for sd in process.session_dirs(REC_DIR):
                st = process.read_state(sd)
                orphans = sorted(glob.glob(os.path.join(sd, "_vraw_*.mp4")))
                if not orphans and not st.get("recording"):
                    continue
                if orphans:
                    self.write(f"Recovering {os.path.basename(sd)} "
                               f"({len(orphans)} unfinished chunk(s))…")
                    for v in orphans:
                        m = rx.search(os.path.basename(v))
                        if m:
                            self._mux_chunk(sd, self.ffmpeg,
                                            bool(st.get("has_mic")),
                                            int(m.group(1)), self.write)
                if st.get("recording"):
                    process.write_state(sd, recording=False)
        except Exception as e:
            self.write(f"Recovery skipped ({e}).")

    def _on_close(self):
        """Close cleanly: tell ffmpeg to finish the current chunk and signal the
        workers to stop. Segments are crash-safe, so whatever the muxer doesn't
        finish here is recovered on next launch."""
        try:
            if self.proc is not None:
                try:
                    self.proc.stdin.write(b"q")
                    self.proc.stdin.flush()
                    self.proc.wait(timeout=5)
                except Exception:
                    try:
                        self.proc.terminate()
                    except Exception:
                        pass
            if self.mux_stop_event:
                self.mux_stop_event.set()
            if self.proc_stop_event:
                self.proc_stop_event.set()
        finally:
            self.root.destroy()


def main():
    root = tk.Tk()
    RecorderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
