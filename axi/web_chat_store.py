"""Persistent chat history storage for the web frontend.

Uses SQLite to store messages per agent. Each message includes role, text,
timestamp, source, and optional stream event type. Messages are stored in
insertion order and retrieved per agent.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any


class ChatStore:
    """Thread-safe SQLite chat history store."""

    MAX_MESSAGES_PER_AGENT = 200  # Rolling window per agent

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent TEXT NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                ts REAL NOT NULL,
                source TEXT,
                stream_event_type TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_agent ON messages(agent)")
        conn.commit()

    def add_message(
        self,
        agent: str,
        role: str,
        text: str,
        ts: float | None = None,
        source: str | None = None,
        stream_event_type: str | None = None,
    ) -> None:
        """Insert a message and trim old messages if over the limit."""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO messages (agent, role, text, ts, source, stream_event_type) VALUES (?, ?, ?, ?, ?, ?)",
            (agent, role, text, ts or time.time(), source, stream_event_type),
        )
        conn.commit()
        self._trim(conn, agent)

    def get_history(self, agent: str, limit: int = 100) -> list[dict[str, Any]]:
        """Get the most recent messages for an agent."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT role, text, ts, source, stream_event_type FROM messages WHERE agent = ? ORDER BY id DESC LIMIT ?",
            (agent, limit),
        ).fetchall()
        # Reverse to chronological order
        return [
            {
                "role": row["role"],
                "text": row["text"],
                "ts": row["ts"],
                "source": row["source"],
                "stream_event_type": row["stream_event_type"],
            }
            for row in reversed(rows)
        ]

    def get_all_agents_history(self, limit_per_agent: int = 100) -> dict[str, list[dict[str, Any]]]:
        """Get history for all agents."""
        conn = self._get_conn()
        agents = [row[0] for row in conn.execute("SELECT DISTINCT agent FROM messages").fetchall()]
        return {agent: self.get_history(agent, limit_per_agent) for agent in agents}

    def _trim(self, conn: sqlite3.Connection, agent: str) -> None:
        """Keep only the most recent MAX_MESSAGES_PER_AGENT messages per agent."""
        count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE agent = ?", (agent,)
        ).fetchone()[0]
        if count > self.MAX_MESSAGES_PER_AGENT:
            excess = count - self.MAX_MESSAGES_PER_AGENT
            conn.execute(
                "DELETE FROM messages WHERE id IN (SELECT id FROM messages WHERE agent = ? ORDER BY id ASC LIMIT ?)",
                (agent, excess),
            )
            conn.commit()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn:
            conn.close()
            self._local.conn = None
