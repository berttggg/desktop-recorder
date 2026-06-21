"""Summary-first analysis + reports.

A session is a folder of hourly segment files (seg_000.mp4 ...). We:
  map    -> split the timeline into ~BLOCK_MINUTES blocks; caption each block
            (a few frames + its transcript) with a fast model.
  reduce -> synthesize all block captions into daily insights with a strong
            model: summary, accomplishments (done), action items (to-do), topics.
Then we render a visual session report, store everything in the knowledge
base, and (re)build the cross-day dashboard.
"""

import os
import glob
import html
import json
import shutil
import hashlib
import datetime as dt

import analyze
import kb

BLOCK_MINUTES = int(os.environ.get("RECORDER_BLOCK_MINUTES", "10"))
BLOCK_SECONDS = BLOCK_MINUTES * 60
FRAMES_PER_BLOCK = 3
THUMB_SCALE = 480

PALETTE = ["#5b9dff", "#57c785", "#f5a623", "#c879ff", "#ff6b6b",
           "#36c5d0", "#e8b339", "#8a8fff", "#6ad4a0", "#ff9f7f"]

MAP_PROMPT = """The image(s) are {n} frames spanning {t0}-{t1} of a screen
recording, plus (maybe) the audio transcript for that span. In ONE JSON object
(no prose) describe what the user was doing:
{{"activity":"<=5 word label of the main task",
  "app":"main app or website",
  "detail":"one sentence on what happened",
  "todos":["a task the user explicitly said they still need to do (omit if none)"]}}
Only state what the frames/transcript support."""

REDUCE_PROMPT = """Below is a chronological list of activity blocks from a
user's day at their computer (time | activity | app | detail | todos).

Synthesize it into ONE JSON object (no prose):
{{"summary":"3-5 sentences: what the day was mostly about and what got done",
  "accomplishments":["concrete things the user completed or made progress on"],
  "action_items":["things the user still needs to do / follow up on"],
  "topics":["canonical project or topic names, 2-6 of them"]}}
Merge duplicates. Be specific and refer to real apps/files/topics seen.

Activity blocks:
{blocks}"""


def _norm(label):
    return " ".join((label or "other").lower().split())


# --------------------------------------------------------------------------
# Map / reduce
# --------------------------------------------------------------------------
def _collect(session_dir, ffmpeg, log):
    """Return (segments_meta, all_frames, all_transcript, total_dur, tmp_dirs)."""
    segs = sorted(glob.glob(os.path.join(session_dir, "seg_*.mp4")))
    if not segs:
        segs = sorted(glob.glob(os.path.join(session_dir, "*.mp4")))
    log(f"{len(segs)} segment(s) in session.")

    base_fps = max(FRAMES_PER_BLOCK / BLOCK_SECONDS, 1.0 / 600)
    base = 0.0
    frames, transcript, tmp_dirs = [], [], []
    for seg in segs:
        dur = analyze.get_duration(seg, ffmpeg)
        # base_fps is tuned for hour-long segments (~FRAMES_PER_BLOCK per block).
        # For short segments it would sample nothing, so floor at ~2 frames per
        # segment; for long segments base_fps dominates and density is unchanged.
        seg_fps = max(base_fps, 2.0 / dur) if dur > 0 else base_fps
        outdir = os.path.join(session_dir, "_frames_" + os.path.basename(seg))
        fr = analyze.extract_frames(seg, ffmpeg, fps=seg_fps, scale=THUMB_SCALE,
                                    outdir=outdir, log=log)
        tmp_dirs.append(outdir)
        frames += [(p, base + t) for (p, t) in fr]
        tr = analyze.transcribe_audio(seg, ffmpeg, log=log)
        transcript += [(base + s, base + e, txt) for (s, e, txt) in tr]
        base += dur
        log(f"  {os.path.basename(seg)}: {len(fr)} frames, {len(tr)} transcript segs, {dur:.0f}s")
    return frames, transcript, base, tmp_dirs


