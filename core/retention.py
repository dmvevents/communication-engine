"""core/retention.py — deletion detection (requirement R26, gate G1).

This module exists to close a hole that `core/parity.py`'s accept-list opened.

Classification made R8 honest: 342 divergent rows on the first live window were messages
the platform had **deleted** since the incumbent archived them, so the run accepts
`UNRETRIEVABLE` and goes green (see state/parity/R8-DIVERGENCE-EXPLAINED.md). But accepting
a class wholesale means its *growth* is invisible: if the platform stopped serving ten
thousand more rows tomorrow, every parity run would still report OK. A deletion would be a
silent state change — exactly the shape this repo keeps finding and killing.

So a deletion becomes an EVENT, not a class. The platform snapshot is already archived per
run (`snapshots/served-*.json`), so consecutive snapshots answer a question neither one can
answer alone: **which rows that we hold were retrievable last time and are not now?**

    newly_unretrievable = stored ∩ previous_served − current_served

Two refusals, both the same discipline as `checks.Verdict.passed` and `parity.compare`:

  * an EMPTY previous snapshot cannot conclude "nothing was deleted" — there is nothing to
    have lost. That is the vacuous pass, so it raises.
  * an EMPTY current snapshot looks identical to "every message was deleted", but the far
    likelier cause is that we lost read access or the call failed. Reporting a mass deletion
    would page an operator to the wrong incident, so it raises too.

Tombstones are durable and first-write-wins (the idiom `store.arrivals` uses): the instant a
row stops being retrievable is a fact about the past, and a later re-run must never move it.
Nothing is ever deleted from the engine's store here — the store is an archive, and a
tombstone records that the platform's copy is gone WITHOUT destroying ours. That distinction
is the whole point: an operator answering a deletion request needs to know which rows are
affected, and an auditor needs to see that the engine knew.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tombstones (
    channel_id  TEXT NOT NULL,
    ts          TEXT NOT NULL,
    detected_at REAL NOT NULL,
    PRIMARY KEY (channel_id, ts)
);
"""


class RetentionError(RuntimeError):
    """The deletion comparison could not be performed. Never downgrade to 'none found'."""


def read_snapshot(path: str | Path) -> set[str]:
    """Load one archived platform snapshot (a JSON list of timestamps)."""
    try:
        raw = json.loads(Path(path).read_text())
    except OSError as ex:
        raise RetentionError(f"cannot read snapshot {path}: {ex}") from ex
    except ValueError as ex:
        raise RetentionError(f"snapshot {path} is not valid JSON: {ex}") from ex
    if isinstance(raw, dict):
        raw = [ts for tss in raw.values() for ts in tss]
    if not isinstance(raw, list):
        raise RetentionError(f"snapshot {path} must be a JSON list of timestamps, "
                             f"got {type(raw).__name__}")
    return {str(t) for t in raw}


def newly_unretrievable(previous_served, current_served, stored_ts) -> set[str]:
    """Rows WE hold that the platform served last time and does not serve now.

    Scoped to rows the engine actually stored: a message deleted upstream that we never
    ingested changes nothing about our archive and is already `UNRETRIEVABLE` to the differ.
    The ones that matter are the rows now sitting in our store with no platform counterpart.
    """
    previous_served = set(previous_served)
    current_served = set(current_served)
    if not previous_served:
        raise RetentionError(
            "the previous platform snapshot is empty — 'nothing was deleted' would be "
            "vacuous, because there is nothing recorded to have lost")
    if not current_served:
        raise RetentionError(
            "the current platform snapshot is empty — that is indistinguishable from "
            "'every message was deleted', and the likelier cause is lost read access or a "
            "failed call; refusing to report a mass deletion on it")
    return (set(stored_ts) & previous_served) - current_served


class Tombstones:
    """Durable record of when each row stopped being retrievable. Never deletes messages."""

    def __init__(self, db_path: str | Path, clock=time.time):
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._clock = clock

    def record(self, channel_id: str, timestamps) -> int:
        """Stamp each row's first-missing instant. Returns how many were NEW.

        INSERT OR IGNORE, so a row already tombstoned keeps its original instant however
        often the reconciliation re-runs — the moment the platform's copy vanished is a
        fact about the past, and re-running the check must not rewrite history.
        """
        now = self._clock()
        new = 0
        for ts in timestamps:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO tombstones (channel_id, ts, detected_at) "
                "VALUES (?,?,?)", (channel_id, str(ts), now))
            new += cur.rowcount
        self.conn.commit()
        return new

    def all_for(self, channel_id: str) -> dict[str, float]:
        return {r["ts"]: r["detected_at"] for r in self.conn.execute(
            "SELECT ts, detected_at FROM tombstones WHERE channel_id=?", (channel_id,))}

    def count(self, channel_id: str | None = None) -> int:
        if channel_id is None:
            return self.conn.execute("SELECT count(*) FROM tombstones").fetchone()[0]
        return self.conn.execute(
            "SELECT count(*) FROM tombstones WHERE channel_id=?", (channel_id,)).fetchone()[0]

    def close(self):
        self.conn.close()


def reconcile(channel_id: str, previous_snapshot, current_snapshot, stored_ts,
              tombstones: Tombstones | None = None) -> dict:
    """Detect deletions since the previous snapshot and tombstone the new ones.

    Returns `{"newly_unretrievable": set, "newly_tombstoned": int, "total_tombstoned": int}`
    — `newly_unretrievable` is what a check judges, while `newly_tombstoned` counts only
    rows never seen missing before, so a persistent deletion does not re-alert every fire
    (the level-vs-edge rule `core/escalate.py` exists for).
    """
    gone = newly_unretrievable(previous_snapshot, current_snapshot, stored_ts)
    recorded = tombstones.record(channel_id, gone) if tombstones else 0
    return {
        "newly_unretrievable": gone,
        "newly_tombstoned": recorded,
        "total_tombstoned": tombstones.count(channel_id) if tombstones else 0,
    }
