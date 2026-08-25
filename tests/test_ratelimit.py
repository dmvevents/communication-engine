"""Tests for core/ratelimit.py — per-(instance, method) back-off honouring Retry-After (ENH-1).

The acceptance being encoded, and the research behind it
(docs.slack.dev/apis/web-api/rate-limits): a 429 is scoped to that METHOD for that
WORKSPACE — "calls to other methods on behalf of this workspace are not restricted". So:

* a 429 on a read method must NOT delay a send method (a global back-off would let
  reading-too-fast make the engine go quiet on a customer);
* the platform's Retry-After value is honoured EXACTLY — limits are unpublished per
  method, so the wait is always the platform's number, never a padded or escalated guess;
* a 429 never becomes a dropped message: the guarded call either returns the result
  (retried) or re-raises RateLimited (surfaced) — there is no silent exit path.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.outbox import Outbox  # noqa: E402
from core.ratelimit import Backoff, RateLimited  # noqa: E402

# Method names are opaque strings to core; these Slack-flavoured ones document intent.
READ, SEND = "conversations.history", "chat.postMessage"


class FakeTime:
    """Deterministic clock + sleep: sleeping advances the clock and records the exact
    durations asked for, so 'honoured exactly' is assertable to the float."""

    def __init__(self, start=1000.0):
        self.t = start
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds

    def advance(self, seconds):
        self.t += seconds


class BackoffScopeTest(unittest.TestCase):
    """THE acceptance: hold state is keyed (instance, method), never wider."""

    def setUp(self):
        self.ft = FakeTime()
        self.b = Backoff(clock=self.ft.now, sleep=self.ft.sleep)

    def test_a_read_429_does_not_delay_a_send_method(self):
        """A 429 on conversations.history restricts conversations.history — Slack's
        contract says other methods on the workspace are NOT restricted. A back-off
        that leaks across methods silences replies because polling was too eager."""
        self.b.note("workspace", READ, 30)
        self.assertEqual(self.b.ready_in("workspace", SEND), 0.0,
                         "a read 429 delayed a send — back-off state is not "
                         "keyed per method")
        receipt = self.b.call("workspace", SEND, lambda: "receipt")
        self.assertEqual(receipt, "receipt")
        self.assertEqual(self.ft.sleeps, [],
                         "the send waited out the READ hold before calling")
        self.assertEqual(self.b.ready_in("workspace", READ), 30.0,
                         "the limited method itself must still be held")

    def test_a_429_on_one_instance_does_not_delay_another(self):
        """Limits are per workspace: two instances of the same adapter (two
        workspaces) discover their budgets independently."""
        self.b.note("workspace-a", READ, 30)
        self.assertEqual(self.b.ready_in("workspace-b", READ), 0.0,
                         "instance-a's 429 delayed instance-b — back-off state is "
                         "not keyed per instance")


class RetryAfterExactnessTest(unittest.TestCase):
    """Retry-After seconds honoured exactly: the platform's number, no padding, no
    exponential escalation, no assumed per-method table."""

    def setUp(self):
        self.ft = FakeTime()
        self.b = Backoff(clock=self.ft.now, sleep=self.ft.sleep)

    def test_the_hold_is_exactly_the_platform_value(self):
        self.b.note("w", READ, 30)
        self.assertEqual(self.b.ready_in("w", READ), 30.0)
        self.ft.advance(29.0)
        self.assertEqual(self.b.ready_in("w", READ), 1.0)
        self.ft.advance(1.0)
        self.assertEqual(self.b.ready_in("w", READ), 0.0)

    def test_retry_after_arrives_as_an_http_header_string(self):
        """HTTP delivers Retry-After as a string; adapters pass it straight through."""
        self.assertEqual(RateLimited("30").retry_after, 30.0)
        self.b.note("w", READ, "12")
        self.assertEqual(self.b.ready_in("w", READ), 12.0)

    def test_repeated_429s_wait_the_platform_value_each_time_not_escalating(self):
        """Discovery, not punishment: if the platform keeps saying 5, we wait 5 —
        an exponential multiplier would be a locally-invented limit table."""
        attempts = {"n": 0}

        def fn():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RateLimited(5)
            return "ok"

        self.assertEqual(self.b.call("w", READ, fn), "ok")
        self.assertEqual(self.ft.sleeps, [5.0, 5.0])

    def test_call_waits_out_the_exact_hold_before_calling(self):
        self.b.note("w", SEND, 10)
        calls = []
        self.b.call("w", SEND, lambda: calls.append("sent"))
        self.assertEqual(self.ft.sleeps, [10.0],
                         "the guarded call must wait out an active hold, exactly")
        self.assertEqual(calls, ["sent"])

    def test_distinct_retry_after_values_are_each_honoured(self):
        seen = iter([RateLimited(7), RateLimited(3)])

        def fn():
            nxt = next(seen, None)
            if nxt is not None:
                raise nxt
            return "receipt"

        self.assertEqual(self.b.call("w", SEND, fn), "receipt")
        self.assertEqual(self.ft.sleeps, [7.0, 3.0])

    def test_junk_retry_after_fails_loudly_at_the_boundary(self):
        """An HTTP-date or garbage header must fail where it entered, not poison the
        clock arithmetic silently (NaN compares false against everything)."""
        with self.assertRaises(ValueError):
            RateLimited("soon")
        with self.assertRaises(ValueError):
            RateLimited(float("nan"))
        with self.assertRaises(ValueError):
            self.b.note("w", READ, -1)


class NeverDroppedTest(unittest.TestCase):
    """A 429 never becomes a dropped message: retried or surfaced, never silently lost."""

    def setUp(self):
        self.ft = FakeTime()
        self.b = Backoff(clock=self.ft.now, sleep=self.ft.sleep)

    def test_exhausted_retries_surface_the_429_to_the_caller(self):
        attempts = {"n": 0}

        def always_limited():
            attempts["n"] += 1
            raise RateLimited(2, instance="w", method=SEND)

        with self.assertRaises(RateLimited):
            self.b.call("w", SEND, always_limited, max_tries=3)
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(self.ft.sleeps, [2.0, 2.0],
                         "each retry must first wait the platform's exact value")

    def test_zero_tries_is_refused_not_a_silent_drop(self):
        with self.assertRaises(ValueError):
            self.b.call("w", SEND, lambda: "never runs", max_tries=0)

    def test_non_429_errors_are_not_this_modules_business(self):
        """Only RateLimited is absorbed into back-off; anything else propagates
        untouched so real faults keep their own handling."""
        with self.assertRaises(KeyError):
            self.b.call("w", READ, lambda: (_ for _ in ()).throw(KeyError("boom")))
        self.assertEqual(self.ft.sleeps, [])

    def test_a_rate_limited_send_is_never_lost_by_the_engine(self):
        """The durable half of the guarantee: RateLimited surfacing through
        Outbox.send leaves the INTENT row on disk, so recovery re-delivers — the 429
        cost a delay, never the message, and never a duplicate."""

        class LimitedOnceAdapter:
            def __init__(self):
                self.delivered = []
                self.attempts = 0

            def send(self, target, text, key=None):
                self.attempts += 1
                if self.attempts == 1:
                    raise RateLimited("30", instance="w", method=SEND)
                self.delivered.append((target, text, key))
                return {"ts": "r1", "key": key}

            def read_back(self, target, key):
                return any(t == target and k == key
                           for t, _, k in self.delivered)

        with tempfile.TemporaryDirectory() as tmp:
            adapter = LimitedOnceAdapter()
            outbox = Outbox(Path(tmp) / "outbox.db", adapter,
                            {"C_CUSTOMER": "direct"})
            with self.assertRaises(RateLimited):
                outbox.send("C_CUSTOMER", "1.1", "the reply")
            self.assertEqual(len(outbox.pending()), 1,
                             "the 429'd send vanished from the outbox — a dropped "
                             "message")
            counts = outbox.recover()
            self.assertEqual(counts["resent"], 1)
            self.assertEqual(len(adapter.delivered), 1,
                             "recovery must deliver exactly once, not double-send")
            target, text, _key = adapter.delivered[0]
            self.assertEqual((target, text), ("C_CUSTOMER", "the reply"))
            self.assertEqual(outbox.pending(), [])
            outbox.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
