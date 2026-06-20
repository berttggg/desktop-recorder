# Desktop Recorder — Your Personal AI Assistant for Your Desktop

An always-on AI that **watches your screen, listens to your day, and remembers
everything for you.** It quietly records what you do, and then an AI engine turns
the raw footage into a structured, searchable memory of your work life:
activities, accomplishments, to-dos, and the topics you touched — automatically,
every day.

Think of it as a second brain that never forgets a meeting, never loses a train
of thought, and can answer "what was I working on last Tuesday afternoon?" by
*meaning*, not keywords. You stop taking notes; the assistant takes them for you.

## What it does for you

- **Remembers your whole day** — captures the screen plus microphone and/or
  system audio, so nothing slips through.
- **Writes your day report automatically** — an AI engine watches the footage and
  produces a clean summary: what you did, what you finished, what's still open,
  and the key topics.
- **Builds a personal knowledge base** — every day is saved and indexed so you can
  search your own past by meaning, not just text. The dashboard opens with a
  model-written **Overview** that ties your recent days together (what's
  progressing, what got finished, and what to focus on next). Embeddings run
  **on-device for free by default**, or switch to **Gemini embeddings** for
  higher-quality search.
- **Records now, uploads later** — recording only captures to disk in crash-safe
  ~10-minute segments; nothing is sent to the cloud until you click **Process
  recordings**. Ideal for spotty or restricted networks: record offline, then
  process once you're back online.
- **Survives a shutdown** — if your laptop dies (or you close the app) mid-record
  or mid-process, you don't lose your day: finished segments are safe on disk and
  processing resumes exactly where it left off.
- **Stays running through hiccups** — automatic model failover means if a model is
  overloaded or its free quota runs out, it transparently switches to another and
  keeps going.
- **Always up to date** — the model picker is populated live from the API, so
  newly released models show up on their own.

## Features

- **Screen + audio capture** via ffmpeg (mic and system-loopback audio).
- **Two analysis backends** (switchable):
  - **Gemini (free tier)** — watches the video *and* listens to the audio
    natively; no separate frame-sampling or transcription needed.
  - **Claude (Anthropic)** — samples frames + transcribes audio locally
    (faster-whisper) and analyzes with Claude. Works with no key too, falling
    back to a basic local summary.
- **On-demand, resumable batch processing**: recordings accumulate untouched
  until you click **Process recordings (N)**, which uploads + analyzes everything
  not yet done (across days). Each segment is checkpointed to disk the moment it
  finishes, so a crash/shutdown resumes from the next un-analyzed segment with no
  re-upload, a failed upload simply retries next time, and a failed run never
  overwrites your previous good report.
- **Automatic model failover** (Gemini): if a model is overloaded (503) or its
  free daily quota is exhausted (429), it transparently switches to another. You
  can pick the **Fallback model** to switch to in the GUI (or leave it on
  *(automatic)* for the built-in cheap-first chain), and an **Embedding
  fallback** for the Gemini embedding backend.
- **Live model discovery**: the model + embedding pickers are populated from the
  API and refreshed on launch and periodically, so newly released models show up
  on their own. Models are shown by their **exact dashboard display name** (e.g.
  "Gemini 3.1 Flash Lite") to match Google's usage dashboard, and **Gemma**
  models are listed too — marked "(text-only)" since they can't analyze video.
- **Knowledge base dashboard**: a local web UI to browse past days and search
  them by meaning.
- **Selectable embedding backend**: choose how the knowledge base is embedded for
  search, right in the GUI —
  - **Local (default)** — fastembed (`BAAI/bge-small-en-v1.5`, 384-dim) on CPU;
    offline, free, and fully private.
  - **Gemini** — Google's hosted `gemini-embedding-*` models (e.g.
    `gemini-embedding-2`); higher quality, but sends your KB text + queries to
    the cloud. The model list is discovered live from the API.

  Switching backends transparently re-embeds your existing days, and search only
  ever compares vectors from the same model, so the two never get mixed up.

## ⚠️ Data & privacy — read this first

**This tool is built for people who do not care about data security.**

To do its job, it captures **everything you do on your laptop** — your screen,
your microphone, and your system audio — and **sends that footage to cloud AI
models** (Google Gemini and/or Anthropic Claude) for analysis. Whatever is on
your screen while it records — emails, passwords typed in plain text, private
messages, financial data, client documents, code, anything — can be uploaded.

In particular:

- **On Google's *free* Gemini tier, your uploaded content may be used to improve
  their products — including review by human reviewers.** That means real people
  may see your screen recordings and hear your audio.
- The Claude (Anthropic) backend also sends sampled frames and audio to
  Anthropic's API for analysis.

If you handle sensitive, confidential, regulated, or client-owned data — or if
you simply value your privacy — **this tool in its default configuration is not
for you.** Only use it if you are genuinely comfortable with the contents of your
screen and audio leaving your machine and being processed (and possibly
human-reviewed) by third-party AI providers.

