"""Personal knowledge base: a SQLite store of analyzed sessions.

Each analyzed session contributes a summary, accomplishments ("done"),
action items ("to-do"), topics, and a time-per-activity breakdown. The
dashboard (built in insights.py) reads these tables to show what you've done
and what's still open across days.
"""

import os
import json
import sqlite3
import datetime as dt

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    date         TEXT,
    started      TEXT,
    ended        TEXT,
    duration_sec INTEGER,
    summary      TEXT,
    report_path  TEXT,
    created_at   TEXT
);
CREATE TABLE IF NOT EXISTS accomplishments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    text       TEXT
);
CREATE TABLE IF NOT EXISTS action_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    date       TEXT,
    text       TEXT,
    status     TEXT DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS topics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    topic      TEXT,
    minutes    REAL
);
CREATE TABLE IF NOT EXISTS embeddings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    kind       TEXT,          -- 'session' | 'todo'
    text       TEXT,
    dim        INTEGER,
    vec        BLOB           -- float32 little-endian
);
-- Content hash of the documents last embedded for a session, so re-indexing
-- can skip sessions whose summary/to-dos/topics haven't changed.
CREATE TABLE IF NOT EXISTS embed_state (
    session_id TEXT PRIMARY KEY,
    doc_hash   TEXT
);
-- Child-table lookups are all by session_id (and status for the to-do list);
-- these keep the dashboard/search snappy as the archive grows.
CREATE INDEX IF NOT EXISTS idx_acc_session    ON accomplishments(session_id);
CREATE INDEX IF NOT EXISTS idx_action_session ON action_items(session_id);
CREATE INDEX IF NOT EXISTS idx_action_status  ON action_items(status);
CREATE INDEX IF NOT EXISTS idx_topics_session ON topics(session_id);
CREATE INDEX IF NOT EXISTS idx_emb_session    ON embeddings(session_id);
CREATE INDEX IF NOT EXISTS idx_emb_kind       ON embeddings(kind);
"""


def db_path(rec_dir):
    return os.path.join(rec_dir, "knowledge_base.db")


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def save_session(path, session_id, meta, insights):
    """Insert/replace a session and its child rows. `insights` is the dict from
    the reduce step; `meta` carries date/started/ended/duration/report_path."""
    conn = connect(path)
    try:
        c = conn.cursor()
        # Clear any prior rows for this session (re-analysis is idempotent).
        for tbl in ("accomplishments", "action_items", "topics"):
            c.execute(f"DELETE FROM {tbl} WHERE session_id=?", (session_id,))
        c.execute("DELETE FROM sessions WHERE id=?", (session_id,))

        date = meta.get("date") or dt.date.today().isoformat()
        c.execute(
            "INSERT INTO sessions (id,date,started,ended,duration_sec,summary,report_path,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (session_id, date, meta.get("started"), meta.get("ended"),
             int(meta.get("duration_sec") or 0), insights.get("summary", ""),
             meta.get("report_path", ""), dt.datetime.now().isoformat(timespec="seconds")),
        )
        for t in insights.get("accomplishments", []) or []:
            c.execute("INSERT INTO accomplishments (session_id,text) VALUES (?,?)",
                      (session_id, str(t)))
        for t in insights.get("action_items", []) or []:
            c.execute("INSERT INTO action_items (session_id,date,text,status) VALUES (?,?,?, 'open')",
                      (session_id, date, str(t)))
        for tb in insights.get("time_breakdown", []) or []:
            if isinstance(tb, dict):
                c.execute("INSERT INTO topics (session_id,topic,minutes) VALUES (?,?,?)",
                          (session_id, str(tb.get("activity", "")), float(tb.get("minutes", 0) or 0)))
        for tp in insights.get("topics", []) or []:
            c.execute("INSERT INTO topics (session_id,topic,minutes) VALUES (?,?,?)",
                      (session_id, str(tp), 0.0))
        conn.commit()
    finally:
        conn.close()


def open_action_items(path):
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT a.id,a.text,a.date,a.session_id,s.report_path"
            " FROM action_items a LEFT JOIN sessions s ON s.id=a.session_id"
            " WHERE a.status='open' ORDER BY a.date DESC, a.id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_action_status(path, item_id, status):
    conn = connect(path)
    try:
        conn.execute("UPDATE action_items SET status=? WHERE id=?", (status, item_id))
        conn.commit()
    finally:
        conn.close()


def recent_sessions(path, limit=30):
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY date DESC, started DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["accomplishments"] = [x["text"] for x in conn.execute(
                "SELECT text FROM accomplishments WHERE session_id=?", (r["id"],))]
            d["open_todos"] = conn.execute(
                "SELECT COUNT(*) FROM action_items WHERE session_id=? AND status='open'",
                (r["id"],)).fetchone()[0]
            out.append(d)
        return out
    finally:
        conn.close()


def topic_totals(path):
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT topic, SUM(minutes) m FROM topics WHERE minutes>0"
            " GROUP BY topic ORDER BY m DESC"
        ).fetchall()
        return [(r["topic"], r["m"]) for r in rows]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Embeddings (semantic search)
# --------------------------------------------------------------------------
def save_embeddings(path, session_id, items, doc_hash=None):
    """Replace this session's embedding rows. `items` is an iterable of
    (kind, text, vector) where vector is a float32-compatible sequence.
    `doc_hash`, if given, records what was embedded so future re-indexing can
    skip this session when its content is unchanged."""
    import numpy as np
    conn = connect(path)
    try:
        conn.execute("DELETE FROM embeddings WHERE session_id=?", (session_id,))
        for kind, text, vec in items:
            blob = np.asarray(vec, dtype="float32").tobytes()
            conn.execute(
                "INSERT INTO embeddings (session_id,kind,text,dim,vec) VALUES (?,?,?,?,?)",
                (session_id, str(kind), str(text), len(blob) // 4, sqlite3.Binary(blob)),
            )
        if doc_hash is not None:
            conn.execute(
                "INSERT INTO embed_state (session_id, doc_hash) VALUES (?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET doc_hash=excluded.doc_hash",
                (session_id, str(doc_hash)),
            )
        conn.commit()
    finally:
        conn.close()


def embed_hashes(path):
    """Map of session_id -> doc_hash for everything already embedded."""
    conn = connect(path)
    try:
        return {r["session_id"]: r["doc_hash"]
                for r in conn.execute("SELECT session_id, doc_hash FROM embed_state")}
    finally:
        conn.close()


def clear_embeddings(path, session_id=None):
    conn = connect(path)
    try:
        if session_id is None:
            conn.execute("DELETE FROM embeddings")
            conn.execute("DELETE FROM embed_state")
        else:
            conn.execute("DELETE FROM embeddings WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM embed_state WHERE session_id=?", (session_id,))
        conn.commit()
    finally:
        conn.close()


def has_embeddings(path):
    conn = connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] > 0
    finally:
        conn.close()


def search(path, query_vec, k=12, kinds=None):
    """Brute-force cosine search over stored embeddings.

    Returns [{score, session_id, kind, text, date, report_path}] best-first.
    Brute force is fine here — a personal KB has at most a few thousand rows.

    Only vectors whose dimension matches the query are compared. Different
    embedding backends/models produce different dimensions; restricting to the
    query's dimension keeps np.vstack safe and means a freshly switched backend
    searches only what's been re-embedded under it (run a reindex to migrate).
    """
    import numpy as np
    q = np.asarray(query_vec, dtype="float32").ravel()
    qd = int(q.shape[0])
    if qd == 0:
        return []
    conn = connect(path)
    try:
        sql = ("SELECT e.session_id, e.kind, e.text, e.vec, s.date, s.report_path"
               " FROM embeddings e LEFT JOIN sessions s ON s.id = e.session_id"
               " WHERE e.dim = ?")
        params = [qd]
        if kinds:
            sql += " AND e.kind IN (%s)" % ",".join("?" * len(kinds))
            params += list(kinds)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    if not rows:
        return []
    qn = float(np.linalg.norm(q)) or 1.0
    mat = np.vstack([np.frombuffer(r["vec"], dtype="float32") for r in rows])
    norms = np.linalg.norm(mat, axis=1)
    norms[norms == 0] = 1.0
    sims = (mat @ q) / (norms * qn)
    order = np.argsort(-sims)[:k]
    out = []
    for i in order:
        r = rows[int(i)]
        out.append({"score": round(float(sims[int(i)]), 4),
                    "session_id": r["session_id"], "kind": r["kind"],
                    "text": r["text"], "date": r["date"],
                    "report_path": r["report_path"]})
    return out


def session_payloads(path):
    """Everything needed to (re)build embedding documents, one dict per session."""
    conn = connect(path)
    try:
        out = []
        for s in conn.execute("SELECT id, summary FROM sessions").fetchall():
            sid = s["id"]
            acc = [r["text"] for r in conn.execute(
                "SELECT text FROM accomplishments WHERE session_id=?", (sid,))]
            todos = [r["text"] for r in conn.execute(
                "SELECT text FROM action_items WHERE session_id=? AND status='open'", (sid,))]
            topics = [r["topic"] for r in conn.execute(
                "SELECT topic FROM topics WHERE session_id=? AND minutes=0", (sid,))]
            out.append({"id": sid, "summary": s["summary"] or "",
                        "accomplishments": acc, "action_items": todos, "topics": topics})
        return out
    finally:
        conn.close()
