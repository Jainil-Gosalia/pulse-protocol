import os
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

DB_PATH = Path(os.environ.get("PULSE_DB")
               or Path(__file__).parent.parent.parent / "events.db")

# event_type -> session status. Events not listed leave status unchanged.
STATUS_TRANSITIONS = {
    "session_start": "idle",
    "user_prompt": "working",
    "activity": "working",
    "subagent_completed": "working",
    "heartbeat": "working",
    "needs_input": "needs_input",
    "error": "error",
    "completed": "idle",
    "session_end": "ended",
}


def _scrub(s: Optional[str]) -> Optional[str]:
    """Strip lone UTF-16 surrogates (from emitters with mis-decoded stdin)
    that would crash UTF-8 JSON responses."""
    if isinstance(s, str):
        return s.encode("utf-8", "replace").decode("utf-8")
    return s


def _scrub_obj(obj):
    """Recursively scrub strings in decoded JSON — escaped surrogates in
    stored rows come back to life on json.loads."""
    if isinstance(obj, str):
        return _scrub(obj)
    if isinstance(obj, dict):
        return {k: _scrub_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_obj(v) for v in obj]
    return obj


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = _connect()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id TEXT NOT NULL,
            hostname TEXT NOT NULL,
            session_id TEXT,
            cwd TEXT,
            source TEXT NOT NULL DEFAULT 'unknown',
            event_type TEXT NOT NULL,
            hook_event_name TEXT,
            summary TEXT,
            payload_json TEXT,
            created_at TEXT NOT NULL
        )
    """)
    # Migrate a pre-multi-tool database that lacks the source column.
    cols = [row[1] for row in c.execute("PRAGMA table_info(events)").fetchall()]
    if "source" not in cols:
        c.execute("ALTER TABLE events ADD COLUMN source TEXT NOT NULL DEFAULT 'unknown'")

    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            instance_id TEXT PRIMARY KEY,
            session_id TEXT,
            source TEXT NOT NULL DEFAULT 'unknown',
            hostname TEXT,
            cwd TEXT,
            status TEXT NOT NULL DEFAULT 'idle',
            last_summary TEXT,
            started_at TEXT,
            last_seen TEXT,
            ended_at TEXT,
            remote_approval INTEGER NOT NULL DEFAULT 0
        )
    """)
    scols = [row[1] for row in c.execute("PRAGMA table_info(sessions)").fetchall()]
    if "remote_approval" not in scols:
        c.execute("ALTER TABLE sessions ADD COLUMN remote_approval INTEGER NOT NULL DEFAULT 0")

    c.execute("""
        CREATE TABLE IF NOT EXISTS decisions (
            id TEXT PRIMARY KEY,
            instance_id TEXT NOT NULL,
            summary TEXT,
            payload_json TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            reason TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            responded_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id TEXT NOT NULL,
            text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            created_at TEXT NOT NULL,
            delivered_at TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_events_instance ON events(instance_id, created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at)")
    conn.commit()
    conn.close()


def _upsert_session(c: sqlite3.Cursor, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    now = event["created_at"]
    new_status = STATUS_TRANSITIONS.get(event["event_type"])
    ended_at = now if event["event_type"] == "session_end" else None

    c.execute("SELECT status, started_at, remote_approval FROM sessions WHERE instance_id = ?",
              (event["instance_id"],))
    row = c.fetchone()

    if row is None:
        status = new_status or "working"
        remote_approval = False
        c.execute("""
            INSERT INTO sessions
            (instance_id, session_id, source, hostname, cwd, status,
             last_summary, started_at, last_seen, ended_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (event["instance_id"], event["session_id"], event["source"],
              event["hostname"], event["cwd"], status,
              event["summary"], now, now, ended_at))
        started_at = now
    else:
        status = new_status or row[0]
        started_at = row[1]
        remote_approval = bool(row[2])
        c.execute("""
            UPDATE sessions
            SET status = ?, last_summary = ?, last_seen = ?,
                cwd = COALESCE(?, cwd), session_id = COALESCE(?, session_id),
                source = ?, ended_at = COALESCE(?, ended_at)
            WHERE instance_id = ?
        """, (status, event["summary"], now, event["cwd"], event["session_id"],
              event["source"], ended_at, event["instance_id"]))

    return {
        "instance_id": event["instance_id"],
        "session_id": event["session_id"],
        "source": event["source"],
        "hostname": event["hostname"],
        "cwd": event["cwd"],
        "status": status,
        "last_summary": event["summary"],
        "started_at": started_at,
        "last_seen": now,
        "ended_at": ended_at,
        "remote_approval": remote_approval,
    }


def insert_event(
    instance_id: str,
    hostname: str,
    event_type: str,
    summary: str,
    payload: Dict[str, Any],
    source: str = "unknown",
    session_id: Optional[str] = None,
    cwd: Optional[str] = None,
    hook_event_name: Optional[str] = None,
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Insert an event, update its session, and return (event, session)."""
    conn = _connect()
    c = conn.cursor()
    created_at = created_at or _now()

    # Scrub lone surrogates once, up front, so every downstream write (events
    # row, sessions row, broadcast payload) is UTF-8 safe.
    summary = _scrub(summary)
    cwd = _scrub(cwd)
    payload = _scrub_obj(payload)

    c.execute(
        """
        INSERT INTO events
        (instance_id, hostname, session_id, cwd, source, event_type,
         hook_event_name, summary, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (instance_id, hostname, session_id, cwd, source, event_type,
         hook_event_name, summary,
         json.dumps(payload, ensure_ascii=False), created_at)
    )
    event_id = c.lastrowid

    event = {
        "id": event_id,
        "instance_id": instance_id,
        "hostname": hostname,
        "session_id": session_id,
        "cwd": cwd,
        "source": source,
        "event_type": event_type,
        "hook_event_name": hook_event_name,
        "summary": summary,
        "payload": payload,
        "created_at": created_at,
    }
    session = _upsert_session(c, event)

    conn.commit()
    conn.close()
    return {"event": event, "session": session}


def _row_to_event(row) -> Dict[str, Any]:
    return {
        "id": row[0],
        "instance_id": row[1],
        "hostname": row[2],
        "session_id": row[3],
        "cwd": row[4],
        "source": row[5],
        "event_type": row[6],
        "hook_event_name": row[7],
        "summary": _scrub(row[8]),
        "payload": _scrub_obj(json.loads(row[9])) if row[9] else {},
        "created_at": row[10],
    }


def get_events(
    instance_id: Optional[str] = None,
    event_type: Optional[str] = None,
    source: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    conn = _connect()
    c = conn.cursor()

    query = """SELECT id, instance_id, hostname, session_id, cwd, source,
               event_type, hook_event_name, summary, payload_json, created_at
               FROM events WHERE 1=1"""
    params: List[Any] = []

    if instance_id:
        query += " AND instance_id = ?"
        params.append(instance_id)
    if event_type:
        query += " AND event_type = ?"
        params.append(event_type)
    if source:
        query += " AND source = ?"
        params.append(source)
    if since:
        query += " AND created_at > ?"
        params.append(since)

    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    c.execute(query, params)
    events = [_row_to_event(row) for row in c.fetchall()]
    conn.close()
    return events


SESSION_COLS = """instance_id, session_id, source, hostname, cwd, status,
               last_summary, started_at, last_seen, ended_at, remote_approval"""


def _row_to_session(r) -> Dict[str, Any]:
    return {
        "instance_id": r[0], "session_id": r[1], "source": r[2],
        "hostname": r[3], "cwd": r[4], "status": r[5], "last_summary": r[6],
        "started_at": r[7], "last_seen": r[8], "ended_at": r[9],
        "remote_approval": bool(r[10]),
    }


def get_sessions(include_ended: bool = True) -> List[Dict[str, Any]]:
    conn = _connect()
    c = conn.cursor()
    query = f"SELECT {SESSION_COLS} FROM sessions"
    if not include_ended:
        query += " WHERE status != 'ended'"
    query += " ORDER BY last_seen DESC"
    c.execute(query)
    sessions = [_row_to_session(r) for r in c.fetchall()]
    conn.close()
    return sessions


def get_session(instance_id: str) -> Optional[Dict[str, Any]]:
    conn = _connect()
    c = conn.cursor()
    c.execute(f"SELECT {SESSION_COLS} FROM sessions WHERE instance_id = ?",
              (instance_id,))
    row = c.fetchone()
    conn.close()
    return _row_to_session(row) if row else None


def set_session_mode(instance_id: str, remote_approval: bool) -> Optional[Dict[str, Any]]:
    conn = _connect()
    c = conn.cursor()
    c.execute("UPDATE sessions SET remote_approval = ? WHERE instance_id = ?",
              (1 if remote_approval else 0, instance_id))
    conn.commit()
    conn.close()
    return get_session(instance_id)


def touch_session(instance_id: str) -> Optional[Dict[str, Any]]:
    """Heartbeat: refresh last_seen (and revive stale -> working) without
    inserting an event row."""
    conn = _connect()
    c = conn.cursor()
    c.execute("""UPDATE sessions SET last_seen = ?,
                 status = CASE WHEN status IN ('working', 'stale') THEN 'working'
                               ELSE status END
                 WHERE instance_id = ?""", (_now(), instance_id))
    conn.commit()
    conn.close()
    return get_session(instance_id)


def reap_stale_sessions(stale_minutes: int) -> List[Dict[str, Any]]:
    """Mark 'working' sessions silent for longer than the threshold as
    'stale' (agent likely crashed or its terminal closed without a
    session_end). Returns the sessions that changed."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
    cutoff_s = cutoff.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT instance_id FROM sessions WHERE status = 'working' AND last_seen < ?",
              (cutoff_s,))
    ids = [r[0] for r in c.fetchall()]
    if ids:
        c.executemany("UPDATE sessions SET status = 'stale' WHERE instance_id = ?",
                      [(i,) for i in ids])
        conn.commit()
    conn.close()
    return [s for i in ids if (s := get_session(i))]


def prune_events(retention_days: int) -> int:
    """Delete events older than the retention window. Sessions are kept."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    cutoff_s = cutoff.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    conn = _connect()
    c = conn.cursor()
    c.execute("DELETE FROM events WHERE created_at < ?", (cutoff_s,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def get_stats(days: int = 7) -> Dict[str, Any]:
    """Activity + attention-latency stats over the last N days.

    'Response seconds' measures the gap between an attention event
    (needs_input) and the next event on the same session — i.e. how long
    agents sit blocked waiting for the human.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)
             ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT COUNT(*), COUNT(DISTINCT instance_id) FROM events WHERE created_at > ?",
              (since,))
    events_count, sessions_count = c.fetchone()
    c.execute("""SELECT COUNT(*) FROM events
                 WHERE event_type IN ('needs_input', 'error') AND created_at > ?""", (since,))
    attention_count = c.fetchone()[0]
    c.execute("""
        SELECT e.created_at,
               (SELECT MIN(e2.created_at) FROM events e2
                WHERE e2.instance_id = e.instance_id AND e2.id > e.id)
        FROM events e
        WHERE e.event_type = 'needs_input' AND e.created_at > ?
    """, (since,))
    waits = [(_parse_ts(nxt) - _parse_ts(start)).total_seconds()
             for start, nxt in c.fetchall() if nxt]
    conn.close()

    waits.sort()
    return {
        "days": days,
        "sessions": sessions_count,
        "events": events_count,
        "attention_events": attention_count,
        "median_response_s": round(waits[len(waits) // 2], 1) if waits else None,
        "total_blocked_s": round(sum(waits), 1),
    }


def queue_message(instance_id: str, text: str) -> Dict[str, Any]:
    conn = _connect()
    c = conn.cursor()
    c.execute("INSERT INTO messages (instance_id, text, created_at) VALUES (?, ?, ?)",
              (instance_id, text, _now()))
    mid = c.lastrowid
    conn.commit()
    conn.close()
    return {"id": mid, "instance_id": instance_id, "text": text, "status": "queued"}


def pop_message(instance_id: str) -> Optional[str]:
    """Oldest queued follow-up for this session, marked delivered."""
    conn = _connect()
    c = conn.cursor()
    c.execute("""SELECT id, text FROM messages
                 WHERE instance_id = ? AND status = 'queued'
                 ORDER BY id LIMIT 1""", (instance_id,))
    row = c.fetchone()
    if row:
        c.execute("UPDATE messages SET status = 'delivered', delivered_at = ? WHERE id = ?",
                  (_now(), row[0]))
        conn.commit()
    conn.close()
    return row[1] if row else None


# ---------- decisions (remote approval) ----------

def _row_to_decision(r) -> Dict[str, Any]:
    return {
        "id": r[0], "instance_id": r[1], "summary": r[2],
        "payload": json.loads(r[3]) if r[3] else {},
        "status": r[4], "reason": r[5],
        "created_at": r[6], "expires_at": r[7], "responded_at": r[8],
    }


DECISION_COLS = "id, instance_id, summary, payload_json, status, reason, created_at, expires_at, responded_at"


def create_decision(decision_id: str, instance_id: str, summary: str,
                    payload: Dict[str, Any], ttl_seconds: int) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=ttl_seconds)
    fmt = "%Y-%m-%dT%H:%M:%S.%f"
    conn = _connect()
    c = conn.cursor()
    c.execute("""INSERT INTO decisions
                 (id, instance_id, summary, payload_json, status, created_at, expires_at)
                 VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
              (decision_id, instance_id, summary, json.dumps(payload),
               now.strftime(fmt)[:-3] + "Z", expires.strftime(fmt)[:-3] + "Z"))
    conn.commit()
    conn.close()
    return get_decision(decision_id)


def get_decision(decision_id: str) -> Optional[Dict[str, Any]]:
    conn = _connect()
    c = conn.cursor()
    c.execute(f"SELECT {DECISION_COLS} FROM decisions WHERE id = ?", (decision_id,))
    row = c.fetchone()
    conn.close()
    return _row_to_decision(row) if row else None


def respond_decision(decision_id: str, status: str, reason: str = "") -> Optional[Dict[str, Any]]:
    """status: 'allow' or 'deny'. Only pending decisions can be answered."""
    conn = _connect()
    c = conn.cursor()
    c.execute("""UPDATE decisions SET status = ?, reason = ?, responded_at = ?
                 WHERE id = ? AND status = 'pending'""",
              (status, reason, _now(), decision_id))
    changed = c.rowcount
    conn.commit()
    conn.close()
    return get_decision(decision_id) if changed else None


def get_pending_decisions() -> List[Dict[str, Any]]:
    conn = _connect()
    c = conn.cursor()
    c.execute(f"SELECT {DECISION_COLS} FROM decisions WHERE status = 'pending' ORDER BY created_at")
    rows = [_row_to_decision(r) for r in c.fetchall()]
    conn.close()
    return rows


def expire_decisions() -> List[Dict[str, Any]]:
    """Mark overdue pending decisions expired; returns those that changed."""
    now = _now()
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT id FROM decisions WHERE status = 'pending' AND expires_at < ?", (now,))
    ids = [r[0] for r in c.fetchall()]
    if ids:
        c.executemany("UPDATE decisions SET status = 'expired' WHERE id = ?",
                      [(i,) for i in ids])
        conn.commit()
    conn.close()
    return [d for i in ids if (d := get_decision(i))]
