"""Resumable, crash-safe batch processing of recorded sessions.

Recording (see recorder_app) only *captures* video into ``seg_*.mp4`` — nothing
is uploaded to Gemini until the user clicks **Process**, which calls
``process_all_pending`` here. This is the "accumulate, then process in a batch"
half of the app, designed to survive the laptop being shut down at any moment.

Durability model
----------------
* **Per-segment checkpoints.** Each ``seg_NNN.mp4`` is analyzed independently
  (``base=0``, i.e. segment-relative time) and its result written to
  ``seg_NNN.blocks.json`` *atomically*. The presence of that file means "done",
  so a crash/shutdown resumes from the next un-checkpointed segment with **no
  re-upload** of work already finished.
* **Failed uploads stay pending.** ``gemini.analyze_one`` reports ``ok=False``
  when a segment's upload failed (bad network / VPN down). Such a segment is left
  *without* a checkpoint, so the next Process run retries just it.
* **Atomic publish / fallback to previous.** After the map phase the checkpoints
  are assembled (segment-relative → session-absolute time) and handed to
  ``insights.finalize_from_blocks``, which writes ``insights.json`` /
  ``report.html`` atomically and updates the KB + dashboard. If that step fails,
  the previous good report is left untouched.
* **state.json** records ``{recording, processed, …}``; a session needs
  processing while it has segments, is not currently recording, and is not yet
  ``processed``.
"""

import os
import glob
import json
import datetime as dt

import analyze
import insights

STATE_NAME = "state.json"
CHECKPOINT_SUFFIX = ".blocks.json"


# --------------------------------------------------------------------------
# Session discovery + state.json
# --------------------------------------------------------------------------
def segments(session_dir):
    """All finalized capture segments in a session, in chronological order."""
    return sorted(glob.glob(os.path.join(session_dir, "seg_*.mp4")))


def session_dirs(rec_dir):
    """All session folders under ``rec_dir``, oldest first."""
    return sorted(d for d in glob.glob(os.path.join(rec_dir, "session_*"))
                  if os.path.isdir(d))


def state_path(session_dir):
    return os.path.join(session_dir, STATE_NAME)


