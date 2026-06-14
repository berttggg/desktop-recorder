# Desktop Recorder

Records your screen and audio, then uses AI to summarize what you actually did —
producing a searchable, cross-day **knowledge base** of your work.

It captures the screen (plus microphone and/or system/desktop audio), and at the
end of a session an AI engine turns the footage into a structured day report:
activities, accomplishments, to-dos, and topics. Everything is stored locally and
browsable in a small web dashboard with semantic search.

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

## Privacy

- All recordings, transcripts, reports and the knowledge-base database stay
  **local** (under `recordings/`, which is git-ignored).
- On Google's **free** Gemini tier, uploaded content may be used to improve their
  products (including human review). Use a paid key, or the Claude backend, for
  sensitive recordings.

## Notes

- `recordings/`, `ffmpeg/`, `_settings.json` and `__pycache__/` are intentionally
  excluded from version control (see `.gitignore`).
