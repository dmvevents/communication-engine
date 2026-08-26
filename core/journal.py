"""core/journal.py — the idempotent audit journal (gate G10; requirements R16, R23).

Measured problem this replaces
------------------------------
The incumbent command log is append-only but **not idempotent**. Counted over its own file:
**323 entries for 177 distinct messages** — 77 messages appear more than once and one appears
**9 times**. Every fire that re-reads a window re-appends the same asks.

An audit trail that inflates is not an audit trail: you cannot count how many asks arrived,
measure time-to-first-response, or reconstruct what happened, because you cannot tell a
repeated ask from a repeated *log write*.

R23 — message REVISIONS (found by probing this module, not by reading it)
------------------------------------------------------------------------
Chat messages get edited, and every platform we care about supports it. The first version of
this journal treated any re-sighting as a duplicate, so an edit was silently discarded:

    "Notes from the meeting are in the doc."   -> recorded STATEMENT
    edited to "Please deploy the patched image now."
    -> journal still said STATEMENT, and still held the ORIGINAL text

Two harms. The audit **misquoted the channel**, and an edit that turns a remark into an
instruction was never acted on. Idempotence must therefore be keyed on *content*, not merely
on identity:

* same (channel, ts) and same text  -> a re-sighting: bump seen_count, change nothing else
* same (channel, ts), text CHANGED  -> a revision: keep full history, update the live row,
  re-classify, and make it visible that any earlier answer predates the edit
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    channel        TEXT NOT NULL,
    ts             TEXT NOT NULL,
    sender_id      TEXT,
    text           TEXT,
    text_hash      TEXT,
    kind           TEXT,
    reason         TEXT,
    matched        TEXT,
    routed         TEXT,
    first_seen_at  REAL NOT NULL,
    last_seen_at   REAL NOT NULL,
    seen_count     INTEGER NOT NULL DEFAULT 1,
    revision       INTEGER NOT NULL DEFAULT 1,
    responded_at   REAL,
    response_key   TEXT,
    PRIMARY KEY (channel, ts)
);
CREATE TABLE IF NOT EXISTS revisions (
    channel     TEXT NOT NULL,
    ts          TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    text        TEXT,
    kind        TEXT,
    reason      TEXT,
    matched     TEXT,
    recorded_at REAL NOT NULL,
    PRIMARY KEY (channel, ts, seq)
);
"""


def _hash(text: str | None) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()


@dataclass
class RecordResult:
    """Truthy only for a first sighting, so `if journal.record(...)` still reads naturally."""
    status: str        # "new" | "reseen" | "revised"
    revision: int = 1

    @property
    def is_new(self) -> bool:
        return self.status == "new"

    @property
    def is_revision(self) -> bool:
        return self.status == "revised"

    def __bool__(self) -> bool:
        return self.is_new


