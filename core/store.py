"""core/store.py — the channel-agnostic message store.

Generalises the live schema (origin: slack/db.py) so any adapter's messages land in one
place. Deliberately boring: sqlite, explicit schema, no ORM.

Two properties are load-bearing and each is pinned by a test that fails if the property
is removed (see tests/test_store.py):

R9  idempotent re-ingest — the live poller depends on gap-free re-polling, so upserting
    the same batch twice must not change the row count or duplicate a message.
R5  the schema is PINNED — the origin system shipped a health check that read a field
    name (`.timestamp`) the events never carried (`.ts`), so the check silently emitted
    neither PASS nor FAIL for weeks. A renamed or missing field must raise here, loudly,
    at ingest time.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

# The normalized message contract (see channels/CONTRACT.md). Renaming or dropping any
# of these is a breaking change and MUST fail loudly rather than silently store nothing.
REQUIRED_FIELDS = ("channel_type", "channel_id", "sender_id", "ts", "text")
OPTIONAL_FIELDS = ("sender_name", "thread_id", "raw", "attachments")
MESSAGE_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    channel_type TEXT NOT NULL,
    channel_id   TEXT NOT NULL,
    ts           TEXT NOT NULL,
    sender_id    TEXT NOT NULL,
    sender_name  TEXT,
    text         TEXT,
    thread_id    TEXT,
    raw          TEXT,
    attachments  TEXT,
    PRIMARY KEY (channel_type, channel_id, ts)
);
CREATE TABLE IF NOT EXISTS cursors (
    instance     TEXT NOT NULL,
    channel_id   TEXT NOT NULL,
    cursor       TEXT NOT NULL,
    updated_at   TEXT,
    PRIMARY KEY (instance, channel_id)
);
CREATE TABLE IF NOT EXISTS arrivals (
    channel_type TEXT NOT NULL,
    channel_id   TEXT NOT NULL,
    ts           TEXT NOT NULL,
    arrived_at   REAL NOT NULL,
    PRIMARY KEY (channel_type, channel_id, ts)
);
"""


class SchemaError(ValueError):
    """A message did not match the pinned contract. Never swallow this."""


def _attachments_json(atts):
    """None stays NULL and a known-empty list stays "[]" — the same None-vs-[]
    distinction the journal keeps for cues (R22): a row that predates the field must
    never masquerade as 'the adapter looked and found no attachments'."""
    return None if atts is None else json.dumps(atts)


class Store:
    def __init__(self, path: str | Path, clock=time.time):
        # `clock` stamps first arrivals (ENH-2); injectable so latency tests are
        # deterministic. Wall clock, same basis as the platform's message ts.
        self.path = str(path)
        self.clock = clock
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        """Adopters hold messages.db files created before the attachments column
        existed (ENH-4), and CREATE TABLE IF NOT EXISTS never alters an existing
        table — the journal hit the same wall with the R22 cues column. Add the
        column in place; legacy rows read back as None, never as a fabricated
        empty attachment list."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(messages)")}
        if "attachments" not in cols:
            self.conn.execute("ALTER TABLE messages ADD COLUMN attachments TEXT")

    # ---- messages ---------------------------------------------------------
    @staticmethod
    def validate(msg: dict) -> None:
        """Raise SchemaError unless msg carries exactly the pinned contract.

        Both directions matter. A MISSING required field is the `.timestamp` bug. An
        UNKNOWN field is a silent drift the other way: the producer thinks it is storing
        something that never lands.
        """
        if not isinstance(msg, dict):
            raise SchemaError(f"message must be a dict, got {type(msg).__name__}")
        missing = [f for f in REQUIRED_FIELDS if f not in msg or msg[f] is None]
        if missing:
            raise SchemaError(f"missing required field(s): {missing}")
        unknown = [k for k in msg if k not in MESSAGE_FIELDS]
        if unknown:
            raise SchemaError(f"unknown field(s) not in the pinned contract: {unknown}")
        atts = msg.get("attachments")
        if atts is not None and not isinstance(atts, list):
            # A lone dict or a pre-encoded JSON string would persist as junk and answer
            # the classifier's has-attachments question with its truthiness (ENH-4).
            raise SchemaError(f"attachments must be a list of attachment descriptors, "
                              f"got {type(atts).__name__}")

    def upsert_messages(self, messages) -> int:
        """Idempotent ingest. Returns the number of rows accepted (not necessarily new).

        Every message is validated BEFORE any write, so a bad batch cannot half-land.
        """
        batch = list(messages)
        for m in batch:
            self.validate(m)
        rows = [
            (m["channel_type"], m["channel_id"], str(m["ts"]), m["sender_id"],
             m.get("sender_name"), m.get("text"), m.get("thread_id"), m.get("raw"),
             _attachments_json(m.get("attachments")))
            for m in batch
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO messages "
            "(channel_type, channel_id, ts, sender_id, sender_name, text, thread_id, "
            "raw, attachments) VALUES (?,?,?,?,?,?,?,?,?)", rows)
        # First-arrival stamp (ENH-2): OR IGNORE, never OR REPLACE — the poller re-reads
        # overlapping windows on every cycle (R9), and a stamp that followed the latest
        # sighting would erase the push-vs-poll delta the detection-latency SLO measures.
        now = self.clock()
        self.conn.executemany(
            "INSERT OR IGNORE INTO arrivals "
            "(channel_type, channel_id, ts, arrived_at) VALUES (?,?,?,?)",
            [(m["channel_type"], m["channel_id"], str(m["ts"]), now) for m in batch])
        self.conn.commit()
        return len(rows)

    def count(self, channel_id: str | None = None) -> int:
        if channel_id is None:
            return self.conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        return self.conn.execute(
            "SELECT count(*) FROM messages WHERE channel_id=?", (channel_id,)).fetchone()[0]

    def timestamps(self, channel_id: str) -> set[str]:
        return {r[0] for r in self.conn.execute(
            "SELECT ts FROM messages WHERE channel_id=?", (channel_id,))}

    def arrivals(self, channel_id: str) -> dict[str, float]:
        """ts -> first-arrival wall-clock time, for the detection-latency SLO."""
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT ts, arrived_at FROM arrivals WHERE channel_id=?", (channel_id,))}

    # ---- cursors ----------------------------------------------------------
    def cursor_get(self, instance: str, channel_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT cursor FROM cursors WHERE instance=? AND channel_id=?",
            (instance, channel_id)).fetchone()
        return row[0] if row else None

    def cursor_set(self, instance: str, channel_id: str, cursor: str,
                   updated_at: str | None = None) -> None:
        """Cursors are OPAQUE strings owned by the adapter — never parsed here.

        NOTE for G2: persisting a cursor is the second half of a dual-write with the
        send. The origin system reconciled that race 24 times. Nothing in this module
        may be treated as making a send+commit atomic; the outbox owns that.
        """
        self.conn.execute(
            "INSERT OR REPLACE INTO cursors (instance, channel_id, cursor, updated_at) "
            "VALUES (?,?,?,?)", (instance, channel_id, cursor, updated_at))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
