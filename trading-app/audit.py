"""审计留痕：投研会全过程持久化（可回溯）。

表结构：
  research_sessions: 会议会话（id/name/symbols/created/finished/status/summary）
  research_audit:    逐条留痕（session_id/round/role_id/role_name/action/content/json/ts）
"""
import json
import os
import sqlite3
import time


ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "trade.db")


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables():
    conn = _conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS research_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        symbols TEXT,
        status TEXT DEFAULT 'running',
        created REAL,
        finished REAL,
        summary TEXT
    );
    CREATE TABLE IF NOT EXISTS research_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER,
        round TEXT,
        role_id TEXT,
        role_name TEXT,
        action TEXT,
        content TEXT,
        payload TEXT,
        ts REAL
    );
    CREATE INDEX IF NOT EXISTS idx_audit_session ON research_audit (session_id, id);
    """)
    conn.commit()
    conn.close()


def create_session(name: str, symbols: list) -> int:
    init_tables()
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO research_sessions (name, symbols, status, created) VALUES (?,?,?,?)",
        (name, json.dumps(symbols), "running", time.time()))
    conn.commit()
    sid = cur.lastrowid
    conn.close()
    return sid


def log(session_id: int, round_name: str, role_id: str, role_name: str,
        action: str, payload=None) -> None:
    """Append one audit entry."""
    init_tables()
    conn = _conn()
    content = action
    if isinstance(payload, dict):
        # short human-readable content
        content = payload.get("view") or payload.get("verdict") or payload.get("verdict_text") or action
    conn.execute(
        "INSERT INTO research_audit (session_id, round, role_id, role_name, action, content, payload, ts) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (session_id, round_name, role_id, role_name, action, str(content)[:500],
         json.dumps(payload, ensure_ascii=False) if payload is not None else None, time.time()))
    conn.commit()
    conn.close()


def log_round(session_id: int, round_no: int, title: str, summary: str) -> None:
    log(session_id, f"round{round_no}", "system", "系统", f"{title}｜{summary}", {"summary": summary})


def finish_session(session_id: int, summary: dict) -> None:
    init_tables()
    conn = _conn()
    conn.execute("UPDATE research_sessions SET status='finished', finished=?, summary=? WHERE id=?",
                 (time.time(), json.dumps(summary, ensure_ascii=False), session_id))
    conn.commit()
    conn.close()


def list_sessions(limit: int = 30) -> list:
    init_tables()
    conn = _conn()
    rows = conn.execute(
        "SELECT id, name, symbols, status, created, finished, summary FROM research_sessions "
        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["symbols"] = json.loads(d["symbols"] or "[]")
        except Exception:
            d["symbols"] = []
        try:
            d["summary"] = json.loads(d["summary"]) if d["summary"] else None
        except Exception:
            pass
        out.append(d)
    return out


def get_session(session_id: int) -> dict:
    init_tables()
    conn = _conn()
    s = conn.execute("SELECT * FROM research_sessions WHERE id=?", (session_id,)).fetchone()
    if not s:
        conn.close()
        return {}
    entries = conn.execute(
        "SELECT * FROM research_audit WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
    conn.close()
    out = dict(s)
    try:
        out["symbols"] = json.loads(out["symbols"] or "[]")
    except Exception:
        out["symbols"] = []
    try:
        out["summary"] = json.loads(out["summary"]) if out["summary"] else None
    except Exception:
        pass
    out["entries"] = []
    for e in entries:
        d = dict(e)
        try:
            d["payload"] = json.loads(d["payload"]) if d["payload"] else None
        except Exception:
            pass
        out["entries"].append(d)
    return out


def delete_session(session_id: int) -> None:
    init_tables()
    conn = _conn()
    conn.execute("DELETE FROM research_audit WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM research_sessions WHERE id=?", (session_id,))
    conn.commit()
    conn.close()
