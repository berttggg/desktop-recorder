@echo off
rem ============================================================
rem  Switch the day-analysis engine to Google Gemini (free tier).
rem  Gemini watches the video AND listens to the audio directly,
rem  so no separate frame-sampling or transcription is needed.
rem  You need a free API key from Google AI Studio (one-time).
rem ============================================================
title Use Gemini (free)

setx ANALYSIS_BACKEND gemini >nul
echo Analysis engine is now: Gemini (free tier).
echo.
echo ONE-TIME SETUP - set your free API key yourself:
echo   1. Get a key at https://aistudio.google.com/apikey
echo   2. Open a Command Prompt and run:
echo.
echo        setx GEMINI_API_KEY your_key_here
echo.
echo   3. Close and reopen "Start Recorder" so it sees the change.
echo.
echo Note: on Google's FREE tier your recordings may be used to
echo improve their products (including human review). Switch to a
echo paid key, or click "Use Claude", if a recording is sensitive.
echo.
pause
