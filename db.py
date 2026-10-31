"""SQLite persistence: tasks (with resumable conversation state), step log, settings, usage."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL DEFAULT 'task',      -- task | goal
    description TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending | running | done | failed | aborted
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL,
    steps       INTEGER NOT NULL DEFAULT 0,
    tokens      INTEGER NOT NULL DEFAULT 0,
    result      TEXT,
    messages    TEXT                               -- JSON conversation, saved after every step
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, id);
CREATE TABLE IF NOT EXISTS steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL,
    step_no     INTEGER NOT NULL,
    ts          REAL NOT NULL,
    tool        TEXT NOT NULL,
    args        TEXT,
    status      TEXT NOT NULL,                     -- ok | error | denied
    output      TEXT,
    duration    REAL,
    agent       TEXT NOT NULL DEFAULT 'main'
);
CREATE INDEX IF NOT EXISTS steps_task ON steps(task_id, step_no);
CREATE TABLE IF NOT EXISTS events (            -- everything an agent narrated, for the web feed
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts    REAL NOT NULL,
    kind  TEXT NOT NULL,
    level TEXT NOT NULL,
    html  TEXT NOT NULL,
    data  TEXT,
    agent TEXT NOT NULL DEFAULT 'main'
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS usage (
    day               TEXT PRIMARY KEY,            -- UTC date YYYY-MM-DD
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost              REAL NOT NULL DEFAULT 0
);
"""


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class DB:
    """Small synchronous wrapper. Every call is a short local query, so it is safe to
    call from the event loop; a lock serialises access across threads."""

    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        for table in ("events", "steps"):
            cols = [r[1] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if "agent" not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN agent TEXT NOT NULL DEFAULT 'main'")

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self.lock:
            return self.conn.execute(sql, args).lastrowid or 0

    # settings ---------------------------------------------------------------
    def get(self, key: str, default: str | None = None) -> str | None:
        rows = self._q("SELECT value FROM settings WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set(self, key: str, value: str | None) -> None:
        if value is None:
            self._x("DELETE FROM settings WHERE key=?", (key,))
        else:
            self._x("INSERT INTO settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # tasks ------------------------------------------------------------------
    def add_task(self, description: str, kind: str = "task") -> int:
        return self._x("INSERT INTO tasks(kind,description,created_at) VALUES(?,?,?)",
                       (kind, description, time.time()))

    def get_task(self, task_id: int) -> dict | None:
        rows = self._q("SELECT * FROM tasks WHERE id=?", (task_id,))
        return dict(rows[0]) if rows else None

    def next_pending(self) -> dict | None:
        rows = self._q("SELECT * FROM tasks WHERE status='pending' ORDER BY id LIMIT 1")
        return dict(rows[0]) if rows else None

    def pending(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT id,kind,description,created_at FROM tasks WHERE status='pending' "
            "ORDER BY id LIMIT ?", (limit,))]

    def cancel_pending(self) -> int:
        """Abort every queued (pending) task. Returns how many were cleared."""
        n = self._q("SELECT COUNT(*) c FROM tasks WHERE status='pending'")[0]["c"]
        if n:
            self._x("UPDATE tasks SET status='aborted', finished_at=? WHERE status='pending'", (time.time(),))
        return n

    def count_pending(self) -> int:
        return self._q("SELECT COUNT(*) c FROM tasks WHERE status='pending'")[0]["c"]

    def recover_running(self) -> list[dict]:
        """Tasks interrupted by a crash/restart go back to the front of the queue."""
        rows = [dict(r) for r in self._q("SELECT id,description FROM tasks WHERE status='running'")]
        self._x("UPDATE tasks SET status='pending' WHERE status='running'")
        return rows

    def start_task(self, task_id: int) -> None:
        self._x("UPDATE tasks SET status='running', started_at=COALESCE(started_at,?) WHERE id=?",
                (time.time(), task_id))

    def save_progress(self, task_id: int, messages: list[dict], steps: int, tokens: int) -> None:
        self._x("UPDATE tasks SET messages=?, steps=?, tokens=? WHERE id=?",
                (json.dumps(messages), steps, tokens, task_id))

    def finish_task(self, task_id: int, status: str, result: str) -> None:
        self._x("UPDATE tasks SET status=?, result=?, finished_at=? WHERE id=?",
                (status, result, time.time(), task_id))

    def load_messages(self, task: dict) -> list[dict] | None:
        return json.loads(task["messages"]) if task.get("messages") else None

    def recent_goal_results(self, limit: int = 5) -> list[dict]:
        rows = self._q("SELECT id,result,status FROM tasks WHERE kind='goal' AND status IN "
                       "('done','failed') ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in reversed(rows)]

    # steps ------------------------------------------------------------------
    def log_step(self, task_id: int, step_no: int, tool: str, args: Any, status: str,
                 output: str, duration: float, agent: str = "main") -> None:
        self._x("INSERT INTO steps(task_id,step_no,ts,tool,args,status,output,duration,agent) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (task_id, step_no, time.time(), tool,
                 args if isinstance(args, str) else json.dumps(args, ensure_ascii=False),
                 status, output[:20000], duration, agent))

    def task_steps(self, task_id: int) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM steps WHERE task_id=? ORDER BY id", (task_id,))]

    def last_steps(self, limit: int = 20) -> list[dict]:
        rows = self._q("SELECT * FROM steps ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in reversed(rows)]

    # events -----------------------------------------------------------------
    def add_event(self, ts: float, kind: str, level: str, html: str, data: dict, agent: str = "main") -> int:
        eid = self._x("INSERT INTO events(ts,kind,level,html,data,agent) VALUES(?,?,?,?,?,?)",
                      (ts, kind, level, html, json.dumps(data, ensure_ascii=False), agent))
        if eid % 500 == 0:  # keep the table bounded
            self._x("DELETE FROM events WHERE id <= ?", (eid - 5000,))
        return eid

    def events_after(self, after_id: int = 0, limit: int = 300) -> list[dict]:
        if after_id:
            rows = self._q("SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (after_id, limit))
        else:
            rows = list(reversed(self._q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))))
        out = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d["data"] or "{}")
            out.append(d)
        return out

    def recent_tasks(self, limit: int = 15) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT id,kind,description,status,created_at,finished_at,steps,tokens,result "
            "FROM tasks WHERE status!='pending' ORDER BY id DESC LIMIT ?", (limit,))]

    # usage ------------------------------------------------------------------
    def add_usage(self, prompt: int, completion: int, cost: float) -> None:
        self._x("INSERT INTO usage(day,prompt_tokens,completion_tokens,cost) VALUES(?,?,?,?) "
                "ON CONFLICT(day) DO UPDATE SET prompt_tokens=prompt_tokens+excluded.prompt_tokens, "
                "completion_tokens=completion_tokens+excluded.completion_tokens, "
                "cost=cost+excluded.cost", (today(), prompt, completion, cost))

    def usage_today(self) -> dict:
        rows = self._q("SELECT * FROM usage WHERE day=?", (today(),))
        if not rows:
            return {"day": today(), "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
        return dict(rows[0])
