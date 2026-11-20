"""
메모 및 일정 관리 모듈 (SQLite 기반)
"""
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional


class DatabaseManager:
    """SQLite로 메모와 일정을 로컬 저장"""

    def __init__(self, db_path: str = "data/assistant.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init_db()

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS memos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    content TEXT,
                    tags TEXT,
                    created_at TEXT DEFAULT (datetime('now', 'localtime')),
                    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
                );

                CREATE TABLE IF NOT EXISTS schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT,
                    start_time TEXT NOT NULL,
                    end_time TEXT,
                    is_all_day INTEGER DEFAULT 0,
                    reminder_min INTEGER DEFAULT 15,
                    created_at TEXT DEFAULT (datetime('now', 'localtime'))
                );

                CREATE TABLE IF NOT EXISTS chat_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    role TEXT,
                    content TEXT,
                    provider TEXT,
                    created_at TEXT DEFAULT (datetime('now', 'localtime'))
                );
            """)

    # ── 메모 ──────────────────────────────────────────

    def add_memo(self, title: str, content: str, tags: str = "") -> int:
        with self._get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO memos (title, content, tags) VALUES (?, ?, ?)",
                (title, content, tags)
            )
            return cur.lastrowid

    def get_memos(self, search: str = "") -> list[dict]:
        with self._get_conn() as conn:
            if search:
                rows = conn.execute(
                    "SELECT * FROM memos WHERE title LIKE ? OR content LIKE ? ORDER BY updated_at DESC",
                    (f"%{search}%", f"%{search}%")
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM memos ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]

    def update_memo(self, memo_id: int, title: str, content: str, tags: str = ""):
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE memos SET title=?, content=?, tags=?, updated_at=datetime('now','localtime') WHERE id=?",
                (title, content, tags, memo_id)
            )

    def delete_memo(self, memo_id: int):
        with self._get_conn() as conn:
            conn.execute("DELETE FROM memos WHERE id=?", (memo_id,))

    # ── 일정 ──────────────────────────────────────────

    def add_schedule(self, title: str, start_time: str, end_time: str = None,
                     description: str = "", is_all_day: bool = False, reminder_min: int = 15) -> int:
        with self._get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO schedules (title, description, start_time, end_time, is_all_day, reminder_min)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (title, description, start_time, end_time, int(is_all_day), reminder_min)
            )
            return cur.lastrowid

    def get_schedules(self, date: str = None) -> list[dict]:
        with self._get_conn() as conn:
            if date:
                rows = conn.execute(
                    "SELECT * FROM schedules WHERE start_time LIKE ? ORDER BY start_time",
                    (f"{date}%",)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM schedules ORDER BY start_time").fetchall()
        return [dict(r) for r in rows]

    def get_upcoming_schedules(self, limit: int = 5) -> list[dict]:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM schedules WHERE start_time >= ? ORDER BY start_time LIMIT ?",
                (now, limit)
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_schedule(self, schedule_id: int):
        with self._get_conn() as conn:
            conn.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))

    # ── 대화 기록 ──────────────────────────────────────

    def save_chat(self, session_id: str, role: str, content: str, provider: str):
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO chat_history (session_id, role, content, provider) VALUES (?, ?, ?, ?)",
                (session_id, role, content, provider)
            )

    def get_chat_sessions(self) -> list[str]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT session_id, MIN(created_at) as first_msg FROM chat_history GROUP BY session_id ORDER BY first_msg DESC LIMIT 20"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_chat_by_session(self, session_id: str) -> list[dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM chat_history WHERE session_id=? ORDER BY created_at",
                (session_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_memo_context(self) -> str:
        """AI에게 제공할 메모/일정 컨텍스트 생성"""
        memos = self.get_memos()[:10]
        upcoming = self.get_upcoming_schedules(5)
        
        context = ""
        if upcoming:
            context += "📅 다가오는 일정:\n"
            for s in upcoming:
                context += f"  - {s['start_time']}: {s['title']}\n"
        if memos:
            context += "\n📝 최근 메모:\n"
            for m in memos[:5]:
                context += f"  - [{m['title']}]: {m['content'][:100]}...\n"
        return context
