"""core/schedule.py — the reference scheduler loop (ENH-6; R3, R4, R20).

The engine shipped primitives and told adopters to wire their own loop — but the loop is
where the incumbent's hardest bugs lived, and every adopter would have re-learned them.
This module is that transfer. Four lessons are structural here, not advisory:

* **single-instance guard.** Two loops on one state directory double-classify, race the
  cursor, and (the day a send path is enabled) double-deliver. The guard is a sqlite
  ``BEGIN EXCLUSIVE`` held for the process lifetime: the OS releases it when the holder
  dies, so a crash leaves no stale lockfile — the failure a PID file invites, where
  nothing ever runs again until a human deletes it. Keyed to the STATE directory, not
  the host: two schedulers on two state dirs are two deployments, not a conflict.

* **cursor-commit ordering.** Poll → journal → cursor-commit is a dual-write; the
  incumbent auto-reconciled its send/cursor variant of the same seam 24 times. Here the
  cursor commits strictly AFTER every polled message has a journal row, so a crash
  between the two re-polls and DUPLICATES (the journal absorbs re-sightings, R16) —
  cursor-first would silently lose the tail of the batch forever, with no reconciler.

* **backoff never suppresses owed work (R3).** Idle backoff widens the cadence for a
  genuinely quiet channel — and self-gated the incumbent to 60-minute intervals exactly
  when a stalled EXEC-REQUEST was keeping the channel quiet (8h17m, sev-high).
  Unattended owed work restores BASE cadence however deep the backoff. Base, not zero:
  the override is a cadence, never a hot spin.

* **the goal-triggered edge (R4/R20).** Every cycle checks the owed registry with no
  reference to inbound messages, and reports through the edge-triggered escalator —
  one page when work becomes unattended, one when it recovers, never a page per cycle.

The loop is deliberately crash-fast: nothing here catches an adapter or store error,
because a swallowed exception is the silent-no-op class (F-2) wearing a loop. Run it
under a supervisor that restarts it; the cursor ordering above is what makes that
restart safe.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from .classify import Taxonomy, classify


class AlreadyRunning(RuntimeError):
    """Another scheduler holds this state directory's lock. Refuse; never race it."""


