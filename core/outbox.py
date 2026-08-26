"""core/outbox.py — the ONLY path to a send (gate G2; requirements R1, R2, R10).

The incumbent system reconciled a send/commit race **24 times**: a responder sent a reply,
died before persisting its cursor, and the loop had to prove-a-reply-existed-in-Slack to
self-heal. That is the classic dual-write problem, and detection-after-the-fact is not a
fix. This module makes the sequence recoverable by construction.

The ladder, in order, all durable:

    INTENT     written BEFORE the adapter is called. If we crash here, recovery knows a
               send *may* have happened and must check.
    SENT       the adapter returned a receipt.
    VERIFIED   a read-back proved the message is actually on the target.
    COMMITTED  safe for the caller to advance its own cursor.

Recovery resumes from **INTENT**, never from a cursor: for every unfinished row it asks the
adapter "is a message carrying this idempotency key already on the target?" — the same
proof-of-delivery technique the live system uses — and only re-sends when the answer is no.
That yields at-most-once delivery with at-least-once *attempt*, i.e. exactly one message.

Reply policy is CONFIG, not code: `never` (default) / `staged` / `direct`. A `never` target
raises; a `staged` target writes a draft for an operator to gate and never calls the adapter.
No caller may reach `adapter.send()` except through `Outbox.send()`.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

INTENT, SENT, VERIFIED, COMMITTED, STAGED = (
    "INTENT", "SENT", "VERIFIED", "COMMITTED", "STAGED")

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    key         TEXT PRIMARY KEY,
    target      TEXT NOT NULL,
    trigger_ts  TEXT NOT NULL,
    text        TEXT NOT NULL,
    state       TEXT NOT NULL,
    receipt     TEXT,
    policy      TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
"""


class PolicyError(PermissionError):
    """The target's reply policy forbids sending. Never downgrade to a warning."""


class SendBlocked(RuntimeError):
    """The adapter refused or the read-back could not prove delivery."""


def idempotency_key(target: str, trigger_ts: str, text: str) -> str:
    """Stable identity of a reply: who, what triggered it, exact content.

    Deliberately content-inclusive — the live system's 41 byte-identical outcome
    signatures came from re-emitting the SAME text for the same trigger, which this key
    collapses into one delivery.
    """
    h = hashlib.sha256()
    h.update(target.encode()); h.update(b"\x00")
    h.update(str(trigger_ts).encode()); h.update(b"\x00")
    h.update(text.encode())
    return h.hexdigest()


