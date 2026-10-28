"""SQLite 任务库：飞书重推去重、任务状态、审批记录、几个键值。

任务状态：confirm（等你确认）→ queued → running → done / failed / stopped；
另有 ignored（确认卡点了忽略）、interrupted（飞书桥重启时还没跑完）。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

FINAL = ("done", "failed", "stopped", "interrupted", "ignored")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id        TEXT UNIQUE,          -- 你发的那条飞书消息（重推靠它去重）
    open_id       TEXT,
    chat_id       TEXT,
    prompt        TEXT,
    status        TEXT,
    reason        TEXT DEFAULT '',      -- 为什么要先确认
    parent_task   INTEGER,              -- 引用回复的是哪个任务（续聊）
    session_id    TEXT,
    resume        INTEGER DEFAULT 0,    -- 1 = 接着 session_id 聊
    card_msg_id   TEXT DEFAULT '',      -- 状态卡片
    result_msg_id TEXT DEFAULT '',      -- 结果卡片
    created_at    REAL,
    started_at    REAL,
    finished_at   REAL,
    cost_usd      REAL,
    num_turns     INTEGER,
    exit_code     INTEGER,
    result        TEXT DEFAULT '',
    error         TEXT DEFAULT '',
    delivered     INTEGER DEFAULT 0,    -- 结果已发到飞书
    cwd           TEXT DEFAULT ''       -- 在哪个目录跑（接着电脑上的会话时用它原来的目录），空＝默认工作目录
);
CREATE INDEX IF NOT EXISTS idx_tasks_card ON tasks(card_msg_id);
CREATE INDEX IF NOT EXISTS idx_tasks_result ON tasks(result_msg_id);
CREATE TABLE IF NOT EXISTS approvals (
    id          TEXT PRIMARY KEY,
    task_id     INTEGER,
    tool_name   TEXT,
    tool_input  TEXT,
    decision    TEXT DEFAULT '',        -- allow / allow_all / deny / timeout / cancelled
    decided_by  TEXT DEFAULT '',
    card_msg_id TEXT DEFAULT '',
    created_at  REAL,
    decided_at  REAL
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(tasks)")}
            if "cwd" not in cols:  # 10-26 前建的库没有这一列
                self._db.execute("ALTER TABLE tasks ADD COLUMN cwd TEXT DEFAULT ''")

    # ---------- 任务 ----------
    def add_task(self, *, msg_id: str, open_id: str, chat_id: str, prompt: str, status: str,
                 session_id: str, reason: str = "", parent_task: int | None = None,
                 resume: int = 0, created_at: float | None = None, cwd: str = "") -> int | None:
        """新建任务；msg_id 已存在（飞书重推）返回 None。"""
        try:
            with self._lock:
                cur = self._db.execute(
                    "INSERT INTO tasks (msg_id, open_id, chat_id, prompt, status, reason, parent_task,"
                    " session_id, resume, created_at, cwd) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (msg_id, open_id, chat_id, prompt, status, reason, parent_task, session_id,
                     resume, created_at or time.time(), cwd))
                return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def last_finished(self, session_id: str) -> float | None:
        """飞书桥最后一次在这个会话里跑完的时间。"""
        with self._lock:
            row = self._db.execute("SELECT MAX(finished_at) FROM tasks WHERE session_id=?", (session_id,)).fetchone()
        return row[0] if row and row[0] else None

    def bridge_sessions(self, limit: int = 30) -> list[dict]:
        """飞书桥跑过的会话：第一句话当标题，最后活动时间，目录。"""
        with self._lock:
            rows = self._db.execute(
                "SELECT t.session_id AS sid,"
                " (SELECT prompt FROM tasks a WHERE a.session_id = t.session_id ORDER BY a.id LIMIT 1) AS first_prompt,"
                " (SELECT cwd FROM tasks b WHERE b.session_id = t.session_id ORDER BY b.id DESC LIMIT 1) AS cwd,"
                " MAX(COALESCE(t.finished_at, t.started_at, t.created_at)) AS last"
                " FROM tasks t WHERE t.status IN ('done', 'failed', 'stopped') AND t.session_id != ''"
                " GROUP BY t.session_id ORDER BY last DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get(self, task_id: int) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def update(self, task_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))

    def find_by_message(self, msg_id: str) -> dict | None:
        """飞书消息 ID 是哪个任务的（你发的原消息、状态卡、结果卡都算）。"""
        if not msg_id:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM tasks WHERE msg_id=? OR card_msg_id=? OR result_msg_id=?"
                " ORDER BY id DESC LIMIT 1", (msg_id, msg_id, msg_id)).fetchone()
        return dict(row) if row else None

    def recent_same_prompt(self, open_id: str, prompt: str, since: float) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM tasks WHERE open_id=? AND prompt=? AND created_at>=?"
                " AND status != 'ignored' ORDER BY id DESC LIMIT 1", (open_id, prompt, since)).fetchone()
        return dict(row) if row else None

    def list_by_status(self, *statuses: str) -> list[dict]:
        marks = ",".join("?" * len(statuses))
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM tasks WHERE status IN ({marks}) ORDER BY id", statuses).fetchall()
        return [dict(r) for r in rows]

    def recent(self, limit: int = 5) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def undelivered(self, since: float) -> list[dict]:
        """跑完了但结果还没发出去的（飞书接口当时失败）。"""
        marks = ",".join("?" * 3)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM tasks WHERE status IN ({marks}) AND delivered=0 AND finished_at>=?"
                " ORDER BY id", ("done", "failed", "stopped", since)).fetchall()
        return [dict(r) for r in rows]

    # ---------- 审批 ----------
    def add_approval(self, approval_id: str, task_id: int, tool_name: str, tool_input: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO approvals (id, task_id, tool_name, tool_input, created_at)"
                " VALUES (?,?,?,?,?)", (approval_id, task_id, tool_name, tool_input, time.time()))

    def update_approval(self, approval_id: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE approvals SET {cols} WHERE id=?", (*fields.values(), approval_id))

    def get_approval(self, approval_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return dict(row) if row else None

    def get_approval_by_card(self, msg_id: str) -> dict | None:
        if not msg_id:
            return None
        with self._lock:
            row = self._db.execute("SELECT * FROM approvals WHERE card_msg_id=?", (msg_id,)).fetchone()
        return dict(row) if row else None

    # ---------- 键值 ----------
    def kv_get(self, k: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row[0] if row else default

    def kv_set(self, k: str, v: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                             (k, v))

    def close(self) -> None:
        with self._lock:
            self._db.close()
