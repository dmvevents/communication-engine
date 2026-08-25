"""core/journal.py — the idempotent audit journal (gate G10; requirement R16).

Measured problem this replaces
------------------------------
The incumbent command log is append-only but **not idempotent**. Counted over its own file:
**323 entries for 177 distinct messages** — 77 messages appear more than once and one appears
**9 times**. Every fire that re-reads a window re-appends the same asks.

An audit trail that inflates is not an audit trail: you cannot count how many asks arrived,
measure time-to-first-response, or reconstruct what happened, because you cannot tell a
repeated ask from a repeated *log write*.

Design
------
* one row per (channel, message ts) — recording is idempotent, so replaying a window is free
* the row is UPDATED, never duplicated, when a message is re-seen (last_seen_at advances and
  seen_count increments, so re-processing is observable without inflating the ask count)
* `distinct_count() == row_count()` is an invariant a test defends
* the append path records the classification and the routing decision, because the audit
  question is never just "what arrived" but "what did we decide, and did we act"
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    channel       TEXT NOT NULL,
    ts            TEXT NOT NULL,
    sender_id     TEXT,
    text          TEXT,
    kind          TEXT,
    reason        TEXT,
    routed        TEXT,
    first_seen_at REAL NOT NULL,
    last_seen_at  REAL NOT NULL,
    seen_count    INTEGER NOT NULL DEFAULT 1,
    responded_at  REAL,
    response_key  TEXT,
    PRIMARY KEY (channel, ts)
);
"""


class Journal:
    def __init__(self, db_path: str | Path):
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def record(self, channel: str, ts: str, sender_id: str | None = None,
               text: str = "", kind: str | None = None, reason: str | None = None,
               routed: str | None = None) -> bool:
        """Record an inbound message. Returns True if this is the FIRST sighting.

        Re-recording the same (channel, ts) updates last_seen_at and increments seen_count.
        It never appends a second row, which is the whole point.
        """
        now = time.time()
        existing = self.get(channel, ts)
        if existing is None:
            self.conn.execute(
                "INSERT INTO journal (channel, ts, sender_id, text, kind, reason, routed, "
                "first_seen_at, last_seen_at, seen_count) VALUES (?,?,?,?,?,?,?,?,?,1)",
                (channel, str(ts), sender_id, text, kind, reason, routed, now, now))
            self.conn.commit()
            return True
        self.conn.execute(
            "UPDATE journal SET last_seen_at=?, seen_count=seen_count+1, "
            # a later pass may refine the classification/routing; keep the newest non-null
            "kind=COALESCE(?, kind), reason=COALESCE(?, reason), routed=COALESCE(?, routed) "
            "WHERE channel=? AND ts=?",
            (now, kind, reason, routed, channel, str(ts)))
        self.conn.commit()
        return False

    def mark_responded(self, channel: str, ts: str, response_key: str) -> None:
        """Tie an inbound ask to the outbox key that answered it.

        Without this link the audit cannot answer 'which asks are still unanswered', which
        is the question the owed-work edge depends on.
        """
        self.conn.execute(
            "UPDATE journal SET responded_at=?, response_key=? WHERE channel=? AND ts=?",
            (time.time(), response_key, channel, str(ts)))
        self.conn.commit()

    def get(self, channel: str, ts: str):
        return self.conn.execute(
            "SELECT * FROM journal WHERE channel=? AND ts=?", (channel, str(ts))).fetchone()

    def row_count(self) -> int:
        return self.conn.execute("SELECT count(*) FROM journal").fetchone()[0]

    def distinct_count(self) -> int:
        return self.conn.execute(
            "SELECT count(*) FROM (SELECT DISTINCT channel, ts FROM journal)").fetchone()[0]

    def unanswered(self, channel: str | None = None) -> list:
        q = "SELECT * FROM journal WHERE responded_at IS NULL"
        args: tuple = ()
        if channel:
            q += " AND channel=?"
            args = (channel,)
        return self.conn.execute(q + " ORDER BY ts", args).fetchall()

    def by_kind(self) -> dict:
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT kind, count(*) FROM journal GROUP BY kind")}

    def export_jsonl(self) -> str:
        """Human/tooling-readable dump — one line per DISTINCT message, never per sighting."""
        out = []
        for r in self.conn.execute("SELECT * FROM journal ORDER BY ts"):
            out.append(json.dumps({k: r[k] for k in r.keys()}))
        return "\n".join(out)

    def close(self) -> None:
        self.conn.close()