def read_state(session_dir):
    try:
        with open(state_path(session_dir), encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def write_state(session_dir, **updates):
    """Merge ``updates`` into the session's state.json and write it atomically."""
    st = read_state(session_dir)
    st.update(updates)
    analyze.atomic_write_json(state_path(session_dir), st)
    return st


def is_recording(session_dir):
    return read_state(session_dir).get("recording") is True


def is_done(session_dir):
    """True if this session has already been finalized and needs no processing.

    A new-style session is done once state.processed is True. A *legacy* session
    (recorded before this refactor, so no state.json) counts as done if it has a
    report.html — that means the old at-stop pipeline already analyzed it, and we
    must not silently re-upload it."""
    st = read_state(session_dir)
    if st.get("processed") is True:
        return True
    if not st and os.path.isfile(os.path.join(session_dir, "report.html")):
        return True
    return False


def needs_processing(session_dir):
    return (bool(segments(session_dir))
            and not is_recording(session_dir)
            and not is_done(session_dir))


def pending_sessions(rec_dir):
    return [d for d in session_dirs(rec_dir) if needs_processing(d)]


def pending_summary(rec_dir):
    """(n_sessions, n_unanalyzed_segments) across everything still to process —
    used for the "Process recordings (N)" button label."""
    sess = pending_sessions(rec_dir)
    segs = 0
    for d in sess:
        segs += sum(1 for s in segments(d) if not os.path.isfile(checkpoint_path(s)))
    return len(sess), segs


# --------------------------------------------------------------------------
# Per-segment checkpoints
# --------------------------------------------------------------------------
def checkpoint_path(seg):
    """seg_007.mp4 -> seg_007.blocks.json (beside it)."""
    return os.path.splitext(seg)[0] + CHECKPOINT_SUFFIX


def read_checkpoint(seg):
    try:
        with open(checkpoint_path(seg), encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else None
    except Exception:
        return None


def _write_checkpoint(seg, blocks, transcript, dur):
    analyze.atomic_write_json(checkpoint_path(seg), {
        "ok": True,
        "dur": float(dur or 0.0),
        "blocks": blocks,
        # tuples -> lists through JSON; assemble() reads them back as [t0,t1,txt]
        "transcript": [list(t) for t in transcript],
    })


def analyze_segment(seg, ffmpeg, log=print):
    """Ensure ``seg`` has a checkpoint. Returns True if it does afterwards
    (already had one, or analysis succeeded), False if it was left pending
    (upload failed / Gemini errored) so a later run can retry it."""
    if os.path.isfile(checkpoint_path(seg)):
        return True
    name = os.path.basename(seg)
    try:
        import gemini
        # base=0: store segment-relative times so segments are independent and
        # can be analyzed/resumed in any order; assemble() offsets them later.
        blocks, transcript, dur, ok = gemini.analyze_one(seg, 0.0, ffmpeg, log=log)
    except Exception as e:
        log(f"  {name}: analysis error ({e}); left pending, will retry.")
        return False
    if not ok:
        log(f"  {name}: upload failed; left pending, will retry next time.")
        return False
    _write_checkpoint(seg, blocks, transcript, dur)
    log(f"  {name}: done ({len(blocks)} block(s)) → checkpoint saved.")
    return True


# --------------------------------------------------------------------------
# Assemble checkpoints -> session-absolute blocks/transcript
# --------------------------------------------------------------------------
def assemble(session_dir, ffmpeg):
    """Stitch all available checkpoints into session-absolute time.

    Returns (blocks, transcript, total_dur, n_done, n_total). Each segment's
    duration advances the running base whether or not it was analyzed (a pending
    segment in the middle still occupies its time slot), so the timeline stays
    aligned even for a partial report."""
    segs = segments(session_dir)
    base = 0.0
    blocks, transcript = [], []
    n_done = 0
    for seg in segs:
        cp = read_checkpoint(seg)
        if cp:
            n_done += 1
            dur = float(cp.get("dur") or 0.0)
            for b in cp.get("blocks") or []:
                nb = dict(b)
                nb["t0"] = (b.get("t0") or 0.0) + base
                nb["t1"] = (b.get("t1") or 0.0) + base
                blocks.append(nb)
            for t in cp.get("transcript") or []:
                if len(t) >= 3:
                    transcript.append((t[0] + base, t[1] + base, t[2]))
        else:
            dur = analyze.get_duration(seg, ffmpeg) or 0.0
        base += dur
    blocks.sort(key=lambda b: b["t0"])
    transcript.sort(key=lambda t: t[0])
    return blocks, transcript, base, n_done, len(segs)


# --------------------------------------------------------------------------
# Process one session / all pending sessions
# --------------------------------------------------------------------------
def _meta_from_state(session_dir):
    st = read_state(session_dir)
    return {"started": st.get("started"), "ended": st.get("ended")}


def process_session(session_dir, ffmpeg, rec_dir, meta=None, log=print,
                    should_stop=None, progress=None):
    """Analyze every un-checkpointed segment of one session, then (re)build its
    report from all checkpoints. Resumable and interruptible: ``should_stop()``
    is polled between segments so closing the app / hitting stop leaves a clean,
    resumable state. Returns a small result dict."""
    name = os.path.basename(session_dir.rstrip("\\/"))
    segs = segments(session_dir)
    if not segs:
        return {"session": name, "analyzed": 0, "total": 0, "processed": False}

    # --- map phase: checkpoint each pending segment ------------------------
    for i, seg in enumerate(segs):
        if should_stop and should_stop():
            log(f"{name}: stopped — progress is saved, resume any time.")
            return {"session": name, "analyzed": None, "total": len(segs),
                    "processed": False, "stopped": True}
        if progress:
            progress(name, i, len(segs))
        analyze_segment(seg, ffmpeg, log=log)

    # --- assemble + publish ------------------------------------------------
    blocks, transcript, total_dur, n_done, n_total = assemble(session_dir, ffmpeg)
    if n_done == 0:
        log(f"{name}: no segments could be analyzed (network down?) — "
            "keeping the previous report; will retry next time.")
        write_state(session_dir, last_error="no segments analyzed")
        return {"session": name, "analyzed": 0, "total": n_total, "processed": False}

    meta = meta or _meta_from_state(session_dir)
    try:
        # tmp_dirs=None: keep _gem_thumbs so a later partial->complete re-run can
        # re-render; recordings/ is local + git-ignored, so the jpgs are cheap.
        report, summary = insights.finalize_from_blocks(
            session_dir, rec_dir, blocks, transcript, total_dur,
            meta=meta, tmp_dirs=None, log=log)
    except Exception as e:
        log(f"{name}: finalize failed ({e}); previous report kept.")
        write_state(session_dir, last_error=str(e))
        return {"session": name, "analyzed": n_done, "total": n_total,
                "processed": False}

    complete = (n_done == n_total)
    write_state(session_dir,
                processed=complete,
                partial=not complete,
                report_path=report,
                last_error=None,
                processed_at=dt.datetime.now().isoformat(timespec="seconds"))
    if complete:
        log(f"{name}: complete ({n_total} segment(s)).")
    else:
        log(f"{name}: partial — {n_done}/{n_total} segment(s); "
            f"{n_total - n_done} still pending, click Process again when the "
            "network is back.")
    return {"session": name, "analyzed": n_done, "total": n_total,
            "processed": complete, "report": report, "summary": summary}


def process_all_pending(rec_dir, ffmpeg, log=print, should_stop=None,
                        progress=None):
    """Process every session that still needs it, oldest first. The core of the
    Process button. Returns an aggregate summary dict."""
    try:
        import gemini
        if not gemini.available():
            log("Gemini isn't available — run \"Use Gemini (free).bat\" and set "
                "GEMINI_API_KEY, then click Process again. Nothing was changed.")
            return {"sessions": 0, "completed": 0, "available": False}
    except Exception as e:
        log(f"Gemini backend unavailable ({e}); nothing was changed.")
        return {"sessions": 0, "completed": 0, "available": False}

    pend = pending_sessions(rec_dir)
    if not pend:
        log("Nothing to process — all recordings are already up to date.")
        return {"sessions": 0, "completed": 0, "available": True}

    log(f"Processing {len(pend)} recording(s)…")
    results, completed = [], 0
    for sd in pend:
        if should_stop and should_stop():
            log("Stopped — remaining recordings are saved and will resume next time.")
            break
        log(f"— {os.path.basename(sd)} —")
        r = process_session(sd, ffmpeg, rec_dir, log=log,
                            should_stop=should_stop, progress=progress)
        results.append(r)
        if r.get("processed"):
            completed += 1
        if r.get("stopped"):
            break

    log(f"Done. {completed}/{len(pend)} recording(s) fully processed"
        + ("; some have segments still pending (retry when online)."
           if completed < len(pend) else "."))
    return {"sessions": len(pend), "completed": completed, "available": True,
            "results": results}