def _map_blocks(frames, transcript, total_dur, log, use_ai=None):
    blocks = []
    n_blocks = max(1, int((total_dur + BLOCK_SECONDS - 1) // BLOCK_SECONDS))
    key = analyze.have_key() if use_ai is None else use_ai
    for b in range(n_blocks):
        t0, t1 = b * BLOCK_SECONDS, min(total_dur, (b + 1) * BLOCK_SECONDS)
        bf = [(p, t) for (p, t) in frames if t0 <= t < t1] or \
             [(p, t) for (p, t) in frames if t0 <= t <= t1]
        bt = [(s, e, x) for (s, e, x) in transcript if t0 <= s < t1]
        if not bf and not bt:
            continue
        thumb = bf[len(bf) // 2][0] if bf else None
        block = {"t0": t0, "t1": t1, "thumb": thumb,
                 "activity": "Activity", "app": "", "detail": "", "todos": []}
        if key and bf:
            sel = bf[:: max(1, len(bf) // FRAMES_PER_BLOCK)][:FRAMES_PER_BLOCK]
            content = [{"type": "text", "text": f"Frames {analyze.fmt_clock(t0)}-{analyze.fmt_clock(t1)}:"}]
            content += [analyze.img_block(p) for (p, _t) in sel]
            if bt:
                tr = " ".join(x for (_s, _e, x) in bt)[:1500]
                content.append({"type": "text", "text": "Transcript: " + tr})
            content.append({"type": "text", "text": MAP_PROMPT.format(
                n=len(sel), t0=analyze.fmt_clock(t0), t1=analyze.fmt_clock(t1))})
            try:
                data = analyze.call_json(analyze.MAP_MODEL, content, max_tokens=400, log=log)
                if isinstance(data, dict):
                    block.update({
                        "activity": str(data.get("activity") or "Activity"),
                        "app": str(data.get("app") or ""),
                        "detail": str(data.get("detail") or ""),
                        "todos": [str(t) for t in (data.get("todos") or [])],
                    })
            except Exception as e:
                log(f"  block {b} map error: {e}")
        elif bt:
            block["detail"] = " ".join(x for (_s, _e, x) in bt)[:200]
        blocks.append(block)
        log(f"  block {analyze.fmt_clock(t0)}: {block['activity']}")
    return blocks


def _local_todos(transcript):
    cues = ("need to", "have to", "i should", "todo", "to do", "remember to",
            "follow up", "must ", "let's ", "i'll ", "i will ")
    out = []
    for _s, _e, x in transcript:
        low = x.lower()
        if any(c in low for c in cues) and len(x) > 8:
            out.append(x.strip())
    return out[:20]


def _reduce(blocks, transcript, total_dur, log, use_ai=None):
    use_ai = analyze.have_key() if use_ai is None else use_ai
    if use_ai and blocks:
        lines = []
        for blk in blocks:
            todos = "; ".join(blk["todos"]) if blk["todos"] else "-"
            lines.append(f"{analyze.fmt_clock(blk['t0'])}-{analyze.fmt_clock(blk['t1'])} | "
                         f"{blk['activity']} | {blk['app']} | {blk['detail']} | {todos}")
        prompt = REDUCE_PROMPT.format(blocks="\n".join(lines)[:15000])
        try:
            data = analyze.call_json(analyze.REDUCE_MODEL,
                                     [{"type": "text", "text": prompt}],
                                     max_tokens=1500, log=log)
            if isinstance(data, dict):
                # Fold in every per-block to-do, then tidy + fuzzy-de-duplicate
                # the whole set so reworded variants ("Meet Jay" / "Meet with
                # Jay") and cue-prefixed dupes collapse to one clean item.
                merged = list(data.get("action_items") or [])
                for blk in blocks:
                    merged += blk.get("todos") or []
                data["action_items"] = analyze.clean_todos(merged)
                return data
        except Exception as e:
            log(f"reduce error: {e}")

    # local fallback (no key)
    return {
        "summary": f"Recorded {analyze.fmt_clock(total_dur)} of activity across "
                   f"{len(blocks)} blocks. Set ANTHROPIC_API_KEY for an AI summary.",
        "accomplishments": [],
        "action_items": analyze.clean_todos(_local_todos(transcript)),
        "topics": [],
    }


def _time_breakdown(blocks):
    mins = {}
    disp = {}
    for blk in blocks:
        k = _norm(blk["activity"])
        disp.setdefault(k, blk["activity"])
        mins[k] = mins.get(k, 0.0) + (blk["t1"] - blk["t0"]) / 60.0
    items = sorted(([disp[k], m] for k, m in mins.items()), key=lambda x: -x[1])
    return [{"activity": a, "minutes": round(m, 1)} for a, m in items]


def _runs(blocks):
    """Merge consecutive same-activity blocks into timeline runs."""
    runs = []
    for blk in blocks:
        if runs and _norm(runs[-1]["activity"]) == _norm(blk["activity"]):
            runs[-1]["t1"] = blk["t1"]
            if not runs[-1]["detail"]:
                runs[-1]["detail"] = blk["detail"]
        else:
            runs.append(dict(blk))
    return runs


# --------------------------------------------------------------------------
# Embedding index (semantic search)
# --------------------------------------------------------------------------
def _session_documents(session_id, data):
    """Yield (kind, text) units to embed for one session: one combined
    'session' document (summary + accomplishments + topics) plus one per to-do."""
    summary = (data.get("summary") or "").strip()
    acc = [str(a) for a in (data.get("accomplishments") or [])]
    topics = [str(t) for t in (data.get("topics") or [])]
    ents = [str(e) for e in (data.get("entities") or [])]
    parts = [summary] + acc
    if topics:
        parts.append("Topics: " + ", ".join(topics))
    if ents:
        parts.append("Mentioned: " + ", ".join(ents))
    doc = "\n".join(p for p in parts if p).strip()
    if doc:
        yield ("session", doc)
    for t in (data.get("action_items") or []):
        t = str(t).strip()
        if t:
            yield ("todo", t)


def _doc_hash(units, sig=""):
    """Stable hash of the (kind, text) units, so we only re-embed on change.

    ``sig`` is the active embedder's signature (backend:model:dim). Folding it in
    means switching the embedding backend/model invalidates every session's hash,
    so the next index/reindex re-embeds them under the new model automatically."""
    h = hashlib.sha1()
    if sig:
        h.update(("sig:" + sig).encode("utf-8"))
        h.update(b"\x02")
    for kind, text in units:
        h.update(kind.encode("utf-8"))
        h.update(b"\x00")
        h.update(text.encode("utf-8"))
        h.update(b"\x01")
    return h.hexdigest()


def _transcript_units(lines, max_chars=1000, cap=80):
    """Chunk transcript lines (strings) into ~max_chars search units so spoken
    content is findable, not just the summary. Capped so a very long day can't
    explode the embedding count. Returns [("transcript", chunk), ...]."""
    units, buf, n = [], [], 0
    for ln in (lines or []):
        ln = (str(ln) or "").strip()
        if not ln:
            continue
        buf.append(ln)
        n += len(ln) + 1
        if n >= max_chars:
            units.append(("transcript", " ".join(buf)))
            buf, n = [], 0
            if len(units) >= cap:
                return units
    if buf and len(units) < cap:
        units.append(("transcript", " ".join(buf)))
    return units


def _index_embeddings(db, session_id, data, transcript=None, log=print):
    """Embed a session's documents (summary, to-dos, AND the verbatim transcript)
    and store them for semantic search. Skips work if the content is unchanged."""
    try:
        import embed
    except Exception:
        return
    if not embed.available():
        log(f"Embeddings: {embed.label()} backend unavailable — skipping semantic index.")
        return
    units = list(_session_documents(session_id, data))
    if transcript:
        lines = [f"[{analyze.fmt_clock(s)}] {x}" for s, _e, x in transcript if x]
        units += _transcript_units(lines)
    if not units:
        return
    sig = ""
    try:
        sig = embed.signature()
    except Exception:
        sig = ""
    h = _doc_hash(units, sig)
    if kb.embed_hashes(db).get(session_id) == h:
        log("Embeddings: content unchanged — reusing existing index.")
        return
    try:
        vecs = embed.embed_documents([t for _k, t in units])
        kb.save_embeddings(db, session_id,
                           [(k, t, v) for (k, t), v in zip(units, vecs)], doc_hash=h)
        log(f"Embeddings: indexed {len(units)} unit(s) for semantic search.")
    except Exception as e:
        log(f"Embeddings: indexing failed ({e}).")


def index_text_unit(rec_dir, pseudo_id, kind, text, log=print):
    """Embed one standalone text — the Overview / weekly / monthly roll-up — as a
    searchable KB unit under a synthetic session id, so it surfaces in search.
    Hash-gated so it only re-embeds when the text actually changes."""
    text = (text or "").strip()
    if not text:
        return
    try:
        import embed
        if not embed.available():
            return
    except Exception:
        return
    db = kb.db_path(rec_dir)
    units = [(kind, text)]
    try:
        sig = embed.signature()
    except Exception:
        sig = ""
    h = _doc_hash(units, sig)
    if kb.embed_hashes(db).get(pseudo_id) == h:
        return
    try:
        vecs = embed.embed_documents([text])
        kb.save_embeddings(db, pseudo_id, [(kind, text, vecs[0])], doc_hash=h)
    except Exception as e:
        log(f"Indexing {kind} roll-up failed ({e}).")


def reindex(rec_dir, force=False, log=print):
    """Backfill embeddings for every session in the knowledge base — no video
    re-analysis needed. Unchanged sessions are skipped unless ``force`` is set
    (e.g. after an embedding-model upgrade). Returns embedding units written."""
    try:
        import embed
    except Exception:
        embed = None
    if embed is None or not embed.available():
        if embed is None:
            log("Embeddings unavailable — install fastembed first.")
        else:
            log(f"Embeddings unavailable — {embed.label()} backend can't run "
                "(check the API key for the Gemini backend, or install fastembed).")
        return 0
    db = kb.db_path(rec_dir)
    payloads = kb.session_payloads(db)
    have = {} if force else kb.embed_hashes(db)
    try:
        sig = embed.signature()
    except Exception:
        sig = ""
    total = skipped = 0
    for p in payloads:
        units = list(_session_documents(p["id"], p))
        rp = p.get("report_path") or ""
        tdir = os.path.dirname(rp) if rp else os.path.join(rec_dir, p["id"])
        tpath = os.path.join(tdir, "transcript.txt")
        try:
            if os.path.isfile(tpath):
                with open(tpath, encoding="utf-8") as f:
                    units += _transcript_units(f.read().splitlines())
        except Exception:
            pass
        if not units:
            continue
        h = _doc_hash(units, sig)
        if have.get(p["id"]) == h:
            skipped += 1
            continue
        vecs = embed.embed_documents([t for _k, t in units])
        kb.save_embeddings(db, p["id"],
                           [(k, t, v) for (k, t), v in zip(units, vecs)], doc_hash=h)
        total += len(units)
    log(f"Reindexed {len(payloads)} session(s) → {total} new unit(s) via {embed.label()}"
        + (f" ({skipped} unchanged, skipped)." if skipped else "."))
    return total


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def _gemini_analyze(session_dir, ffmpeg, log):
    """Run the Gemini native-video backend, guarding import/availability/errors.
    Returns (blocks, transcript, total_dur, tmp_dirs); empty blocks signals the
    caller to fall back to the Claude/local pipeline."""
    try:
        import gemini
    except Exception as e:
        log(f"Gemini backend requested but SDK missing ({e}); install with: pip install google-genai")
        return [], [], 0.0, []
    if not gemini.available():
        log("Gemini backend requested but no GEMINI_API_KEY / google-genai — falling back.")
        return [], [], 0.0, []
    log(f"Analyzing with Gemini native video ({gemini.MODEL})…")
    try:
        return gemini.analyze_video(session_dir, ffmpeg, log=log)
    except Exception as e:
        log(f"Gemini analysis failed ({e}); falling back.")
        return [], [], 0.0, []


def analyze_session(session_dir, ffmpeg, rec_dir, meta=None, log=print):
    meta = meta or {}
    session_id = os.path.basename(session_dir.rstrip("\\/"))
    log(f"Analyzing session {session_id}…")

    backend = os.environ.get("ANALYSIS_BACKEND", "claude").strip().lower()
    blocks = transcript = None
    tmp_dirs = []

    if backend == "gemini":
        blocks, transcript, total_dur, tmp_dirs = _gemini_analyze(session_dir, ffmpeg, log)
        if blocks:
            log("Synthesizing daily insights (Gemini)…")
            import gemini
            insights = gemini.reduce(blocks, log=log)
        else:
            # Gemini unavailable/failed/empty — clean up and fall back below.
            for d in tmp_dirs:
                shutil.rmtree(d, ignore_errors=True)
            tmp_dirs = []
            blocks = None
            log("Gemini produced nothing — falling back to the Claude/local pipeline.")

    if blocks is None:
        use_ai = (backend != "local") and analyze.have_key()
        frames, transcript, total_dur, tmp_dirs = _collect(session_dir, ffmpeg, log)
        log("Captioning activity blocks…")
        blocks = _map_blocks(frames, transcript, total_dur, log, use_ai=use_ai)
        log("Synthesizing daily insights…")
        insights = _reduce(blocks, transcript, total_dur, log, use_ai=use_ai)

    return _finalize_and_report(session_dir, session_id, rec_dir, blocks,
                                transcript, total_dur, insights, meta, tmp_dirs, log)


def _day_research(session_dir, insights, log=print):
    """Web-grounded enrichment for one day: background on the day's topics + a
    resource/how-to for each open to-do, with sources. Stored at
    insights['research'] = {text, sources, sig}. Gated by gemini.research_enabled;
    hash-cached on summary+topics+to-dos so re-processing a day doesn't re-spend
    search quota. Best-effort — any failure just leaves no research."""
    try:
        import gemini
        if not gemini.research_enabled() or not gemini.available():
            return
    except Exception:
        return
    summary = (insights.get("summary") or "").strip()
    todos = [str(t) for t in (insights.get("action_items") or [])]
    topics = [str(t) for t in (insights.get("topics") or [])]
    entities = [str(e) for e in (insights.get("entities") or [])]
    if not summary and not todos and not entities:
        return
    sig = hashlib.sha1(json.dumps([summary, topics, todos, entities],
                       ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    try:    # reuse prior research when the inputs are unchanged (no re-spend)
        with open(os.path.join(session_dir, "insights.json"), encoding="utf-8") as f:
            prev = json.load(f).get("research")
        if isinstance(prev, dict) and prev.get("sig") == sig and prev.get("text"):
            insights["research"] = prev
            return
    except Exception:
        pass
    ctx = []
    if summary:
        ctx.append("Summary: " + summary)
    if topics:
        ctx.append("Topics: " + ", ".join(topics))
    if entities:
        ctx.append("Notable entities seen today (people / accounts / companies / "
                   "products / tickers):\n" + "\n".join("- " + e for e in entities[:20]))
    if todos:
        ctx.append("Open to-dos:\n" + "\n".join("- " + t for t in todos))
    log("Researching the day on the web (Google Search grounding)…")
    text, sources = gemini.research(
        gemini.DAY_RESEARCH_PROMPT.format(context="\n".join(ctx)), log=log)
    if text:
        insights["research"] = {"text": text, "sources": sources, "sig": sig}
        log(f"Web research added ({len(sources)} source(s)).")


def _finalize_and_report(session_dir, session_id, rec_dir, blocks, transcript,
                         total_dur, insights, meta, tmp_dirs, log):
    """Shared back half of analysis: time breakdown, write transcript/insights
    files, render the report, persist to the KB, index embeddings, rebuild the
    dashboard, and clean up temp dirs. Returns (report_path, summary)."""
    time_breakdown = _time_breakdown(blocks)
    insights["time_breakdown"] = time_breakdown
    runs = _runs(blocks)

    # Notable on-screen entities gathered across the day (deduped, capped).
    ents, seen = [], set()
    for b in blocks:
        for e in (b.get("entities") or []):
            e = str(e).strip()
            k = e.lower()
            if e and k not in seen:
                seen.add(k)
                ents.append(e)
    if ents:
        insights["entities"] = ents[:25]

    # Web-grounded enrichment (entities + background + to-do resources), before we persist.
    _day_research(session_dir, insights, log)

    # transcript + insights files — written atomically (temp + os.replace) so a
    # shutdown mid-write can never corrupt the previous good copy.
    if transcript:
        analyze.atomic_write_text(
            os.path.join(session_dir, "transcript.txt"),
            "\n".join(f"[{analyze.fmt_clock(s)}-{analyze.fmt_clock(e)}] {x}"
                      for s, e, x in transcript))
    analyze.atomic_write_json(os.path.join(session_dir, "insights.json"), insights)

    report = _render_session_report(session_dir, session_id, insights, runs,
                                    time_breakdown, transcript, total_dur, meta)

    # persist to knowledge base
    meta = dict(meta)
    meta.setdefault("date", session_id.split("_")[1] if "_" in session_id else dt.date.today().isoformat())
    if len(meta["date"]) == 8 and meta["date"].isdigit():
        meta["date"] = f"{meta['date'][:4]}-{meta['date'][4:6]}-{meta['date'][6:]}"
    meta["duration_sec"] = total_dur
    meta["report_path"] = report
    kb.save_session(kb.db_path(rec_dir), session_id, meta, insights)
    log("Saved to knowledge base.")
    _index_embeddings(kb.db_path(rec_dir), session_id, insights,
                      transcript=transcript, log=log)

    build_dashboard(rec_dir, log=log)

    for d in (tmp_dirs or []):
        shutil.rmtree(d, ignore_errors=True)

    return report, insights.get("summary", "")


# --------------------------------------------------------------------------
# Finalize from per-segment blocks (used by the resumable batch processor)
# --------------------------------------------------------------------------
def finalize_from_blocks(session_dir, rec_dir, blocks, transcript, total_dur,
                         meta=None, tmp_dirs=None, log=print):
    """Synthesize + publish a session report from already-extracted blocks.

    ``blocks``/``transcript`` are the assembled per-segment results, already in
    session-absolute time. Only the daily *reduce* (synthesis) runs here, plus
    the report/KB/embeddings/dashboard steps — no video is re-read. All file
    writes are atomic (see _finalize_and_report), so a previous good report
    survives any interruption. Returns (report_path, summary)."""
    meta = meta or {}
    session_id = os.path.basename(session_dir.rstrip("\\/"))
    log(f"Synthesizing {session_id} ({len(blocks)} block(s))…")
    try:
        import gemini
        insights = gemini.reduce(blocks, log=log)
    except Exception as e:
        log(f"Gemini synthesis unavailable ({e}); using local synthesis.")
        insights = _reduce(blocks, transcript, total_dur, log, use_ai=False)
    return _finalize_and_report(session_dir, session_id, rec_dir, blocks,
                                transcript, total_dur, insights, meta, tmp_dirs, log)


# --------------------------------------------------------------------------
# HTML rendering
# --------------------------------------------------------------------------
CSS = """
:root{--bg:#0f1115;--card:#181b22;--line:#262b36;--txt:#e6e8ee;--mut:#9aa3b2;--accent:#5b9dff;--good:#57c785;--warn:#f5a623}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.55 -apple-system,Segoe UI,Roboto,sans-serif}
a{color:var(--accent);text-decoration:none}
.wrap{max-width:1040px;margin:0 auto;padding:28px 20px 70px}
h1{font-size:24px;margin:0 0 2px}
.meta{color:var(--mut);font-size:13px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin-bottom:16px}
.card h2{margin:0 0 10px;font-size:13px;letter-spacing:.05em;text-transform:uppercase;color:var(--mut)}
.lead{font-size:16px;line-height:1.6}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:760px){.cols{grid-template-columns:1fr}}
ul.clean{margin:0;padding:0;list-style:none}
ul.clean li{padding:7px 0 7px 26px;position:relative;border-bottom:1px solid var(--line)}
ul.clean li:last-child{border-bottom:0}
ul.done li:before{content:"✓";position:absolute;left:0;color:var(--good);font-weight:700}
ul.todo li:before{content:"○";position:absolute;left:2px;color:var(--warn)}
.bar{display:flex;height:26px;border-radius:7px;overflow:hidden;margin:4px 0 12px}
.bar span{display:block}
.legend{display:flex;flex-wrap:wrap;gap:8px 16px;font-size:13px;color:var(--mut)}
.legend i{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:6px;vertical-align:middle}
.run{display:flex;gap:14px;padding:12px 0;border-bottom:1px solid var(--line)}
.run:last-child{border-bottom:0}
.run img{width:200px;max-width:34vw;border-radius:8px;border:1px solid var(--line)}
.run .t{font:12px/1 ui-monospace,monospace;color:#0b0d11;background:var(--accent);padding:4px 7px;border-radius:6px;white-space:nowrap}
.run h3{margin:0 0 4px;font-size:16px}
.run p{margin:3px 0;color:var(--mut)}
.pill{display:inline-block;background:#0d0f14;border:1px solid var(--line);color:var(--mut);border-radius:20px;padding:3px 10px;margin:2px 4px 2px 0;font-size:13px}
details{margin-top:8px}summary{cursor:pointer;color:var(--mut)}
pre{white-space:pre-wrap;color:var(--mut);font:12.5px/1.5 ui-monospace,monospace}
.empty{color:var(--mut);font-style:italic}
.searchbar{position:sticky;top:0;z-index:5;margin-bottom:12px;padding:12px 0 8px;background:linear-gradient(var(--bg) 72%,rgba(15,17,21,0))}
input.search{width:100%;background:#0d0f14;border:1px solid var(--line);color:var(--txt);border-radius:10px;padding:11px 14px;font-size:15px;outline:none;transition:border-color .15s,box-shadow .15s}
input.search:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(91,157,255,.18)}
input.search::placeholder{color:var(--mut)}
#qmode{margin:7px 2px 0;min-height:15px}
.run.day{transition:opacity .15s ease;border-radius:8px;padding-left:10px;padding-right:10px;margin:0 -10px}
.run.day:hover{background:rgba(255,255,255,.025)}
.pill.score{background:var(--accent);color:#0b0d11;border-color:transparent;font-weight:600}
#noresults{padding:14px 2px}
.kpi{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:6px}
.kpi .b{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 16px;min-width:120px}
.kpi .n{font-size:24px;font-weight:700}.kpi .l{color:var(--mut);font-size:12px}
"""

# Dashboard search behavior. When the page is *served* (serve.py), the box does
# semantic search: it queries /search, then reorders/filters the day cards by
# cosine score. Opened as a plain file:// page (no server) it falls back to the
# original substring filter so the dashboard still works on its own.
DASH_JS = r"""
(function(){
  var q=document.getElementById('q');
  if(!q) return;
  var days=document.getElementById('days');
  var mode=document.getElementById('qmode');
  var none=document.getElementById('noresults');
  var matchwrap=document.getElementById('matchwrap');
  var matchbox=document.getElementById('matches');
  var served=(location.protocol==='http:'||location.protocol==='https:');
  var SEMANTIC='Semantic search · ranked by meaning';
  var TEXT='Text filter · matches words';
  var ABS_MIN=0.20, REL=0.62;   // a day must clear this cosine gate to show
  function setMode(t){ if(mode) mode.textContent=t; }
  setMode(served?SEMANTIC:TEXT);

  function cards(){ return days ? [].slice.call(days.querySelectorAll('.day')) : []; }
  function showNone(on){ if(none) none.style.display=on?'block':'none'; }
  function clearBadges(){
    cards().forEach(function(e){
      var b=e.querySelector('.score'); if(b) b.parentNode.removeChild(b);
    });
  }
  function addBadge(card,score){
    var h=card.querySelector('h3'); if(!h) return;
    var b=document.createElement('span');
    b.className='pill score';
    b.textContent=Math.round(score*100)+'% match';
    h.insertBefore(b,h.firstChild);
  }
  function esc(s){return (s==null?'':String(s)).replace(/[&<>"]/g,function(c){
    return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[c];});}
  function clearMatches(){ if(matchbox) matchbox.innerHTML=''; if(matchwrap) matchwrap.style.display='none'; }
  function renderMatches(results){
    if(!matchwrap||!matchbox) return;
    try{
      var rows=(results||[]).slice(0,8);
      if(!rows.length){ clearMatches(); return; }
      var html='';
      rows.forEach(function(it){
        var d=it.date||'', rp=it.report_path||'';
        var link=rp?("<a href='"+esc(rp)+"'>"+esc(d||'open report')+"</a>")
                   :(d?("<span class='pill'>"+esc(d)+"</span>"):'');
        var t=it.text||''; if(t.length>240){ t=t.slice(0,240)+'…'; }
        html+="<div class='run day' style='display:block'>"
             +"<h3 style='margin:4px 0'><span class='pill score'>"+Math.round((it.score||0)*100)
             +"%</span> <span class='pill'>"+esc(it.kind||'')+"</span> "+link+"</h3>"
             +"<p style='color:var(--txt);margin:2px 0'>"+esc(t)+"</p></div>";
      });
      matchbox.innerHTML=html;
      matchwrap.style.display='block';
    }catch(e){ clearMatches(); }
  }
  function filterF(v){
    var els=document.querySelectorAll('.f');
    for(var i=0;i<els.length;i++)
      els[i].style.display=els[i].innerText.toLowerCase().indexOf(v)>=0?'':'none';
  }
  function resetDays(){
    clearBadges();
    var a=cards();
    a.sort(function(x,y){return (+x.dataset.i)-(+y.dataset.i);});
    a.forEach(function(e){days.appendChild(e); e.style.display='block';});
    showNone(false);
  }
  function substrDays(v){
    clearBadges();
    var shown=0;
    cards().forEach(function(e){
      var hit=e.innerText.toLowerCase().indexOf(v)>=0;
      e.style.display=hit?'block':'none'; if(hit) shown++;
    });
    showNone(shown===0);
  }
  function rankDays(results,v){
    clearBadges();
    var best={};
    (results||[]).forEach(function(it){
      var s=it.session_id; if(!s) return;
      if(best[s]==null || it.score>best[s]) best[s]=it.score;
    });
    var top=0,k; for(k in best) if(best[k]>top) top=best[k];
    var gate=Math.max(ABS_MIN, top*REL);
    var a=cards(), shown=0;
    a.sort(function(x,y){
      var sx=best[x.dataset.sid], sy=best[y.dataset.sid];
      return (sy==null?-2:sy)-(sx==null?-2:sx);
    });
    a.forEach(function(e){
      var sc=best[e.dataset.sid], ok=(sc!=null && sc>=gate);
      days.appendChild(e);
      e.style.display=ok?'block':'none';
      if(ok){ addBadge(e,sc); shown++; }
    });
    if(shown===0 && v){ substrDays(v); }   // nothing cleared the gate → text fallback
    else { showNone(shown===0); }
  }

  var timer, seq=0;
  function run(){
    var raw=q.value.trim(), v=raw.toLowerCase();
    if(!served){ clearMatches(); filterF(v); if(!raw){resetDays();} else {substrDays(v);} return; }
    clearTimeout(timer);
    if(!raw){ setMode(SEMANTIC); clearMatches(); resetDays(); return; }
    setMode('Searching…');
    var my=++seq;
    timer=setTimeout(function(){
      fetch('/search?q='+encodeURIComponent(raw)+'&k=50')
        .then(function(r){return r.json();})
        .then(function(d){
          if(my!==seq) return;                 // a newer keystroke superseded us
          if(d.error){ setMode(TEXT+' (offline)'); clearMatches(); substrDays(v); return; }
          setMode(SEMANTIC);
          renderMatches(d.results);
          rankDays(d.results,v);
        })
        .catch(function(){ if(my===seq){ setMode(TEXT+' (offline)'); clearMatches(); substrDays(v); } });
    },160);
  }
  q.addEventListener('input',run);

  document.addEventListener('keydown',function(e){
    if(e.key==='/' && document.activeElement!==q){ e.preventDefault(); q.focus(); q.select(); }
    else if(e.key==='Escape' && document.activeElement===q){ q.value=''; run(); q.blur(); }
  });
})();
"""


def _bar(time_breakdown, esc):
    total = sum(t["minutes"] for t in time_breakdown) or 1
    bar, leg = [], []
    for i, t in enumerate(time_breakdown[:10]):
        col = PALETTE[i % len(PALETTE)]
        pct = 100 * t["minutes"] / total
        bar.append(f"<span style='width:{pct:.1f}%;background:{col}' title=\"{esc(t['activity'])}\"></span>")
        leg.append(f"<span><i style='background:{col}'></i>{esc(t['activity'])} "
                   f"({t['minutes']:.0f}m)</span>")
    return "<div class='bar'>" + "".join(bar) + "</div><div class='legend'>" + "".join(leg) + "</div>"


def _render_session_report(session_dir, session_id, insights, runs, time_breakdown,
                           transcript, total_dur, meta):
    esc = html.escape
    p = [f"<!doctype html><html><head><meta charset='utf-8'><title>Day report {esc(session_id)}</title>"
         f"<style>{CSS}</style></head><body><div class='wrap'>"]
    date = meta.get("date", session_id)
    p.append(f"<h1>Day report</h1><div class='meta'>{esc(date)} &nbsp;·&nbsp; "
             f"{analyze.fmt_clock(total_dur)} recorded &nbsp;·&nbsp; "
             f"<a href='knowledge_base.db' style='display:none'></a>"
             f"<a href='../dashboard.html'>← Knowledge base</a></div>")

    p.append("<div class='card'><h2>What the day was about</h2>"
             f"<div class='lead'>{esc(insights.get('summary',''))}</div></div>")

    acc = insights.get("accomplishments") or []
    todo = insights.get("action_items") or []
    p.append("<div class='cols'>")
    p.append("<div class='card'><h2>Done</h2>" + (
        "<ul class='clean done'>" + "".join(f"<li>{esc(str(a))}</li>" for a in acc) + "</ul>"
        if acc else "<p class='empty'>Nothing flagged as completed.</p>") + "</div>")
    p.append("<div class='card'><h2>To do / follow up</h2>" + (
        "<ul class='clean todo'>" + "".join(f"<li>{esc(str(a))}</li>" for a in todo) + "</ul>"
        if todo else "<p class='empty'>No open items detected.</p>") + "</div>")
    p.append("</div>")

    ents = insights.get("entities") or []
    if ents:
        pills = "".join(f"<span class='pill'>{esc(str(e))}</span>" for e in ents)
        p.append("<div class='card'><h2>People &amp; things noticed</h2>" + pills + "</div>")

    research = insights.get("research") or {}
    rtext = (research.get("text") or "").strip()
    if rtext:
        paras = "".join(f"<p>{esc(ln)}</p>" for ln in rtext.split("\n") if ln.strip())
        srcs = [s for s in (research.get("sources") or []) if s.get("uri")]
        slist = ""
        if srcs:
            items = "".join(
                f"<li><a href='{esc(s['uri'])}'>{esc(s.get('title') or s['uri'])}</a></li>"
                for s in srcs)
            slist = (f"<details><summary>Sources ({len(srcs)})</summary>"
                     f"<ul class='clean'>{items}</ul></details>")
        p.append("<div class='card'><h2>Research &amp; resources (web)</h2>"
                 f"<div class='lead'>{paras}</div>{slist}</div>")

    if time_breakdown:
        p.append("<div class='card'><h2>Where the time went</h2>" + _bar(time_breakdown, esc) + "</div>")

    topics = insights.get("topics") or []
    if topics:
        p.append("<div class='card'><h2>Topics</h2>"
                 + "".join(f"<span class='pill'>{esc(str(t))}</span>" for t in topics) + "</div>")

    if runs:
        p.append("<div class='card'><h2>Timeline</h2>")
        for r in runs:
            thumb = (f"<img src='{analyze.img_data_uri(r['thumb'])}'>" if r.get("thumb") else "")
            p.append("<div class='run'>" + thumb + "<div><div class='t'>"
                     f"{analyze.fmt_clock(r['t0'])} – {analyze.fmt_clock(r['t1'])}</div>"
                     f"<h3>{esc(r['activity'])}{(' · ' + esc(r['app'])) if r.get('app') else ''}</h3>"
                     f"<p>{esc(r.get('detail',''))}</p></div></div>")
        p.append("</div>")

    if transcript:
        full = "\n".join(f"[{analyze.fmt_clock(s)}] {x}" for s, e, x in transcript)
        p.append("<div class='card'><details><summary>Full transcript</summary><pre>"
                 + esc(full) + "</pre></details></div>")

    p.append("</div></body></html>")
    out = os.path.join(session_dir, "report.html")
    analyze.atomic_write_text(out, "".join(p))
    return out


def _dedup_todo_rows(rows):
    """Collapse the same task repeated across sessions/days in the dashboard's
    open-to-do list ("Meet Jay" carried over every day until it's done). Rows
    arrive newest-first, so the surviving row keeps the most recent date + link."""
    kept = []
    for r in rows:
        txt = (r.get("text") or "").strip()
        if not txt:
            continue
        if any(analyze.todos_similar(txt, k.get("text") or "") for k in kept):
            continue
        kept.append(r)
    return kept


def build_kb_overview(rec_dir, force=False, log=print):
    """Generate + cache a model-written overview of recent work for the dashboard.

    Cached in <rec_dir>/kb_overview.json keyed by a hash of the inputs (recent day
    summaries + open to-dos), so the model is called only when those actually
    change. Returns the overview text ('' if unavailable / no key)."""
    path = kb.db_path(rec_dir)
    sessions = kb.recent_sessions(path, limit=20)
    todos = _dedup_todo_rows(kb.open_action_items(path))
    cache_path = os.path.join(rec_dir, "kb_overview.json")

    sig_src = json.dumps(
        [(s.get("date"), s.get("id"), s.get("summary")) for s in sessions]
        + [t.get("text") for t in todos],
        ensure_ascii=False, sort_keys=True)
    sig = hashlib.sha1(sig_src.encode("utf-8")).hexdigest()

    if not force:
        try:
            with open(cache_path, encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("sig") == sig:
                return cached.get("text", "")
        except Exception:
            pass

    if not sessions:
        return ""

    lines = ["Recent days (newest first):"]
    for s in sessions[:12]:
        summ = (s.get("summary") or "").strip()
        if summ:
            lines.append(f"- {s.get('date','')}: {summ}")
    if todos:
        lines += ["", "Open to-dos:"]
        for t in todos[:20]:
            txt = (t.get("text") or "").strip()
            if txt:
                lines.append(f"- {txt}")

    text = ""
    try:
        import gemini
        if gemini.available():
            log("Summarizing your recent work for the knowledge base…")
            text = gemini.summarize_overview("\n".join(lines), log=log)
    except Exception as e:
        log(f"KB overview skipped ({e}).")
        text = ""

    if text:
        try:
            analyze.atomic_write_json(cache_path, {
                "sig": sig, "text": text,
                "generated": dt.datetime.now().isoformat(timespec="seconds")})
        except Exception:
            pass
        index_text_unit(rec_dir, "_overview", "overview", text, log=log)
    return text


def _read_kb_overview(rec_dir):
    try:
        with open(os.path.join(rec_dir, "kb_overview.json"), encoding="utf-8") as f:
            d = json.load(f)
        return (d.get("text") or "").strip(), (d.get("generated") or "")
    except Exception:
        return "", ""


def build_period_insights(rec_dir, days, label, force=False, log=print):
    """Generate + cache a model-written weekly/monthly review. ``days`` is the
    look-back window (7 = week, 30 = month), ``label`` is "week"/"month". Cached
    in <rec_dir>/period_<label>.json, hash-gated on the inputs so the model only
    runs when the days/to-dos in the window change. Returns text ('' if n/a)."""
    path = kb.db_path(rec_dir)
    sessions = kb.recent_sessions(path, limit=200)
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    recent = [s for s in sessions if (s.get("date") or "") >= cutoff]
    todos = _dedup_todo_rows(kb.open_action_items(path))
    cache_path = os.path.join(rec_dir, f"period_{label}.json")

    sig_src = json.dumps(
        [label] + [(s.get("date"), s.get("id"), s.get("summary")) for s in recent]
        + [t.get("text") for t in todos], ensure_ascii=False, sort_keys=True)
    sig = hashlib.sha1(sig_src.encode("utf-8")).hexdigest()

    if not force:
        try:
            with open(cache_path, encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("sig") == sig:
                return cached.get("text", "")
        except Exception:
            pass

    if not recent:
        return ""

    lines = [f"Days in the last {label} (newest first):"]
    for s in recent[:40]:
        summ = (s.get("summary") or "").strip()
        if summ:
            lines.append(f"- {s.get('date','')}: {summ}")
    if todos:
        lines += ["", "Still-open to-dos:"]
        for t in todos[:30]:
            txt = (t.get("text") or "").strip()
            if txt:
                lines.append(f"- {txt}")

    text = ""
    try:
        import gemini
        if gemini.available():
            log(f"Summarizing your {label}…")
            text = gemini.summarize_period("\n".join(lines), label, log=log)
    except Exception as e:
        log(f"{label.capitalize()} insight skipped ({e}).")
        text = ""

    if text:
        try:
            analyze.atomic_write_json(cache_path, {
                "sig": sig, "text": text,
                "generated": dt.datetime.now().isoformat(timespec="seconds")})
        except Exception:
            pass
        index_text_unit(rec_dir, f"_period_{label}", label, text, log=log)
    return text


def _read_period(rec_dir, label):
    try:
        with open(os.path.join(rec_dir, f"period_{label}.json"), encoding="utf-8") as f:
            d = json.load(f)
        return (d.get("text") or "").strip(), (d.get("generated") or "")
    except Exception:
        return "", ""


def build_dashboard(rec_dir, log=print):
    esc = html.escape
    path = kb.db_path(rec_dir)
    todos = _dedup_todo_rows(kb.open_action_items(path))
    sessions = kb.recent_sessions(path, limit=60)
    totals = kb.topic_totals(path)
    days = kb.day_count(path)

    p = [f"<!doctype html><html><head><meta charset='utf-8'><title>Knowledge base</title>"
         f"<style>{CSS}</style></head><body><div class='wrap'>"]
    p.append("<h1>Personal knowledge base</h1>")
    total_min = sum(m for _t, m in totals)
    p.append("<div class='kpi'>"
             f"<div class='b'><div class='n'>{days}</div><div class='l'>days recorded</div></div>"
             f"<div class='b'><div class='n'>{len(todos)}</div><div class='l'>open to-dos</div></div>"
             f"<div class='b'><div class='n'>{total_min/60:.1f}h</div><div class='l'>time analyzed</div></div>"
             "</div>")

    ov_text, ov_when = _read_kb_overview(rec_dir)
    if ov_text:
        paras = "".join(f"<p style='margin:6px 0'>{esc(ln)}</p>"
                        for ln in ov_text.split("\n") if ln.strip())
        when = (f" &nbsp;·&nbsp; <span style='text-transform:none;font-weight:400'>"
                f"updated {esc(ov_when[:16].replace('T', ' '))}</span>") if ov_when else ""
        p.append(f"<div class='card'><h2>Overview{when}</h2>"
                 f"<div class='lead'>{paras}</div></div>")

    # Weekly + monthly roll-ups (model-written; cached, refreshed each Process run)
    for _lbl, _head in (("week", "This week"), ("month", "This month")):
        _txt, _when = _read_period(rec_dir, _lbl)
        if not _txt:
            continue
        _paras = "".join(f"<p style='margin:6px 0'>{esc(ln)}</p>"
                         for ln in _txt.split("\n") if ln.strip())
        _w = (f" &nbsp;·&nbsp; <span style='text-transform:none;font-weight:400'>"
              f"updated {esc(_when[:16].replace('T', ' '))}</span>") if _when else ""
        p.append(f"<div class='card f'><h2>{_head}{_w}</h2>"
                 f"<div class='lead'>{_paras}</div></div>")

    p.append("<div class='searchbar'>"
             "<input class='search' id='q' autocomplete='off' spellcheck='false' "
             "placeholder='Search your days, tasks, topics, transcript…  (press /)'>"
             "<div class='meta' id='qmode'></div></div>")
    # Direct search hits (transcript snippets, to-dos, roll-ups) — filled by JS.
    p.append("<div class='card' id='matchwrap' style='display:none'>"
             "<h2>Top matches</h2><div id='matches'></div></div>")

    p.append("<div class='card f'><h2>Open to-dos</h2>")
    if todos:
        p.append("<ul class='clean todo'>")
        for t in todos:
            rp = t.get("report_path") or ""
            link = f" <a href='{esc(rp.replace(rec_dir, '.').replace(os.sep, '/'))}'>({esc(t.get('date',''))})</a>" if rp else f" <span class='pill'>{esc(t.get('date',''))}</span>"
            p.append(f"<li>{esc(t['text'])}{link}</li>")
        p.append("</ul>")
    else:
        p.append("<p class='empty'>No open to-dos. Analyze a session to populate this.</p>")
    p.append("</div>")

    if totals:
        tb = [{"activity": t, "minutes": m} for t, m in totals]
        p.append("<div class='card f'><h2>Time by topic (all days)</h2>" + _bar(tb, esc) + "</div>")

    p.append("<div class='card'><h2>Recent days</h2>")
    if sessions:
        p.append("<div id='days'>")
        for i, s in enumerate(sessions):
            rp = (s.get("report_path") or "").replace(rec_dir, ".").replace(os.sep, "/")
            head = f"<a href='{esc(rp)}'>{esc(s.get('date',''))}</a>" if rp else esc(s.get("date", ""))
            sid = esc(str(s.get("id", "")))
            p.append(f"<div class='run day' data-sid='{sid}' data-i='{i}' style='display:block'>"
                     f"<h3 style='margin:6px 0'>{head} "
                     f"<span class='pill'>{s.get('open_todos',0)} to-do</span></h3>"
                     f"<p style='color:var(--txt)'>{esc(s.get('summary',''))}</p></div>")
        p.append("</div>")
        p.append("<p id='noresults' class='empty' style='display:none'>"
                 "No days match — try different words.</p>")
    else:
        p.append("<p class='empty'>No sessions yet.</p>")
    p.append("</div>")

    p.append("<script>" + DASH_JS + "</script>")
    p.append("</div></body></html>")
    out = os.path.join(rec_dir, "dashboard.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write("".join(p))
    log(f"Dashboard updated → {out}")
    return out