class SingleInstanceGuard:
    def __init__(self, lock_path):
        self.lock_path = str(lock_path)
        self.conn = None

    def acquire(self) -> "SingleInstanceGuard":
        # isolation_level=None so the explicit BEGIN is ours, timeout=0 so a held lock
        # refuses NOW instead of queueing a second loop behind the first.
        conn = sqlite3.connect(self.lock_path, timeout=0, isolation_level=None)
        try:
            conn.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as ex:
            conn.close()
            raise AlreadyRunning(
                f"another scheduler already holds {self.lock_path} — one loop per "
                "state directory; stop the running instance instead of racing it"
            ) from ex
        self.conn = conn
        return self

    def release(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()


# The classify -> consequence map. Every actionable kind becomes OWED work so it stays
# visible until attended (R4) — the journal alone is an audit trail, not a to-do list.
ROUTES = {
    "EXEC-REQUEST": "owed:exec",        # work was asked for; someone must drive it
    "COMMITMENT-ASK": "owed:operator",  # needs a human gate, never an auto-answer
    "QUESTION": "owed:answer",
    "ATTACHMENT-ONLY": "owed:eyes",     # content the text pipeline cannot read
    "STATEMENT": "logged",              # the journal row IS the handling
}


def route(kind: str) -> str:
    """Destination for a classified kind. A kind this map has never heard of fails
    toward a human — 'logged' as the default would be the silently-inert class."""
    return ROUTES.get(kind, "owed:operator")


@dataclass
class Source:
    """One polling source: an adapter plus the channels this instance watches.
    Adapters are duck-typed (poll(cursor) -> (messages, new_cursor)) so this module
    never imports one — core stays channel-agnostic."""
    name: str                  # instance name; namespaces the cursor
    adapter: object
    channels: tuple = ()
    taxonomy: Taxonomy = field(default_factory=Taxonomy)


class Scheduler:
    CONDITION = "owed-work-unattended"

    def __init__(self, store, journal, owed, escalator, sources,
                 base_interval: float = 60.0, max_interval: float | None = None,
                 lock_path=None, clock=time.monotonic, sleep=time.sleep,
                 on_cycle=None):
        self.store, self.journal, self.owed = store, journal, owed
        self.escalator = escalator
        self.sources = list(sources)
        self.base_interval = float(base_interval)
        self.max_interval = float(max_interval if max_interval is not None
                                  else base_interval * 16)
        self.lock_path = lock_path
        self.clock, self.sleep = clock, sleep
        self.on_cycle = on_cycle or (lambda summary: None)
        self._interval = self.base_interval
        self._last_fire = None
        self._backoff_until = 0.0

    # ---- the loop ---------------------------------------------------------
    def run(self, max_cycles: int | None = None, stop=lambda: False) -> int:
        """Run guarded cycles until stop() or max_cycles. Returns cycles fired."""
        guard = (SingleInstanceGuard(self.lock_path).acquire()
                 if self.lock_path else None)
        try:
            fired = 0
            while not stop() and (max_cycles is None or fired < max_cycles):
                wait = self.seconds_until_fire()
                if wait > 0:
                    # Chunked so a stop() flipping mid-backoff is honoured within a
                    # second, not after a 16-minute sleep.
                    self.sleep(min(wait, 1.0))
                    continue
                self.on_cycle(self.cycle())
                fired += 1
            return fired
        finally:
            if guard is not None:
                guard.release()

    def seconds_until_fire(self, now: float | None = None) -> float:
        now = self.clock() if now is None else now
        if self._last_fire is None:
            return 0.0
        if self.owed.should_fire(self._backoff_until, now):
            # R3: owed work restores BASE cadence — backoff may delay routine polling,
            # never recovery. Floored at base so the override is a cadence, not a spin.
            return max(0.0, self._last_fire + self.base_interval - now)
        return max(0.0, self._backoff_until - now)

    # ---- one cycle: probe -> classify -> journal -> route -> owed ---------
    def cycle(self) -> dict:
        now = self.clock()
        polled = fresh = 0
        for src in self.sources:
            for ch in src.channels:
                cursor = self.store.cursor_get(src.name, ch)
                messages, new_cursor = src.adapter.poll(cursor)
                # poll() is adapter-wide; the engine owns per-channel attribution.
                mine = [m for m in messages if m.get("channel_id") == ch]
                self.store.upsert_messages(mine)
                # ORDER IS THE PROPERTY: every message journaled BEFORE the cursor
                # commits. A crash between the two re-polls this window and the
                # journal absorbs the duplicates; the swapped order loses the tail
                # of the batch forever (the incumbent's 24-auto-reconcile seam).
                fresh += self._journal_and_route(ch, mine, src.taxonomy)
                self._commit_cursor(src.name, ch, cursor, new_cursor)
                polled += len(mine)
        self._close_answered()
        unattended = self._observe_owed()
        self._last_fire = now
        self._interval = (self.base_interval if fresh
                          else min(self._interval * 2, self.max_interval))
        self._backoff_until = now + self._interval
        return {"polled": polled, "fresh": fresh, "unattended": unattended,
                "next_interval": self._interval}

    def _journal_and_route(self, channel, messages, taxonomy) -> int:
        fresh = 0
        for m in messages:
            c = classify(m.get("text") or "", taxonomy,
                         attachments=m.get("attachments"))
            dest = route(c.kind)
            res = self.journal.record(
                channel, m["ts"], sender_id=m.get("sender_id"),
                text=m.get("text") or "", kind=c.kind, reason=c.reason,
                matched=c.matched, routed=dest)
            if dest.startswith("owed:") and (res.is_new or res.is_revision):
                # The routed ask IS owed work (R4). A revision re-owes on purpose:
                # an edit that turns a remark into an ask is a new ask (R23), and
                # owe() is INSERT OR REPLACE so it reopens closed work.
                self.owed.owe(self.wid(channel, m["ts"]),
                              f"[{c.kind} -> {dest}] {channel} {m['ts']}: "
                              f"{(m.get('text') or '')[:120]}")
            if res.is_new or res.is_revision:
                fresh += 1
        return fresh

    def _commit_cursor(self, instance, channel, cursor, new_cursor) -> None:
        # The cursor is adapter-opaque (channels/CONTRACT.md): persist, never parse.
        if new_cursor is not None and new_cursor != cursor:
            self.store.cursor_set(instance, channel, new_cursor)

    def _close_answered(self) -> None:
        """Message-routed owed work closes when its journal row is tied to a response —
        EXCEPT rows edited after we answered (R23): the answer now answers the wrong
        question, so the reopened work must survive this sweep. Foreign owed items
        (adopter promises with their own ids) are never touched: only ids this loop
        minted can match."""
        reopened = {(r["channel"], r["ts"])
                    for r in self.journal.edited_after_response()}
        answered = {self.wid(r["channel"], r["ts"])
                    for r in self.journal.answered()
                    if (r["channel"], r["ts"]) not in reopened}
        for row in self.owed.open_items():
            if row["id"] in answered:
                self.owed.close(row["id"])

    def _observe_owed(self) -> int:
        """R4: consult the owed registry with no reference to inbound messages, and
        report through the edge-triggered escalator (R20) — never page-per-cycle."""
        unattended = self.owed.unattended()
        self.escalator.observe(
            self.CONDITION, ok=not unattended,
            detail=f"{len(unattended)} owed item(s) with no live driver, oldest: "
                   + (unattended[0]["description"][:80] if unattended else ""))
        return len(unattended)

    @staticmethod
    def wid(channel, ts) -> str:
        """Owed-work id for a message-routed ask. Derived, never parsed back — closure
        matches by constructing the same id from journal rows."""
        return f"msg@{channel}@{ts}"
