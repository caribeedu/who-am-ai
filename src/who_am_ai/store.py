"""SQLite state: transcript cursors, the observation ledger, profile items, versions, changelog and spend."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    stem TEXT PRIMARY KEY, path TEXT NOT NULL, offset INTEGER NOT NULL DEFAULT 0, mtime REAL
);
CREATE TABLE IF NOT EXISTS seen (message_id TEXT PRIMARY KEY);
-- The ledger: every preference ever observed, with its evidence. It only grows; the profile is rebuilt from it.
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,              -- owner (edited me.md) | observed (from a chat) | imported
    preference TEXT NOT NULL,
    quote TEXT,
    topic TEXT,
    chat TEXT,
    ts TEXT NOT NULL,                -- when it was said
    consumed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
-- The current profile, as items. Rendering into me.md (and the word cap) is done by code.
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    section TEXT NOT NULL,
    text TEXT NOT NULL,
    example TEXT,
    source TEXT NOT NULL,            -- strongest kind among its observations
    observation_ids TEXT NOT NULL,   -- JSON list
    support INTEGER NOT NULL,
    chats INTEGER NOT NULL,
    last_seen TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS versions (id INTEGER PRIMARY KEY, written_at TEXT NOT NULL, content TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS changelog (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, summary TEXT, added TEXT, removed TEXT, changed TEXT
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, step TEXT NOT NULL, model TEXT NOT NULL,
    input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL NOT NULL DEFAULT 0, ok INTEGER NOT NULL, error TEXT
);
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, note TEXT NOT NULL);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.db.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        return self.db.execute(sql, args).fetchone()

    def x(self, sql: str, args: tuple = ()) -> int:
        cur = self.db.execute(sql, args)
        self.db.commit()
        return cur.lastrowid

    # ---------------------------------------------------------------- ledger

    def add_observation(self, kind: str, preference: str, quote: str | None, topic: str | None, chat: str | None,
                        ts: str) -> int:
        return self.x("""INSERT INTO observations (kind, preference, quote, topic, chat, ts, created_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?)""", (kind, preference, quote, topic, chat, ts, now()))

    def pending_observations(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM observations WHERE consumed = 0 ORDER BY id")

    def active_items(self) -> list[dict]:
        rows = self.q("SELECT * FROM items WHERE active = 1 ORDER BY id")
        return [{**dict(r), "observation_ids": json.loads(r["observation_ids"])} for r in rows]

    def last_version(self) -> str | None:
        row = self.one("SELECT content FROM versions ORDER BY id DESC LIMIT 1")
        return row["content"] if row else None

    def month_spend(self) -> float:
        start = datetime.now(timezone.utc).strftime("%Y-%m-01")
        return float(self.one("SELECT coalesce(sum(cost_usd), 0) AS c FROM llm_calls WHERE ts >= ?", (start,))["c"])

    def log_run(self, note: str) -> None:
        self.x("INSERT INTO runs (ts, note) VALUES (?, ?)", (now(), note))
