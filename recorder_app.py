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

# Long recordings are split into segments of this length (seconds). Each
# session becomes a folder of seg_000.mp4, seg_001.mp4, … which the analyzer
# stitches back into one timeline.
SEGMENT_SECONDS = int(os.environ.get("RECORDER_SEGMENT_SECONDS", "3600"))

# Live (during-recording) analysis: capture straight into short *finalized*
# chunks so a background worker can analyze each one with Gemini while the next
# records. Shorter than SEGMENT_SECONDS so feedback arrives while you work.
LIVE_SEGMENT_SECONDS = int(os.environ.get("RECORDER_LIVE_SEGMENT_SECONDS", "600"))
LIVE_POLL_SECONDS = float(os.environ.get("RECORDER_LIVE_POLL_SECONDS", "2"))


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
        self.tmpfile = None      # ffmpeg output (video + optional mic)
        self.has_mic = False
        self.sys_rec = None      # SystemAudioRecorder instance
        self.sys_wav = None
        self.start_time = None
        self.timer_job = None
        self.session_started_iso = None
        self.session_ended_iso = None
        self.live = False             # live during-recording analysis active?
        self.live_thread = None       # background chunk mux+analyze worker
        self.live_stop_event = None   # signals the worker to drain & finish
        self._live = None             # accumulated {blocks,transcript,base,tmp_dirs}
        self._settings = _load_settings()
        self._applied_model = None    # last Gemini model pushed into gemini.MODEL

        root.title("Desktop Recorder + Analyzer")
        root.geometry("700x620")
        root.minsize(620, 540)

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
            model_values = list(getattr(_gem, "KNOWN_MODELS", []))
            default_model = _gem.MODEL
        except Exception:
            model_values = ["gemini-2.5-flash", "gemini-2.5-flash-lite",
                            "gemini-2.0-flash", "gemini-2.0-flash-lite"]
            default_model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        saved = self._settings.get("gemini_model")
        if saved:
            default_model = saved
        if default_model and default_model not in model_values:
            model_values = [default_model] + model_values

        ana = ttk.LabelFrame(root, text="Analysis")
        ana.pack(fill="x", padx=14, pady=(0, 8))
        ana.columnconfigure(1, weight=1)

        self.live_var = tk.BooleanVar(value=True)
        self.live_chk = ttk.Checkbutton(
            ana, text="Analyze while recording  (free Gemini backend)",
            variable=self.live_var)
        self.live_chk.grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(8, 4))

        ttk.Label(ana, text="Gemini model:").grid(row=1, column=0, sticky="w",
                                                  padx=(10, 4), pady=(0, 10))
        self.model_var = tk.StringVar(value=default_model)
        self.model_combo = ttk.Combobox(ana, textvariable=self.model_var,
                                        values=model_values)
        self.model_combo.grid(row=1, column=1, sticky="we", padx=(4, 4), pady=(0, 10))
        self.model_combo.bind("<<ComboboxSelected>>", self._apply_model)
        self.model_combo.bind("<Return>", self._apply_model)
        self.model_combo.bind("<FocusOut>", self._apply_model)
        self.model_refresh_btn = ttk.Button(ana, text="↻", width=3,
                                            command=self._refresh_models)
        self.model_refresh_btn.grid(row=1, column=2, sticky="w", padx=(0, 10), pady=(0, 10))
        self._applied_model = default_model
        if os.environ.get("ANALYSIS_BACKEND", "claude").strip().lower() != "gemini":
            self.model_combo.config(state="disabled")
            self.model_refresh_btn.config(state="disabled")
        elif saved:
            # An explicit prior in-app choice wins over the import-time default.
            self._apply_model(announce=False, force=True)

        # ---- Action buttons ----------------------------------------------
        btns = ttk.Frame(root)
        btns.pack(fill="x", padx=14, pady=(0, 8))
        self.rec_btn = ttk.Button(btns, text="●  Start Recording", command=self.toggle_record)
        self.rec_btn.pack(side="left")
        self.analyze_btn = ttk.Button(btns, text="Analyze last recording",
                                      command=self.analyze_last, state="disabled")
        self.analyze_btn.pack(side="left", padx=8)
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
        _ToolTip(self.live_chk, "Analyze each ~10-min chunk with Gemini while you record, so "
                 "the day report is almost ready when you stop. Needs ANALYSIS_BACKEND=gemini "
                 "and a GEMINI_API_KEY — run 'Use Gemini (free).bat', then reopen.")
        _ToolTip(self.model_combo, "Which Gemini model analyzes your recording. If one model's "
                 "free daily quota runs out, switch to another here — it applies right away "
                 "(even mid-recording) and is remembered. You can also type any model name.")
        _ToolTip(self.model_refresh_btn, "Refresh this list from Google — pulls every model your "
                 "key can use right now (including new ones). Needs an internet connection.")
        _ToolTip(self.analyze_btn, "Run AI analysis on the most recent recording and open the report.")
        _ToolTip(self.dash_btn, "Open your cross-day knowledge base: summaries, to-dos and search.")
        _ToolTip(self.open_btn, "Open the folder where recordings and reports are saved.")

        self.refresh_devices()
        if not self.ffmpeg:
            self.write("ffmpeg not found. Install it (winget install Gyan.FFmpeg) and reopen the app.")
            self.rec_btn.config(state="disabled")
        else:
            self.write(f"Using ffmpeg: {self.ffmpeg}")
        self._log_backend_status()
        self._update_backend_badge()
        # Quietly pull the live model list so the dropdown reflects what the key
        # can actually use (incl. newly released models), without blocking launch.
        if os.environ.get("ANALYSIS_BACKEND", "claude").strip().lower() == "gemini":
            self._refresh_models(announce=False, initial=True)

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
                live = "ON" if self.live_var.get() else "available (checkbox off)"
                self.write(f"Backend: Gemini ({self._gemini_model()}) — free native "
                           f"video analysis. Live during-recording analysis is {live}.")
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
                     (self.fps_spin, st), (self.sysaudio_chk, st), (self.live_chk, st)):
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

    def _apply_model(self, *_args, announce=True, force=False):
        """Push the chosen Gemini model into the backend so the next chunk /
        analysis uses it. Lets you switch models when one's free daily quota is
        used up — no restart needed. The choice is remembered across restarts."""
        model = (self.model_var.get() or "").strip()
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
            self.write(f"Gemini analysis model → {model}.")
        self._update_backend_badge()

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
        current = (self.model_var.get() or "").strip()
        values = list(models)
        if current and current not in values:   # keep a typed/custom choice visible
            values = [current] + values
        self.model_combo["values"] = values
        if announce:
            self.write(f"Found {len(models)} model(s) available to your key.")

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
        try:
            fps = int(self.fps_var.get())
        except ValueError:
            fps = 15
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_dir = os.path.join(REC_DIR, f"session_{ts}")
        os.makedirs(self.session_dir, exist_ok=True)
        self.tmpfile = os.path.join(self.session_dir, "_capture.mp4")
        self.sys_wav = os.path.join(self.session_dir, "_sys.wav")
        self.session_started_iso = dt.datetime.now().isoformat(timespec="seconds")
        self.session_ended_iso = None

        audio = self.audio_var.get()
        self.has_mic = bool(audio and audio != "(no microphone)")

        self.live = bool(self.live_var.get() and self._live_active())
        if self.live_var.get() and not self.live:
            self.write("Live analysis needs the Gemini backend + key "
                       "(run \"Use Gemini (free).bat\" and set GEMINI_API_KEY); "
                       "recording normally — you can Analyze afterward.")

        cmd = [
            self.ffmpeg, "-hide_banner", "-y",
            "-f", "gdigrab", "-framerate", str(fps), "-i", "desktop",
        ]
        if self.has_mic:
            cmd += ["-f", "dshow", "-i", f"audio={audio}"]
        cmd += [
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            # Keyframe at least every ~10s so the segment muxer can cut cleanly
            # near each boundary (segments split only on keyframes).
            "-g", str(max(2, fps) * 10),
        ]
        if self.has_mic:
            cmd += ["-c:a", "aac", "-b:a", "128k"]
        if self.live:
            # Write straight into short *finalized* chunks (_vraw_000.mp4, …) so
            # the worker can mux+analyze each one while the next records.
            cmd += ["-f", "segment", "-segment_time", str(LIVE_SEGMENT_SECONDS),
                    "-reset_timestamps", "1", "-segment_format", "mp4",
                    os.path.join(self.session_dir, "_vraw_%03d.mp4")]
        else:
            cmd += [self.tmpfile]

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
                first_wav = (os.path.join(self.session_dir, "_sraw_000.wav")
                             if self.live else self.sys_wav)
                self.sys_rec = audio_capture.SystemAudioRecorder(first_wav)
                self.sys_rec.start()
                self.write("Capturing system audio (loopback).")
            except Exception as e:
                self.sys_rec = None
                self.write(f"System audio unavailable: {e}")

        self.start_time = time.time()
        self.rec_btn.config(text="■  Stop Recording")
        self.analyze_btn.config(state="disabled")
        self._set_settings_enabled(False)
        self.status_lbl.config(foreground=_BUSY)

        if self.live:
            # Hand sys_rec to the worker — it owns rotation/stop from here on.
            self._live = {"blocks": [], "transcript": [], "base": 0.0,
                          "tmp_dirs": [os.path.join(self.session_dir, "_gem_thumbs")]}
            self.live_stop_event = threading.Event()
            self.live_thread = threading.Thread(
                target=self._live_loop,
                args=(self.session_dir, self.ffmpeg, self.has_mic,
                      self.sys_rec, self.live_stop_event, self._live),
                daemon=True)
            self.live_thread.start()
            self.write(f"Recording started → {os.path.basename(self.session_dir)}\\ "
                       f"(live analysis every {LIVE_SEGMENT_SECONDS // 60} min "
                       f"via Gemini {self._gemini_model()})")
        else:
            self.write(f"Recording started → {os.path.basename(self.session_dir)}\\ "
                       f"(segments every {SEGMENT_SECONDS // 60} min)")
        self._tick()

    def _tick(self):
        if self.proc is None:
            return
        elapsed = int(time.time() - self.start_time)
        msg = f"● Recording   {elapsed // 60:02d}:{elapsed % 60:02d}"
        if self.live and self._live is not None:
            nb = len(self._live.get("blocks") or [])
            msg += f"      live analysis: {nb} block(s) so far"
        self.status.set(msg)
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

        if self.live:
            # The last _vraw chunk is now finalized; let the worker drain it.
            # (The worker owns sys_rec — it stops the loopback itself.)
            self.status_lbl.config(foreground=_INFO)
            self.status.set("Finalizing…")
            self._stop_live()
            return

        self.status_lbl.config(foreground=_INFO)
        self.status.set("Saving…")
        sys_ok = False
        if self.sys_rec is not None:
            try:
                self.sys_rec.stop()
                sys_ok = (os.path.isfile(self.sys_wav)
                          and os.path.getsize(self.sys_wav) > 1000
                          and self.sys_rec.error is None)
                if not sys_ok:
                    self.write("System audio produced no data — skipping it.")
            except Exception as e:
                self.write(f"System audio stop error: {e}")
            self.sys_rec = None

        self._finalize(sys_ok)
        self.status_lbl.config(foreground=_OK)
        self.status.set("Ready.")
        self._set_settings_enabled(True)

    def _finalize(self, sys_ok):
        if not (self.tmpfile and os.path.isfile(self.tmpfile)):
            self.write("Warning: capture file not found.")
            return
        self.write("Mixing system audio and splitting into segments…" if sys_ok
                   else "Splitting recording into segments…")

        n = self._segment(self.tmpfile, self.sys_wav, self.session_dir,
                          self.has_mic, sys_ok)
        if n:
            for f in (self.tmpfile, self.sys_wav):
                try:
                    if os.path.isfile(f):
                        os.remove(f)
                except Exception:
                    pass
        else:
            # Segmenting failed — keep the raw capture as the single segment so
            # the session is still analyzable.
            try:
                os.replace(self.tmpfile, os.path.join(self.session_dir, "seg_000.mp4"))
                self.write("Segmenting failed; kept the raw capture as seg_000.mp4.")
            except Exception as e:
                self.write(f"Finalize error: {e}")
                return

        segs = sorted(glob.glob(os.path.join(self.session_dir, "seg_*.mp4")))
        if not segs:
            self.write("Warning: no output segments found.")
            return
        size = sum(os.path.getsize(s) for s in segs) / 1e6
        label = "video + mic + system audio" if (sys_ok and self.has_mic) else \
                "video + system audio" if sys_ok else \
                "video + mic" if self.has_mic else "video"
        self.write(f"Saved {len(segs)} segment(s) in "
                   f"{os.path.basename(self.session_dir)}\\ ({size:.1f} MB) [{label}]")
        self.analyze_btn.config(state="normal")

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

    def _segment(self, video, sys_wav, session_dir, has_mic, sys_ok):
        """Write seg_000.mp4, seg_001.mp4, … into session_dir; return the count.

        Video is copied (no re-encode); when system audio is present it is mixed
        in (with the mic, if any) and re-encoded to AAC. -reset_timestamps makes
        each segment start at t=0 so the analyzer can stitch them by duration.
        """
        pattern = os.path.join(session_dir, "seg_%03d.mp4")
        seg_opts = ["-f", "segment", "-segment_time", str(SEGMENT_SECONDS),
                    "-reset_timestamps", "1", "-segment_format", "mp4"]
        extra_in, maps, codec = self._audio_args(sys_wav, has_mic, sys_ok)
        cmd = ([self.ffmpeg, "-hide_banner", "-y", "-i", video]
               + extra_in + maps + codec + seg_opts + [pattern])
        try:
            r = subprocess.run(
                cmd, capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception as e:
            self.write(f"Segment error: {e}")
            return 0
        if r.returncode != 0:
            return 0
        return len(glob.glob(os.path.join(session_dir, "seg_*.mp4")))

    # ---- Live (during-recording) analysis ---------------------------------
    def _live_active(self):
        """True if live during-recording analysis can run: backend is Gemini and
        the SDK + key are present. Live mode is Gemini-only — the Claude/Whisper
        pipeline is too CPU-heavy to run alongside the capture."""
        if os.environ.get("ANALYSIS_BACKEND", "claude").strip().lower() != "gemini":
            return False
        try:
            import gemini
            return gemini.available()
        except Exception:
            return False

    def _gemini_model(self):
        try:
            import gemini
            return gemini.MODEL
        except Exception:
            return "gemini"

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

    def _process_chunk(self, session_dir, ffmpeg, has_mic, idx, live, log):
        """Mux one finalized chunk into seg_NNN.mp4 and analyze it with Gemini,
        accumulating results (in session-absolute time) into ``live``."""
        vraw = os.path.join(session_dir, f"_vraw_{idx:03d}.mp4")
        if not (os.path.isfile(vraw) and os.path.getsize(vraw) > 1000):
            log(f"  live: chunk {idx} missing/empty — skipping.")
            return
        sraw = os.path.join(session_dir, f"_sraw_{idx:03d}.wav")
        sys_ok = os.path.isfile(sraw) and os.path.getsize(sraw) > 1000
        seg = os.path.join(session_dir, f"seg_{idx:03d}.mp4")

        if self._mux_one(vraw, sraw, seg, has_mic, sys_ok):
            for f in (vraw, sraw):
                try:
                    if os.path.isfile(f):
                        os.remove(f)
                except Exception:
                    pass
        else:
            # Mux failed — keep the raw video chunk (mic embedded, no sys audio)
            # so the chunk is still analyzable.
            try:
                os.replace(vraw, seg)
                log(f"  live: chunk {idx} mux failed; kept raw video as "
                    f"{os.path.basename(seg)}.")
            except Exception as e:
                log(f"  live: chunk {idx} unusable ({e}).")
                return
            try:
                if os.path.isfile(sraw):
                    os.remove(sraw)
            except Exception:
                pass

        base = live.get("base", 0.0)
        log(f"  live: analyzing chunk {idx} (@{analyze.fmt_clock(base)})…")
        blocks, transcript, dur = insights.analyze_chunk_live(seg, base, ffmpeg, log=log)
        live["blocks"].extend(blocks)
        live["transcript"].extend(transcript)
        live["base"] = base + (dur or 0.0)
        log(f"  live: chunk {idx} → {len(blocks)} block(s); session now "
            f"{analyze.fmt_clock(live['base'])}.")

    def _live_loop(self, session_dir, ffmpeg, has_mic, sys_rec, stop_event, live):
        """Background worker: while recording, mux+analyze each finalized chunk.

        The video segment muxer writes _vraw_000.mp4, _vraw_001.mp4, … ; chunk N
        is finalized once _vraw_{N+1}.mp4 appears (the muxer lags by one). For
        each finalized chunk we rotate the system-audio WAV to match, mux
        video+audio into seg_NNN.mp4, and hand it to Gemini. Accumulated results
        land in ``live`` for the finalizer at stop. Never raises into the app."""
        log = lambda m: self.root.after(0, self.write, m)
        rotated_to = 0   # index of the currently-open _sraw WAV (start opened _000)
        processed = 0    # next chunk index to mux + analyze
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
                        log(f"  live: system-audio rotate failed ({e}).")
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
                        self._process_chunk(session_dir, ffmpeg, has_mic,
                                            processed, live, log)
                    except Exception as e:
                        log(f"  live: chunk {processed} error ({e}).")
                    processed += 1

                if draining and processed > maxidx:
                    break
                stop_event.wait(LIVE_POLL_SECONDS)
        except Exception as e:
            log(f"Live worker stopped on error: {e}")

    def _stop_live(self):
        """Signal the live worker to drain the last chunk(s) and finish. The
        join + daily synthesis run on a background thread so the GUI stays live."""
        self.write("Stopping… finishing live analysis of the last chunk(s).")
        self.analyze_btn.config(state="disabled")
        self.rec_btn.config(state="disabled")
        if self.live_stop_event:
            self.live_stop_event.set()
        meta = {"started": self.session_started_iso, "ended": self.session_ended_iso}
        threading.Thread(
            target=self._live_finalize_worker,
            args=(self.session_dir, self._live, self.live_thread, meta),
            daemon=True).start()

    def _live_finalize_worker(self, session_dir, live, thread, meta):
        """Wait for the live worker to drain, then synthesize the day and open
        the report. Falls back to a full at-stop analysis if live produced no
        blocks (e.g. Gemini was unreachable the whole session)."""
        log = lambda m: self.root.after(0, self.write, m)
        try:
            if thread:
                thread.join()
            blocks = (live or {}).get("blocks") or []
            transcript = (live or {}).get("transcript") or []
            total_dur = (live or {}).get("base", 0.0)
            tmp_dirs = (live or {}).get("tmp_dirs") or []
            if blocks:
                report, summary = insights.finalize_session_live(
                    session_dir, REC_DIR, blocks, transcript, total_dur,
                    meta=meta, tmp_dirs=tmp_dirs, log=log)
            else:
                log("Live analysis produced no blocks — running a full analysis "
                    "of the saved segments instead.")
                report, summary = insights.analyze_session(
                    session_dir, self.ffmpeg, REC_DIR, meta=meta, log=log)
            self.root.after(0, self._show_summary, summary)
            if report and os.path.isfile(report):
                self.root.after(0, self.write, "Opening visual report…")
                try:
                    os.startfile(report)
                except Exception:
                    pass
        except Exception as e:
            self.root.after(0, self.write, f"Live finalize error: {e}")
        finally:
            self.live = False
            self.root.after(0, lambda: self.status_lbl.config(foreground=_OK))
            self.root.after(0, lambda: self.status.set("Ready."))
            self.root.after(0, lambda: self.rec_btn.config(state="normal"))
            self.root.after(0, lambda: self.analyze_btn.config(state="normal"))
            self.root.after(0, lambda: self._set_settings_enabled(True))

    # ---- Analysis ---------------------------------------------------------
    def analyze_last(self):
        segs = (sorted(glob.glob(os.path.join(self.session_dir, "seg_*.mp4")))
                if self.session_dir else [])
        if not segs:
            messagebox.showinfo("No recording", "Record something first.")
            return
        if not os.environ.get("ANTHROPIC_API_KEY"):
            self.write("No ANTHROPIC_API_KEY — will produce transcript only (no Claude summary).")
        self.analyze_btn.config(state="disabled")
        self.rec_btn.config(state="disabled")
        self.status_lbl.config(foreground=_INFO)
        self.status.set("Analyzing…")
        meta = {"started": self.session_started_iso, "ended": self.session_ended_iso}
        threading.Thread(target=self._analyze_worker,
                         args=(self.session_dir, meta), daemon=True).start()

    def _analyze_worker(self, session_dir, meta):
        try:
            report, summary = insights.analyze_session(
                session_dir, self.ffmpeg, REC_DIR, meta=meta,
                log=lambda m: self.root.after(0, self.write, m),
            )
            self.root.after(0, self._show_summary, summary)
            if report and os.path.isfile(report):
                self.root.after(0, self.write, "Opening visual report…")
                try:
                    os.startfile(report)
                except Exception:
                    pass
        except Exception as e:
            self.root.after(0, self.write, f"Analysis error: {e}")
        finally:
            self.root.after(0, lambda: self.status_lbl.config(foreground=_OK))
            self.root.after(0, lambda: self.status.set("Ready."))
            self.root.after(0, lambda: self.rec_btn.config(state="normal"))
            self.root.after(0, lambda: self.analyze_btn.config(state="normal"))

    def _show_summary(self, summary):
        self.log.insert("end", "\n===== SUMMARY =====\n")
        self.log.insert("end", summary + "\n")
        self.log.see("end")


def main():
    root = tk.Tk()
    RecorderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
