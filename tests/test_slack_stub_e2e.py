"""The real Slack adapter against the loopback stub (ENH-31 C): poll, sweep, 429.

tests/slack_stub.py is a stateful Slack Web API on 127.0.0.1:0; the adapter reaches it
through COMMS_SLACK_API_BASE, which admits loopback only. Nothing here can touch
slack.com. The send half of the path (chat.postMessage behind the outbox, read-back,
the six guards) is exercised in tests/test_slack_send.py.
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from core.config import load_adapter_class  # noqa: E402
from core.ratelimit import RateLimited  # noqa: E402
from slack_stub import SlackStub  # noqa: E402
from test_schedule import SchedulerHarness  # noqa: E402

ASK = "where is the dashboard?"


class StubBase(SchedulerHarness):
    def setUp(self):
        super().setUp()
        self.stub = SlackStub().start()
        self.addCleanup(self.stub.stop)

    def adapter(self, channels="C_ONE"):
        cls = load_adapter_class(ROOT / "channels", "slack")
        return cls(auth={"token": "stub-tok", "channels": channels},
                   env={"COMMS_SLACK_API_BASE": self.stub.base})


class PollThroughTheStubTest(StubBase):
    def test_history_is_polled_gap_free_and_the_token_rides_the_header(self):
        self.stub.seed("C_ONE", "first", user="U_A", ts="1.000000")
        self.stub.seed("C_ONE", ASK, user="U_A", ts="2.000000")
        self.stub.seed("C_ONE", "a reply in the thread", user="U_B", ts="3.000000",
                       thread_ts="2.000000")
        msgs, cursor = self.adapter().poll(None)
        self.assertEqual([m["ts"] for m in msgs], ["1.000000", "2.000000"],
                         "history must serve top-level messages only, oldest first")
        self.assertEqual(json.loads(cursor), {"C_ONE": "2.000000"})
        again, cursor2 = self.adapter().poll(cursor)
        self.assertEqual((again, cursor2), ([], cursor), "same cursor, nothing new")
        for method, form, auth in self.stub.requests:
            self.assertEqual(auth, "Bearer stub-tok")
            self.assertNotIn("token", form, "the token must never travel in the form")

    def test_a_429_carries_the_platforms_retry_after(self):
        self.stub.rate_limit["conversations.history"] = "17"
        with self.assertRaises(RateLimited) as ctx:
            self.adapter().poll(None)
        self.assertEqual((ctx.exception.retry_after, ctx.exception.method),
                         (17.0, "conversations.history"))


class SweepThroughTheStubTest(StubBase):
    def test_an_in_thread_answer_on_the_platform_closes_the_ask(self):
        """The whole ENH-30 path on a real adapter: history never serves the reply,
        conversations.replies does, the sweep ties the ask to it."""
        self.stub.seed("C_ONE", ASK, user="U_A", ts="10.000000")
        sched = self.scheduler(self.adapter(), channels=("C_ONE",))
        first = sched.cycle()
        self.assertEqual(first["answered"], 0)
        self.assertIsNone(self.journal.get("C_ONE", "10.000000")["responded_at"])
        self.stub.seed("C_ONE", "here: http://127.0.0.1:8502", user="U_B",
                       ts="11.000000", thread_ts="10.000000")
        second = sched.cycle()
        self.assertEqual(second["answered"], 1)
        row = self.journal.get("C_ONE", "10.000000")
        self.assertEqual(row["response_key"], "thread-reply:11.000000")
        self.assertIn("conversations.replies", [m for m, _, _ in self.stub.requests])
        self.assertNotIn("chat.postMessage", [m for m, _, _ in self.stub.requests],
                         "polling and sweeping must never write")


if __name__ == "__main__":
    unittest.main(verbosity=2)
