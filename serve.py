"""Local dashboard server with semantic search.

The dashboard (dashboard.html) can filter day cards by substring when opened
straight off disk (file://). To rank days/to-dos/topics by *meaning* it needs a
backend that embeds the query at search time and compares it against the stored
vectors — that's what this does.

Run it and a browser opens to the dashboard. Typing in the search box hits
GET /search?q=...&k=..., which embeds the query with the configured backend
(see embed.py — local fastembed by default, or Gemini) and returns the
best-matching sessions by cosine similarity. The page itself is served from
127.0.0.1 only; with the local backend nothing leaves the machine, whereas the
Gemini backend sends each query to Google to embed.

    python serve.py                # serve + open browser
    python serve.py --reindex      # rebuild embeddings first (after a backend switch)
    python serve.py --port 9000 --no-browser

If the embedding backend isn't available, /search returns {"error": ...} and the
page falls back to its plain substring filter automatically.
"""

import os
import sys
import json
import argparse
import threading
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# Console here can be GBK (see MEMORY): force UTF-8 so the '→' in log lines and
# any non-ASCII summary text don't crash the process.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

import kb
import embed
import insights

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def default_rec_dir():
    """Same resolution order as recorder_app, so we serve the real archive."""
    env = os.environ.get("RECORDER_OUTPUT_DIR")
    if env:
        return env
    if os.path.isdir("D:\\"):
        return r"D:\DesktopRecordings"
    return os.path.join(APP_DIR, "recordings")


# Embedding a query is the slow part of a search (the model runs on CPU). The
# same few queries get retyped as the user edits the box, so cache the vectors.
_QCACHE = {}
_QCACHE_LOCK = threading.Lock()
_QCACHE_MAX = 256


def query_vector(text):
    """Embed a query, memoized. Returns a float32 vector."""
    with _QCACHE_LOCK:
        hit = _QCACHE.get(text)
    if hit is not None:
        return hit
    vec = embed.embed_query(text)
    with _QCACHE_LOCK:
        if len(_QCACHE) >= _QCACHE_MAX:
            _QCACHE.clear()
        _QCACHE[text] = vec
    return vec


class Handler(SimpleHTTPRequestHandler):
    """Static files from rec_dir, plus a JSON /search endpoint."""

    rec_dir = "."  # set per-instance via partial(directory=...) + class attr

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/search":
            return self._handle_search(parse_qs(parsed.query))
        if parsed.path in ("/", ""):
            # Land on the dashboard rather than a directory listing.
            self.send_response(302)
            self.send_header("Location", "/dashboard.html")
            self.end_headers()
            return
        return super().do_GET()

    def _handle_search(self, qs):
        query = (qs.get("q", [""])[0] or "").strip()
        try:
            k = max(1, min(200, int(qs.get("k", ["50"])[0])))
        except ValueError:
            k = 50
        if not query:
            return self._send_json({"query": "", "results": []})
        if not embed.available():
            # JS sees .error and falls back to substring filtering.
            return self._send_json(
                {"error": "embeddings unavailable", "query": query, "results": []})
        try:
            qvec = query_vector(query)
            results = kb.search(kb.db_path(self.rec_dir), qvec, k=k)
            # Make report links relative to the served root so they work in-browser.
            for r in results:
                rp = r.get("report_path") or ""
                if rp:
                    r["report_path"] = rp.replace(self.rec_dir, ".").replace(os.sep, "/")
            return self._send_json({"query": query, "results": results})
        except Exception as e:
            return self._send_json(
                {"error": str(e), "query": query, "results": []}, status=200)

    def log_message(self, fmt, *args):
        # Quiet the per-asset noise; only surface search queries and errors.
        msg = fmt % args
        if "/search" in msg or " 4" in msg or " 5" in msg:
            sys.stderr.write("  %s\n" % msg)


def _warm_model():
    """Load the embedding model in the background so the first query is fast."""
    if not embed.available():
        return
    try:
        query_vector("warmup")
        print("Embedding model ready — semantic search is live.")
    except Exception as e:
        print(f"Embedding model warmup failed ({e}); search will retry on demand.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Serve the knowledge-base dashboard with semantic search.")
    ap.add_argument("--dir", default=default_rec_dir(), help="recordings/knowledge-base directory")
    ap.add_argument("--port", type=int, default=int(os.environ.get("RECORDER_SERVE_PORT", "8765")))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--reindex", action="store_true", help="rebuild embeddings before serving")
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser")
    args = ap.parse_args(argv)

    rec_dir = args.dir
    os.makedirs(rec_dir, exist_ok=True)

    if args.reindex:
        insights.reindex(rec_dir, force=True)

    # Regenerate the dashboard so it carries the latest data and search JS.
    try:
        insights.build_dashboard(rec_dir)
    except Exception as e:
        print(f"Could not rebuild dashboard ({e}); serving existing file.")

    if embed.available():
        where = ("on-device, offline" if embed.current()[0] == "local"
                 else "CLOUD — search queries are sent to Google")
        print(f"Embedding backend: {embed.label()} ({where}).")
    elif embed.current()[0] == "gemini":
        print("Note: Gemini embedding backend is selected but not ready (missing "
              "GEMINI_API_KEY or google-genai) — search falls back to substring matching.")
    else:
        print("Note: fastembed not installed — search falls back to substring matching.")
        print("      Install with:  pip install fastembed")

    Handler.rec_dir = rec_dir  # read by _handle_search
    handler = partial(Handler, directory=rec_dir)  # static root for SimpleHTTPRequestHandler

    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://{args.host}:{args.port}/dashboard.html"
    print(f"Serving {rec_dir}")
    print(f"Dashboard: {url}   (Ctrl+C to stop)")

    threading.Thread(target=_warm_model, daemon=True).start()
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
