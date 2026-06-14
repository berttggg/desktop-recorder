@echo off
rem ============================================================
rem  Switch the day-analysis engine back to Claude (Anthropic).
rem  Needs ANTHROPIC_API_KEY set for AI summaries; with no key
rem  at all it still makes a basic local summary.
rem ============================================================
title Use Claude

setx ANALYSIS_BACKEND claude >nul
echo Analysis engine is now: Claude (Anthropic).
echo Close and reopen "Start Recorder" so it sees the change.
echo.
pause
