"""core/owed.py — the owed-work edge (gate G3; requirements R3, R4).

Root cause of a sev-high incident: a customer EXEC-REQUEST with a stated deadline was
ACKed, a "NEXT STEP" was written to a file, and then **nothing ran for 8h17m**. The loop
advanced only when a new inbound message arrived, so promised-but-unstarted work was
invisible. Worse, idle backoff widened the polling interval, so the system looked *more*
idle the longer it was failing.

The two structural fixes here:

  R4  a **goal-triggered edge**: `unattended()` finds owed work whose driver is not alive,
      with no reference to inbound messages at all.
  R3  **backoff must never suppress owed work**: `should_fire()` returns True whenever
      unattended work exists, regardless of how deep the backoff is.

The lesson encoded: *"a NEXT STEP written into a file is inert — only a live process makes
progress."* So owed work records a driver, and a driver that is not alive means the work is
unattended, not in-progress.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS owed (
    id          TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    deadline    REAL,
    driver      TEXT,
    created_at  REAL NOT NULL,
    closed_at   REAL
);
"""


class OwedRegistry:
    def __init__(self, db_path: str | Path,
                 driver_alive: Callable[[str], bool] | None = None):
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        # Injected so tests can model a dead driver; production passes a real liveness probe.
        self.driver_alive = driver_alive or (lambda d: False)

    def owe(self, wid: str, description: str, deadline: float | None = None,
            driver: str | None = None) -> None:
        """Record work we have promised. A driver may be attached now or later."""
        self.conn.execute(
            "INSERT OR REPLACE INTO owed (id, description, deadline, driver, created_at, "
            "closed_at) VALUES (?,?,?,?,?,NULL)",
            (wid, description, deadline, driver, time.time()))
        self.conn.commit()

    def attach_driver(self, wid: str, driver: str) -> None:
        self.conn.execute("UPDATE owed SET driver=? WHERE id=?", (driver, wid))
        self.conn.commit()

    def close(self, wid: str) -> None:
        self.conn.execute("UPDATE owed SET closed_at=? WHERE id=?", (time.time(), wid))
        self.conn.commit()

    def open_items(self) -> list:
        return self.conn.execute(
            "SELECT * FROM owed WHERE closed_at IS NULL ORDER BY created_at").fetchall()

    def unattended(self) -> list:
        """Open work with no LIVE driver — the blind spot that cost 8h17m.

        A driver string is not evidence; it is checked for liveness. A record whose driver
        died is unattended, exactly like one that never had a driver.
        """
        out = []
        for row in self.open_items():
            if not row["driver"] or not self.driver_alive(row["driver"]):
                out.append(row)
        return out

    def overdue(self, now: float | None = None) -> list:
        now = time.time() if now is None else now
        return [r for r in self.open_items()
                if r["deadline"] is not None and r["deadline"] < now]

    def should_fire(self, backoff_until: float = 0.0, now: float | None = None) -> bool:
        """R3: backoff may delay routine polling, but NEVER owed work.

        The incumbent's backoff self-gated to 30- then 60-minute intervals after idle
        polls, which is right for a quiet channel and catastrophic when the channel is
        quiet *because* the owed work stalled.
        """
        now = time.time() if now is None else now
        if self.unattended():
            return True
        return now >= backoff_until

    def close_db(self) -> None:
        self.conn.close()
