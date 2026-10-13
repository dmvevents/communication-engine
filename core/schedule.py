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

from . import threads
from .classify import ACTIONABLE_KINDS, Taxonomy, classify


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


def route(kind: str, *, ambiguous: bool = False,
          escalate_ambiguous: bool = False) -> str:
    """Destination for a classified kind. A kind this map has never heard of fails
    toward a human — 'logged' as the default would be the silently-inert class.

    ENH-9: a HEDGED decision (classify's ambiguous downgrade) goes to a human when the
    source opted in — the hedge used to become 'logged' with nothing anywhere saying
    so. Opt-in and not default, because on a chatty narrating channel every hedge is a
    page; the default keeps the old behaviour while the journal's recorded signal
    (ambiguity_stats) gives the adopter the number they need to decide."""
    if ambiguous and escalate_ambiguous:
        return "owed:operator"
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
    # ENH-9: route this source's hedged classifications to owed:operator instead of
    # only logging them. Per source, like the taxonomy: whether hedges deserve human
    # eyes depends on the channel's vocabulary and traffic, not on the engine.
    escalate_ambiguous: bool = False
    # ENH-31: who may close an ask (empty = any non-asker whose reply is not an ask),
    # shared by the thread rule and the DM rule; and which channels are 1:1 DMs, where
    # a TOP-LEVEL message from an answerer closes the ask (DMs have no threads in
    # practice: measured 535 DM messages, 0 in-thread).
    answerers: tuple = ()
    dm_channels: tuple = ()


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
        # ENH-30: where each channel's thread-sweep window resumes, and the order a
        # source's channels are swept in, live in the STORE (sweep_start_*, sweep_order_*),
        # not here: the cron deployment builds a new Scheduler per run, and in-memory
        # state restarted every run at the newest asks, in config order (review F1).

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
        polled = fresh = answered = 0
        sweep_rate_limited, sweep_truncated, sweep_errors = [], [], []
        sweep_skipped = []
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
                fresh += self._journal_and_route(ch, mine, src)
                self._commit_cursor(src.name, ch, cursor, new_cursor)
                polled += len(mine)
            # ENH-30: the open asks' threads. A history poll never serves a thread
            # reply, so an in-thread answer reaches the engine only here; the fetched
            # replies are journaled like any other message (a question asked inside a
            # thread becomes an owed ask of its own).
            #
            # One thread-sweep budget per ADAPTER per cycle, spent across its channels:
            # 25 calls per channel exceeded Slack's tier at two channels (review F8).
            # Spent in CONFIG order it starved: a first channel with 25 open roots — a
            # normal support backlog — took the whole budget every cycle, so no later
            # channel was ever swept and its in-thread answers never closed their asks,
            # silently (review D2: the ENH-30 symptom itself, for channel 2 onward).
            # So: one call stays reserved for every channel still waiting, so a backlog
            # channel takes the bulk but never the whole budget; the channel that took
            # the bulk moves to the back of the order, so two backlog channels take
            # turns; a channel a cycle did skip (a 429, or more channels than calls)
            # goes first next cycle; and every skip is reported by name and cause. The
            # window and the order are durable (store), so the cron deployment's
            # one-Scheduler-per-run keeps every guarantee. A 429 ends this source's
            # sweeping for the cycle; polling is already done.
            budget = threads.SWEEP_MAX
            order = self._sweep_order_for(src)
            swept_now, skipped_now, spent = [], [], {}
            stopped = None              # set once a 429 ends this source's sweeping
            for i, ch in enumerate(order):
                if budget <= 0:
                    skipped_now.append(ch)
                    # Named with its cause, and only when it owes a sweep (review F4): a
                    # channel with no open ask was not left behind.
                    if threads.open_asks_for(self.journal, ch):
                        sweep_skipped.append((ch, stopped or "budget"))
                    continue
                swept_now.append(ch)
                allow = max(1, budget - (len(order) - 1 - i))
                swept = threads.sweep(
                    src.adapter, self.store, self.journal, ch, max_threads=allow,
                    start=self.store.sweep_start_get(src.name, ch),
                    # A reply that classifies as an ask of its own is a new question,
                    # never the answer (review F7); the source's vocabulary decides.
                    is_answer=lambda m, _src=src: classify(
                        m.get("text") or "", _src.taxonomy,
                        attachments=m.get("attachments")).kind not in ACTIONABLE_KINDS,
                    answerers=src.answerers)
                if swept is not None:
                    # Charged by ATTEMPT: a refused fetch cost the platform a call too
                    # (review F2 — an all-errors channel was charged nothing, and the
                    # next channel got a second full budget).
                    budget -= swept["calls"]
                    spent[ch] = swept["calls"]
                    # The same order as the poll path: every fetched reply has a journal
                    # row BEFORE the window commits past it. Durable since round 2, the
                    # window written first survived a crash in journaling and left the
                    # replies in the store, owed to nobody, until it wrapped (review R2-2).
                    fresh += self._journal_and_route(ch, swept["replies"], src)
                    self.store.sweep_start_set(src.name, ch, swept["next_start"])
                    answered += len(swept["answered"])
                    # The sweep's refusals are the operator's business (G4): a 429, a
                    # truncated sweep or a dead thread must not print the same cycle
                    # line as a healthy one.
                    if swept["rate_limited"]:
                        sweep_rate_limited.append((ch, swept["rate_limited"]))
                        budget = 0          # the platform said stop; stop for this source
                        stopped = "rate-limited"
                    if swept["truncated"]:
                        sweep_truncated.append(ch)
                    sweep_errors.extend((ch, ts, msg) for ts, msg in swept["errors"])
            # ENH-31 B: a 1:1 DM has no threads to sweep; its asks close on the next
            # top-level message by an answerer. Store-only — no platform call, no budget.
            for ch in src.dm_channels:
                answered += len(threads.close_dm_asks(
                    self.store, self.journal, ch, answerers=src.answerers,
                    is_answer=lambda m, _src=src: classify(
                        m.get("text") or "", _src.taxonomy,
                        attachments=m.get("attachments")).kind not in ACTIONABLE_KINDS))
            # The channel that took the bulk — the one that SPENT the most — goes to the
            # back (review F3: rotating position 0 let a small channel's turn hand the
            # bulk straight back to the same backlog channel); skipped ones go first.
            bulk = max(swept_now, key=lambda c: spent.get(c, 0)) if swept_now else None
            rest = [c for c in swept_now if c != bulk] + ([bulk] if bulk is not None else [])
            new_order = skipped_now + rest
            self.store.sweep_order_set(src.name, new_order)
        self._close_answered()
        unattended = self._observe_owed()
        self._last_fire = now
        self._interval = (self.base_interval if fresh
                          else min(self._interval * 2, self.max_interval))
        self._backoff_until = now + self._interval
        return {"polled": polled, "fresh": fresh, "answered": answered,
                "unattended": unattended, "next_interval": self._interval,
                "sweep_rate_limited": sweep_rate_limited,
                "sweep_truncated": sweep_truncated, "sweep_errors": sweep_errors,
                "sweep_skipped": sweep_skipped}

    def _sweep_order_for(self, src) -> list:
        """This cycle's sweep order for one source, read from the store (so it survives
        the cron deployment's one Scheduler per run): the channels last cycle skipped
        first, then the ones it swept with the one that spent the most (took the bulk)
        moved to the back; channels new to the config join at the back, removed ones
        drop out. No saved order (a fresh state directory) is the config order."""
        saved = self.store.sweep_order_get(src.name)
        return ([c for c in saved if c in src.channels]
                + [c for c in src.channels if c not in saved])

    def _journal_and_route(self, channel, messages, src) -> int:
        fresh = 0
        for m in messages:
            c = classify(m.get("text") or "", src.taxonomy,
                         attachments=m.get("attachments"))
            dest = route(c.kind, ambiguous=c.ambiguous,
                         escalate_ambiguous=src.escalate_ambiguous)
            # ambiguous is recorded UNCONDITIONALLY (ENH-9): the count must accrue for
            # sources that have not opted in — they are the ones still deciding.
            res = self.journal.record(
                channel, m["ts"], sender_id=m.get("sender_id"),
                text=m.get("text") or "", kind=c.kind, reason=c.reason,
                matched=c.matched, routed=dest, ambiguous=c.ambiguous)
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