class Journal:
    def __init__(self, db_path: str | Path):
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        """Adopters hold journal.db files created before the cues column existed (R22),
        and CREATE TABLE IF NOT EXISTS never alters an existing table. Refusing such a
        file would destroy an audit trail in order to improve it — add the column in
        place instead; legacy rows read back as None, never as a fabricated decision."""
        for table in ("journal", "revisions"):
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if "matched" not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN matched TEXT")

    def record(self, channel: str, ts: str, sender_id: str | None = None,
               text: str = "", kind: str | None = None, reason: str | None = None,
               matched: list | None = None, routed: str | None = None) -> RecordResult:
        """Record an inbound message. Idempotent on IDENTITY and sensitive to CONTENT.

        Returns a RecordResult whose truthiness is "this is the first sighting", so callers
        can still write `if journal.record(...)`. Check `.is_revision` to re-route an edit.

        `matched` is the classification's cue list (R22): the evidence that makes the
        recorded decision disputable. [] means "the classifier matched nothing"; None
        means "no decision supplied" — the two must stay distinct on disk.
        """
        now = time.time()
        h = _hash(text)
        mjson = None if matched is None else json.dumps(list(matched))
        existing = self.get(channel, ts)

        if existing is None:
            self.conn.execute(
                "INSERT INTO journal (channel, ts, sender_id, text, text_hash, kind, reason, "
                "matched, routed, first_seen_at, last_seen_at, seen_count, revision) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,1,1)",
                (channel, str(ts), sender_id, text, h, kind, reason, mjson, routed,
                 now, now))
            self._add_revision(channel, ts, 1, text, kind, reason, mjson, now)
            self.conn.commit()
            return RecordResult("new", 1)

        # A revision is only claimed when the caller actually supplied text. A bare
        # re-sighting (text="") must not be mistaken for an edit-to-empty.
        edited = bool(text) and existing["text_hash"] not in (None, h)
        if edited:
            rev = int(existing["revision"]) + 1
            self.conn.execute(
                "UPDATE journal SET text=?, text_hash=?, kind=COALESCE(?, kind), "
                "reason=COALESCE(?, reason), matched=COALESCE(?, matched), "
                "routed=COALESCE(?, routed), "
                "last_seen_at=?, seen_count=seen_count+1, revision=? "
                "WHERE channel=? AND ts=?",
                (text, h, kind, reason, mjson, routed, now, rev, channel, str(ts)))
            self._add_revision(channel, ts, rev, text, kind, reason, mjson, now)
            self.conn.commit()
            return RecordResult("revised", rev)

        self.conn.execute(
            "UPDATE journal SET last_seen_at=?, seen_count=seen_count+1, "
            # a later pass may refine the classification; keep the newest non-null
            "kind=COALESCE(?, kind), reason=COALESCE(?, reason), routed=COALESCE(?, routed), "
            "matched=COALESCE(?, matched) "
            "WHERE channel=? AND ts=?",
            (now, kind, reason, routed, mjson, channel, str(ts)))
        self.conn.commit()
        return RecordResult("reseen", int(existing["revision"]))

    def _add_revision(self, channel, ts, seq, text, kind, reason, mjson, when):
        self.conn.execute(
            "INSERT OR REPLACE INTO revisions (channel, ts, seq, text, kind, reason, "
            "matched, recorded_at) VALUES (?,?,?,?,?,?,?,?)",
            (channel, str(ts), seq, text, kind, reason, mjson, when))

    def audit(self, channel: str, ts: str) -> dict | None:
        """The audit link (R22): from a journal row back to the decision that classified
        it — kind, reason, and the cues that matched, decoded for the caller. `matched`
        is None for rows journaled before cue recording existed; an absent record must
        never masquerade as "the classifier matched nothing"."""
        row = self.get(channel, ts)
        if row is None:
            return None
        return {"kind": row["kind"], "reason": row["reason"],
                "matched": None if row["matched"] is None else json.loads(row["matched"]),
                "revision": row["revision"]}

    def revisions(self, channel: str, ts: str) -> list:
        """Per-edit history, oldest first, as plain dicts — this is the RUNBOOK's audit
        walk, read by a human disputing a decision, and a list of raw sqlite3.Row
        objects prints as '[<sqlite3.Row object at 0x...>]' showing none of the
        history it holds (ENH-22)."""
        rows = self.conn.execute(
            "SELECT * FROM revisions WHERE channel=? AND ts=? ORDER BY seq",
            (channel, str(ts))).fetchall()
        return [dict(r) for r in rows]

    def edited_after_response(self) -> list:
        """Messages edited AFTER we answered them — any earlier reply may now be wrong.

        This is the audit question an edit creates: we answered version 1, the channel now
        shows version 2, and nobody has looked at the difference.
        """
        return self.conn.execute(
            "SELECT j.* FROM journal j JOIN revisions r "
            "ON r.channel=j.channel AND r.ts=j.ts "
            "WHERE j.responded_at IS NOT NULL AND r.recorded_at > j.responded_at "
            "GROUP BY j.channel, j.ts").fetchall()

    def mark_responded(self, channel: str, ts: str, response_key: str) -> None:
        """Tie an inbound ask to the outbox key that answered it."""
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

    def answered(self, channel: str | None = None) -> list:
        """The closable complement of unanswered(): rows already tied to a response.
        The reference scheduler closes message-routed owed work from this set — by
        re-deriving each row's owed id, never by parsing ids back apart."""
        q = "SELECT * FROM journal WHERE responded_at IS NOT NULL"
        args: tuple = ()
        if channel:
            q += " AND channel=?"
            args = (channel,)
        return self.conn.execute(q + " ORDER BY ts", args).fetchall()

    def by_kind(self) -> dict:
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT kind, count(*) FROM journal GROUP BY kind")}

    def export_jsonl(self) -> str:
        """One line per DISTINCT message, never per sighting."""
        return "\n".join(json.dumps({k: r[k] for k in r.keys()})
                         for r in self.conn.execute("SELECT * FROM journal ORDER BY ts"))

    def close(self) -> None:
        self.conn.close()