If you want to reduce exposure: use a **paid** API key (paid tiers are not used
for training), prefer the Claude backend, keep embeddings on the **Local**
backend, or simply don't record sensitive screens. Recordings, transcripts,
reports, and the knowledge-base database themselves stay **local** (under
`recordings/`, which is git-ignored) — it's the *analysis* step (and, if you pick
the **Gemini** embedding backend, the *search-indexing* step) that ships your
content to the cloud. With the default **Local** embedding backend, search runs
entirely on your machine.

## Requirements

- Windows, Python 3.10+ (developed on 3.13).
- **ffmpeg** and **ffprobe** on your `PATH`, or placed in a `ffmpeg/` folder next
  to the scripts (`ffmpeg/ffmpeg.exe`, `ffmpeg/ffprobe.exe`).
- Python packages: `pip install -r requirements.txt`

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
2. Choose an analysis engine and set your **own** API key (the key is read from
   an environment variable — it is never stored in this repo):
   - **Gemini (free):** double-click `Use Gemini (free).bat`, then set your key:
     ```
     setx GEMINI_API_KEY your_key_here
     ```
     Get a free key at https://aistudio.google.com/apikey
   - **Claude:** double-click `Use Claude.bat`, then:
     ```
     setx ANTHROPIC_API_KEY your_key_here
     ```
3. Close and reopen the launcher so it picks up the new environment values.

## Usage

- **`Start Recorder.bat`** — launch the recorder GUI. Pick your mic, toggle
  system audio, choose the Gemini model, then Start/Stop. **Recording only
  captures to disk — nothing is uploaded.** When you're ready (e.g. once your
  VPN is up), click **Process recordings (N)** to upload + analyze everything
  that's accumulated; *N* is how many recordings are still waiting. Processing
  runs in the background, is resumable, and opens the report when it finishes.
- **`Open Dashboard.bat`** — open the knowledge-base dashboard in your browser to
  review and search past sessions.
- **`Install Autostart.bat`** — make the recorder open automatically every time
  you log in to Windows (it drops a hidden launcher in your Startup folder, so no
  console window appears). Run **`Uninstall Autostart.bat`** to turn it off.

## Troubleshooting

**Every upload fails / "connection timed out" / "forbidden" (WinError 10060 or
10013).** The Gemini backend talks to `generativelanguage.googleapis.com`. If
your network can't reach Google directly — a corporate firewall, or a region
where Google is blocked — every upload (and the daily synthesis) fails and the
report comes back empty. The fix is to route the recorder through a local
proxy/VPN:

- In the recorder GUI, put your proxy address in the **Proxy** box, e.g.
  `http://127.0.0.1:7890` (the typical port for a Clash/V2Ray client). It
  applies immediately and is remembered.
- Or set it once from a terminal and restart the launcher:
  ```
  setx RECORDER_PROXY http://127.0.0.1:7890
  ```
  A standard `HTTPS_PROXY` / `ALL_PROXY` environment variable works too and
  takes precedence over the GUI box. The proxy is used for **all** Google calls
  — analysis, daily synthesis, and (if selected) Gemini embeddings.

Uploads also now **retry with backoff** and fail fast on a dead connection
instead of hanging, so a brief network blip no longer loses a whole chunk.

**Every upload fails with "CERTIFICATE_VERIFY_FAILED / self-signed certificate
in certificate chain."** This is *different* from the errors above — the
connection reaches Google fine, but a corporate VPN/firewall (e.g. Xgate,
Zscaler) or antivirus is **inspecting HTTPS**: it terminates the TLS connection
and re-signs it with its own root certificate. Your browser trusts that
certificate because IT installed it in the **Windows certificate store**, but
Python ships its own separate trust list (`certifi`) and ignores the OS store,
so it rejects the certificate. The fix is to let the recorder trust the same
store your browser does:

```
pip install truststore
```

Then restart the recorder — uploads will trust the OS certificate store (which
already contains the corporate CA), whether or not inspection is active at the
moment. It's in `requirements.txt`, so a fresh `pip install -r requirements.txt`
covers it too. If for some reason the CA isn't in the Windows store, point the
recorder at the corporate root-CA `.pem` directly:

```
setx RECORDER_CA_BUNDLE C:\path\to\corp-root-ca.pem
```

As a last resort you can disable certificate checking entirely with
`setx RECORDER_SSL_VERIFY 0` (and restart) — but this turns off protection
against interception, so only use it if nothing else works.

If you can't use a proxy, switch analysis to the **Claude** backend
(`Use Claude.bat`) and keep embeddings on **Local** — that path doesn't touch
Google at all.

## Notes

- `recordings/`, `ffmpeg/`, `_settings.json` and `__pycache__/` are intentionally
  excluded from version control (see `.gitignore`).
