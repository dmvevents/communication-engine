"""core/escalate.py — edge-triggered operator escalation (gate G11; R20).

The live probe fires once per minute. A level-triggered notifier would therefore emit
1,440 identical alerts a day for one stuck condition — and a monitor that spams is a
monitor that gets muted, which is worse than no monitor. The rule encoded here: notify on
a state CHANGE (an edge), never on a state (a level).

Two design points that are easy to lose:

  * State lives in sqlite, not in memory. The 1,440/day failure mode is specifically
    cron-shaped: every poll is a fresh process, so an in-memory "already alerted" flag
    resets each fire and dedupes nothing. Durability IS the requirement.
  * `notify` runs BEFORE the new state is committed — the opposite trade from the outbox.
    There, a duplicate send to a customer is the disaster; here, a duplicate operator page
    is an annoyance while a LOST page is an unwatched outage. If notify raises, the edge
    stays uncommitted and the next observation retries it.

Condition identity is the NAME alone. Details wobble (ages, percentages, counts); keying
dedupe on the detail string would defeat it entirely.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Callable

HEALTHY, DEGRADED = "healthy", "degraded"

SCHEMA = """
CREATE TABLE IF NOT EXISTS conditions (
    name        TEXT PRIMARY KEY,
    state       TEXT NOT NULL,
    detail      TEXT,
    changed_at  REAL NOT NULL
);
"""


class Escalator:
    def __init__(self, db_path: str | Path, notify: Callable[[str], None]):
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        # Injected so core stays channel-agnostic. Production wires this to whatever the
        # operator actually reads (e.g. a staged outbox draft) — never a direct send.
        self.notify = notify

    def observe(self, name: str, ok: bool, detail: str = "",
                now: float | None = None) -> bool:
        """Record one observation of a condition; notify IFF it is an edge.

        Returns True when a notification was emitted. The stored row remembers the last
        EDGE — levels do not touch it — so `changed_at` reads as "degraded since ...".
        """
        state = HEALTHY if ok else DEGRADED
        row = self.conn.execute(
            "SELECT state FROM conditions WHERE name=?", (name,)).fetchone()
        if row is not None and row["state"] == state:
            return False        # a level, not an edge — the 1,440-alerts/day trap
        if row is None and ok:
            # Bring-up of a healthy condition is baseline, not news.
            self._commit(name, state, detail, now)
            return False
        msg = f"{'RECOVERED' if ok else 'DEGRADED'}: {name}" + (
            f" — {detail}" if detail else "")
        self.notify(msg)
        # Committed strictly AFTER notify (see module docstring): a notify that raised
        # has not committed, so the edge fires again on the next observation.
        self._commit(name, state, detail, now)
        return True

    def _commit(self, name: str, state: str, detail: str, now: float | None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO conditions (name, state, detail, changed_at) "
            "VALUES (?,?,?,?)",
            (name, state, detail, time.time() if now is None else now))
        self.conn.commit()

    def state_of(self, name: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM conditions WHERE name=?", (name,)).fetchone()

    def close_db(self) -> None:
        self.conn.close()
