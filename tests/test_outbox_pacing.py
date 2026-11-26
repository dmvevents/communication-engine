"""Per-channel send pacing at <=1 message/second (ENH-13).

The research behind the item (docs.slack.dev/apis/web-api/rate-limits): chat.postMessage
is limited to ~1 message per second PER CHANNEL. The outbox had no pacing at all, so a
burst of replies would trade a cheap local wait for a platform 429 — and a 429 mid-send
is exactly the seam the INTENT ladder exists to survive. Cheaper to not trip it.

The acceptance being encoded:

* a burst of N sends to one channel leaves the adapter's attempt timestamps >= 1s apart,
  and the wait is EXACTLY the remainder of the interval (ENH-1's no-padding discipline:
  a padded wait is a locally-invented limit);
* the pace is scoped per channel — the platform scopes this limit to the channel, so one
  busy channel must not silence every other one (the same disease ENH-1 killed for
  methods);
* a 429 that fires mid-burst anyway (someone else consumed the channel's budget) still
  yields exactly-once delivery for every message: the 429'd attempt stays INTENT on disk,
  recovery re-sends it — once — and the retry waits out the full interval from the FAILED
  attempt, because that attempt consumed budget too and an unspaced retry re-trips the
  very 429 it is recovering from.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.outbox import COMMITTED, Outbox, _Crash, idempotency_key  # noqa: E402
from core.ratelimit import RateLimited  # noqa: E402

CH_A = "C_CUSTOMER_A"
CH_B = "C_CUSTOMER_B"
TS = "1700000000.000100"


class FakeTime:
    """Deterministic clock + sleep: sleeping advances the clock and records the exact
    durations asked for, so 'paced exactly' is assertable to the float."""

    def __init__(self, start=1000.0):
        self.t = start
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class TimedAdapter:
    """Records the pacer-visible clock at every attempt, so spacing is measured at the
    seam that matters: when the request would hit the platform."""

    def __init__(self, clock):
        self.clock = clock
        self.attempts = []       # (t, target, key) — every call, delivered or not
        self.delivered = []      # (target, text, key) — the remote side's ground truth

    def send(self, target, text, key=None):
        self.attempts.append((self.clock(), target, key))
        self.delivered.append((target, text, key))
        return {"ts": f"receipt-{len(self.attempts)}", "key": key}

    def read_back(self, target, key):
        return any(t == target and k == key for t, _, k in self.delivered)


class EnforcingAdapter(TimedAdapter):
    """The platform's own view of the limit: any attempt < 1s after the last accepted
    post to a channel is 429'd. `steal_slot_at` models the burst reality the item names —
    on that attempt number someone ELSE consumes the channel's budget, so a correctly
    paced attempt still gets a 429 and the slot counts as taken."""

    def __init__(self, clock, steal_slot_at=None):
        super().__init__(clock)
        self.accepted_at = {}
        self.steal_slot_at = steal_slot_at
        self.limited = 0

    def send(self, target, text, key=None):
        now = self.clock()
        self.attempts.append((now, target, key))
        last = self.accepted_at.get(target)
        stolen = len(self.attempts) == self.steal_slot_at
        if stolen or (last is not None and now - last < 1.0):
            self.limited += 1
            if stolen:
                self.accepted_at[target] = now   # the thief's message took the slot
            raise RateLimited("1", method="chat.postMessage")
        self.accepted_at[target] = now
        self.delivered.append((target, text, key))
        return {"ts": f"receipt-{len(self.attempts)}", "key": key}


class PacingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "outbox.db"
        self.ft = FakeTime()
        self.adapter = TimedAdapter(self.ft.now)
        self.policies = {CH_A: "direct", CH_B: "direct"}

    def tearDown(self):
        self.tmp.cleanup()

    def box(self, adapter=None):
        # send_interval deliberately NOT passed: the 1/sec default IS the property —
        # it must cover the platform floor without every caller knowing to ask.
        return Outbox(self.db, adapter or self.adapter, self.policies,
                      clock=self.ft.now, sleep=self.ft.sleep)

    def texts(self, n):
        return [f"[AGENT] reply {i}" for i in range(n)]

    # ---- the spacing acceptance --------------------------------------------
    def test_a_burst_to_one_channel_is_spaced_at_one_per_second(self):
        b = self.box()
        for text in self.texts(5):
            b.send(CH_A, TS, text)
        times = [t for t, _, _ in self.adapter.attempts]
        self.assertEqual(times, [1000.0, 1001.0, 1002.0, 1003.0, 1004.0],
                         "a burst of 5 sends to one channel must hit the adapter "
                         ">= 1s apart — unspaced attempts are a platform 429")
        self.assertEqual(self.ft.sleeps, [1.0, 1.0, 1.0, 1.0],
                         "the wait must be EXACTLY the remainder of the interval — "
                         "padding is a locally-invented limit (ENH-1 discipline)")

    def test_the_first_send_to_a_channel_is_not_delayed(self):
        self.box().send(CH_A, TS, "[AGENT] first")
        self.assertEqual(self.ft.sleeps, [],
                         "an idle channel has budget — waiting before the first "
                         "send throttles nothing and delays a customer")

    def test_pacing_is_per_channel_not_global(self):
        b = self.box()
        b.send(CH_A, TS, "[AGENT] to A")
        b.send(CH_B, TS, "[AGENT] to B")
        self.assertEqual(self.ft.sleeps, [],
                         "channel B waited out channel A's interval — the platform "
                         "scopes this limit per channel, so must the pacer")
        b.send(CH_A, TS, "[AGENT] to A again")
        self.assertEqual(self.ft.sleeps, [1.0],
                         "the busy channel itself must still be held to 1/sec")

    def test_a_quiet_interval_is_not_re_waited(self):
        b = self.box()
        b.send(CH_A, TS, "[AGENT] one")
        self.ft.t += 5.0     # the channel went quiet past the interval
        b.send(CH_A, TS, "[AGENT] two")
        self.assertEqual(self.ft.sleeps, [],
                         "budget already elapsed in real time must not be slept again")

    # ---- pacing must not leak into no-op paths ------------------------------
    def test_a_deduped_repeat_neither_sleeps_nor_touches_the_adapter(self):
        b = self.box()
        b.send(CH_A, TS, "[AGENT] once")
        before = len(self.adapter.attempts)
        r = b.send(CH_A, TS, "[AGENT] once")
        self.assertTrue(r["deduped"])
        self.assertEqual(len(self.adapter.attempts), before)
        self.assertEqual(self.ft.sleeps, [],
                         "a dedupe never reaches the platform, so it must never pace")

    # ---- recovery is a burst source too -------------------------------------
    def test_recovery_resends_to_one_channel_are_paced(self):
        # three crashes after INTENT leave three undelivered rows for one channel;
        # recovery re-sending them unspaced is the same burst the live path avoids
        for text in self.texts(3):
            with self.assertRaises(_Crash):
                self.box().send(CH_A, TS, text, _crash_at="after_intent")
        counts = self.box().recover()
        self.assertEqual(counts["resent"], 3)
        times = [t for t, _, _ in self.adapter.attempts]
        self.assertEqual([b - a for a, b in zip(times, times[1:])], [1.0, 1.0],
                         "recovery re-sent a burst to one channel unspaced")

    # ---- the 429 acceptance: a burst that trips anyway ----------------------
    def test_a_429_mid_burst_still_yields_exactly_once_for_every_message(self):
        adapter = EnforcingAdapter(self.ft.now, steal_slot_at=3)
        b = self.box(adapter)
        texts = self.texts(5)
        limited_here = 0
        for text in texts:
            try:
                b.send(CH_A, TS, text)
            except RateLimited:
                limited_here += 1    # surfaced, never swallowed — INTENT row remains
        self.assertEqual(limited_here, 1, "the stolen slot must surface as a 429")
        self.assertEqual(len(b.pending()), 1,
                         "the 429'd send must survive as durable pending work")

        counts = b.recover()
        self.assertEqual(counts["resent"], 1)
        self.assertEqual(adapter.limited, 1,
                         "the recovery re-send re-tripped the 429: a 429'd attempt "
                         "consumed the channel's budget, so the retry must wait out "
                         "the full interval from the FAILED attempt")
        for text in texts:
            n = sum(1 for tg, tx, _ in adapter.delivered
                    if (tg, tx) == (CH_A, text))
            self.assertEqual(n, 1,
                             f"message {text!r}: delivered {n} times, expected "
                             "exactly 1 — a 429 mid-burst cost a delay, never a "
                             "message and never a duplicate")
        self.assertEqual(b.pending(), [])
        accepted = sorted(t for t, tg, _ in adapter.attempts if tg == CH_A)
        gaps = [b_ - a_ for a_, b_ in zip(accepted, accepted[1:])]
        self.assertTrue(all(g >= 1.0 for g in gaps),
                        f"attempts reached the platform < 1s apart: {gaps}")

    def test_the_recovered_send_still_commits(self):
        adapter = EnforcingAdapter(self.ft.now, steal_slot_at=1)
        b = self.box(adapter)
        with self.assertRaises(RateLimited):
            b.send(CH_A, TS, "[AGENT] reply")
        b.recover()
        key = idempotency_key(CH_A, TS, "[AGENT] reply")
        self.assertEqual(b.get(key)["state"], COMMITTED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
