"""Persistent memory for Jarvis: a conversation log plus durable facts.

Two layers, deliberately separate:

* `messages` - the raw transcript. Cheap, append-only, used to reconstruct the
  tail of a conversation so Jarvis picks up where he left off.
* `facts`    - distilled things worth keeping forever. Written by the model via
  the `remember` tool, or by the user via `/remember`. These are injected into
  every system prompt, so they stay small on purpose.
"""

import re
import sqlite3
import time
from typing import Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
"""

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "be",
    "of", "to", "in", "on", "for", "with", "my", "me", "i", "you", "it",
    "that", "this", "what", "who", "do", "does", "did", "about", "his", "her",
    "their", "them", "they",
}


class Memory:
    def __init__(self, path=None):
        self.path = str(path or config.DB_PATH)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.session_id: Optional[int] = None

    # -- sessions ---------------------------------------------------------
    def start_session(self) -> int:
        cur = self.conn.execute(
            "INSERT INTO sessions (started_at) VALUES (?)", (time.time(),)
        )
        self.conn.commit()
        self.session_id = cur.lastrowid
        return self.session_id

    def last_session_id(self) -> Optional[int]:
        row = self.conn.execute(
            "SELECT id FROM sessions WHERE id != COALESCE(?, -1) "
            "ORDER BY id DESC LIMIT 1",
            (self.session_id,),
        ).fetchone()
        return row["id"] if row else None

    # -- transcript -------------------------------------------------------
    def log(self, role: str, content: str) -> None:
        if not content or self.session_id is None:
            return
        self.conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            (self.session_id, role, content, time.time()),
        )
        self.conn.commit()

    def recent(self, limit: int, session_id: Optional[int] = None) -> list[dict]:
        """Most recent `limit` messages of a session, oldest first."""
        sid = session_id if session_id is not None else self.session_id
        if sid is None:
            return []
        rows = self.conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (sid, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def carry_over(self, limit: int) -> list[dict]:
        """Tail of the *previous* session, so a fresh start still has thread."""
        prev = self.last_session_id()
        return self.recent(limit, session_id=prev) if prev else []

    # -- durable facts ----------------------------------------------------
    def remember(self, text: str) -> str:
        text = " ".join(text.split()).strip()
        if not text:
            return "Nothing to remember."
        try:
            self.conn.execute(
                "INSERT INTO facts (text, created_at) VALUES (?, ?)",
                (text, time.time()),
            )
            self.conn.commit()
            return f"Stored: {text}"
        except sqlite3.IntegrityError:
            return f"Already knew that: {text}"

    def forget(self, fact_id: int) -> str:
        cur = self.conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
        self.conn.commit()
        return "Forgotten." if cur.rowcount else f"No fact with id {fact_id}."

    def all_facts(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, text FROM facts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def search_facts(self, query: str, limit: int = 10) -> list[dict]:
        """Token-overlap search. Crude, but the fact table is small by design."""
        tokens = [
            t for t in re.findall(r"[a-z0-9']+", query.lower())
            if t not in _STOPWORDS and len(t) > 2
        ]
        rows = self.all_facts(limit=500)
        if not tokens:
            return rows[:limit]

        scored = []
        for row in rows:
            low = row["text"].lower()
            score = sum(1 for t in tokens if t in low)
            if score:
                scored.append((score, row))
        scored.sort(key=lambda pair: -pair[0])
        return [row for _, row in scored[:limit]]

    def stats(self) -> dict:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]
        return {
            "facts": q("SELECT COUNT(*) FROM facts"),
            "messages": q("SELECT COUNT(*) FROM messages"),
            "sessions": q("SELECT COUNT(*) FROM sessions"),
        }

    def close(self) -> None:
        self.conn.close()