class Outbox:
    def __init__(self, db_path: str | Path, adapter, policies: dict[str, str] | None = None,
                 *, send_interval: float = 1.0,
                 clock=time.monotonic, sleep=time.sleep):
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.adapter = adapter
        # Default DENY. A target absent from the policy map cannot be sent to.
        self.policies = policies or {}
        # ENH-13: chat.postMessage allows ~1 message/second PER CHANNEL
        # (docs.slack.dev/apis/web-api/rate-limits), so 1.0 is the default floor —
        # a burst of replies would trade this cheap local wait for a platform 429,
        # the exact seam the INTENT ladder exists to survive. Cheaper to not trip it.
        # `clock`/`sleep` are injectable like ratelimit.Backoff's, and for the same
        # reason: the tests must assert spacing to the float.
        self.send_interval = send_interval
        self._clock = clock
        self._sleep = sleep
        self._pace_last: dict[str, float] = {}

    def _pace(self, target: str) -> None:
        """Hold this attempt until >= send_interval after the last attempt to `target`.

        Keyed per channel because the platform scopes the limit to the channel — a
        global hold would let one busy channel silence every other one (the disease
        ENH-1 killed for methods). The wait is EXACTLY the remainder of the interval,
        never padded: padding is a locally-invented limit.

        The clock marks the ATTEMPT, not the success: a 429'd attempt consumed the
        channel's budget too, so an unspaced retry would re-trip the very 429 it is
        recovering from. In-memory on purpose, like ratelimit.Backoff — the horizon
        is ~1s, so a restarted process at worst sends one message slightly early per
        channel, and the platform's own 429 (surfaced, never swallowed) is the
        backstop.
        """
        last = self._pace_last.get(target)
        if last is not None:
            wait = last + self.send_interval - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._pace_last[target] = self._clock()

    # ---- internals --------------------------------------------------------
    def _write(self, key, **cols):
        cols["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in cols)
        self.conn.execute(f"UPDATE outbox SET {sets} WHERE key=?",
                          (*cols.values(), key))
        self.conn.commit()          # durable before we do anything observable

    def get(self, key):
        return self.conn.execute("SELECT * FROM outbox WHERE key=?", (key,)).fetchone()

    def policy_for(self, target: str) -> str:
        return self.policies.get(target, "never")

    # ---- the send ladder --------------------------------------------------
    def send(self, target: str, trigger_ts: str, text: str, _crash_at: str | None = None):
        """Deliver exactly once, or stage, or refuse. Returns a receipt dict.

        `_crash_at` is used ONLY by the fault-injection harness to simulate the process
        dying at a named seam; production callers never pass it.
        """
        policy = self.policy_for(target)
        if policy == "never":
            raise PolicyError(
                f"reply policy for {target} is 'never' — refusing to send. "
                "Sending requires an explicit policy of 'staged' or 'direct'.")

        key = idempotency_key(target, trigger_ts, text)
        row = self.get(key)

        if row is None:
            # This INSERT is also the CLAIM on the ladder below: concurrent senders of
            # the same reply race the SELECT above, and the primary key must arbitrate
            # to exactly one owner. OR IGNORE because the loser's insert is a clean
            # no-op, not an IntegrityError crash (6-sender probe delivered 3× before
            # this, fire=13).
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO outbox (key, target, trigger_ts, text, state, "
                "policy, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (key, target, str(trigger_ts), text,
                 STAGED if policy == "staged" else INTENT,
                 policy, time.time(), time.time()))
            self.conn.commit()      # R1: INTENT is durable BEFORE the adapter is touched
            if cur.rowcount == 0:   # lost the SELECT→INSERT race; read what the winner wrote
                row = self.get(key)

        if row is not None:
            # R2: already delivered (or proven) — return the existing receipt, do NOT re-send.
            if row["state"] in (VERIFIED, COMMITTED):
                return {"key": key, "receipt": row["receipt"], "state": row["state"],
                        "deduped": True}
            if row["state"] == STAGED:
                return {"key": key, "receipt": None, "state": STAGED, "deduped": True}
            # INTENT/SENT: another sender holds the claim. Sending here is the
            # double-delivery bug — and if the claimant DIED mid-flight, only
            # read-back can tell "sent, unrecorded" from "never sent", so the row
            # belongs to recover(), never to a second live send.
            return {"key": key, "receipt": None, "state": row["state"],
                    "in_flight": True}

        if policy == "staged":
            # Draft only. The operator gates the actual send; the adapter is never called.
            return {"key": key, "receipt": None, "state": STAGED, "staged": True}

        if _crash_at == "after_intent":
            raise _Crash("crash after INTENT, before adapter.send")

        self._pace(target)
        receipt = self.adapter.send(target, text, key=key)

        if _crash_at == "after_send":
            # The nastiest seam: the message IS on the target but we never recorded it.
            raise _Crash("crash after adapter.send, before recording SENT")

        self._write(key, state=SENT, receipt=json.dumps(receipt))

        if _crash_at == "before_readback":
            raise _Crash("crash after SENT, before read-back")

        if not self.adapter.read_back(target, key):
            raise SendBlocked(f"read-back could not prove delivery of {key} to {target}")
        self._write(key, state=VERIFIED)

        if _crash_at == "before_commit":
            raise _Crash("crash after VERIFIED, before COMMITTED")

        self._write(key, state=COMMITTED)
        return {"key": key, "receipt": receipt, "state": COMMITTED, "deduped": False}

    # ---- recovery ---------------------------------------------------------
    def recover(self) -> dict:
        """Resume every unfinished row from INTENT. Returns a counts summary.

        For each row not yet VERIFIED/COMMITTED/STAGED, ask the target whether a message
        carrying this idempotency key already landed:
          * yes -> mark VERIFIED then COMMITTED. **No re-send.**
          * no  -> the send never happened; do it now.
        """
        counts = {"resumed": 0, "resent": 0, "already_delivered": 0}
        rows = self.conn.execute(
            "SELECT * FROM outbox WHERE state IN (?,?)", (INTENT, SENT)).fetchall()
        for row in rows:
            counts["resumed"] += 1
            key, target = row["key"], row["target"]
            if self.adapter.read_back(target, key):
                counts["already_delivered"] += 1
            else:
                # recovery is a burst source too: N undelivered rows for one channel
                # re-sent back-to-back would 429 exactly like the live path
                self._pace(target)
                receipt = self.adapter.send(target, row["text"], key=key)
                self._write(key, receipt=json.dumps(receipt))
                counts["resent"] += 1
                if not self.adapter.read_back(target, key):
                    raise SendBlocked(f"re-send of {key} still not provable")
            self._write(key, state=VERIFIED)
            self._write(key, state=COMMITTED)
        return counts

    def pending(self) -> list:
        return self.conn.execute(
            "SELECT * FROM outbox WHERE state IN (?,?)", (INTENT, SENT)).fetchall()

    def staged(self) -> list:
        return self.conn.execute(
            "SELECT * FROM outbox WHERE state=?", (STAGED,)).fetchall()

    def close(self):
        self.conn.close()


class _Crash(BaseException):
    """Simulated process death. Inherits BaseException so ordinary `except Exception`
    handlers cannot accidentally swallow it and hide a broken seam."""
