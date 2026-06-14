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
  search your own past by meaning (local semantic search), not just text.
- **Works while you work** — on the Gemini backend it analyzes each ~10-minute
  chunk live, so your day report is essentially ready the moment you stop.
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
- **Live analysis while recording** (Gemini): each ~10-minute chunk is analyzed
  as you go, so the day report is almost ready the moment you stop.
- **Automatic model failover** (Gemini): if a model is overloaded (503) or its
  free daily quota is exhausted (429), it transparently switches to another.
- **Live model discovery**: the model picker is populated from the API, so newly
  released models show up automatically.
- **Knowledge base dashboard**: a local web UI to browse past days and search
  them by meaning (local embeddings via fastembed).

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
for training), prefer the Claude backend, or simply don't record sensitive
screens. Recordings, transcripts, reports, and the knowledge-base database
themselves stay **local** (under `recordings/`, which is git-ignored) — it's the
*analysis* step that ships your content to the cloud.

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
  system audio, choose the Gemini model, then Start/Stop. Analysis runs on stop
  (or live, while recording, on the Gemini backend).
- **`Open Dashboard.bat`** — open the knowledge-base dashboard in your browser to
  review and search past sessions.

## Notes

- `recordings/`, `ffmpeg/`, `_settings.json` and `__pycache__/` are intentionally
  excluded from version control (see `.gitignore`).
