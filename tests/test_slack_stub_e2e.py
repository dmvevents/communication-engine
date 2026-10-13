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

    def adapter(self, channels="C_ONE", token="stub-tok"):
        cls = load_adapter_class(ROOT / "channels", "slack")
        return cls(auth={"token": token, "channels": channels},
                   env={"COMMS_SLACK_API_BASE": self.stub.base})

    def outbox(self, adapter, policies):
        from core.outbox import Outbox
        return Outbox(Path(self.tmp.name) / "outbox.db", adapter, policies,
                      send_interval=0.0, marker="[AGENT]")


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


class EngineDeliveriesThroughTheStubTest(StubBase):
    """The engine's OWN posts on the shipped path (review round 2, R2-1 / R2-3): the real
    adapter polls them back with their metadata, so the DM rule and the sweep recognise
    them and never credit them to an ask the surface did not tie. Round 1's test planted
    the metadata by hand on the fake adapter — a raw the real poll never stored."""

    DM = "D_SOMEONE"

    def test_the_surfaces_dm_reply_never_closes_the_older_open_ask(self):
        self.stub.seed(self.DM, "where is the dashboard?", user="U_HUMAN", ts="1.000000")
        self.stub.seed(self.DM, "and the logs?", user="U_HUMAN", ts="2.000000")
        adapter = self.adapter(channels=self.DM, token="xoxb-stub")
        sched = self.scheduler(adapter, channels=(self.DM,), dm_channels=(self.DM,))
        sched.cycle()
        self.assertEqual({r["id"] for r in self.owed.open_items()},
                         {f"msg@{self.DM}@1.000000", f"msg@{self.DM}@2.000000"})
        # the operator answers ask 2 from the surface: stage, click, prove, tie
        box = self.outbox(adapter, {self.DM: "staged"})
        key = box.stage(self.DM, "2.000000", "[AGENT] logs: /var/log/comms")["key"]
        self.assertEqual(box.release(key)["state"], "COMMITTED")
        self.journal.mark_responded(self.DM, "2.000000", key, at=self.now)
        # the next poll brings the engine's post back, WITH its metadata
        self.assertEqual(sched.cycle()["answered"], 0,
                         "the engine's own delivery closed the next older open ask")
        self.assertIsNone(self.journal.get(self.DM, "1.000000")["responded_at"])
        self.assertIn(f"msg@{self.DM}@1.000000", {r["id"] for r in self.owed.open_items()})
        stored = self.store.top_level_after(self.DM, "2.000000")
        self.assertEqual(len(stored), 1)
        self.assertEqual(json.loads(stored[0]["raw"])["metadata"]["event_type"],
                         "comms_engine_reply", "the poll stored the post without its metadata")
        polls = [f for m, f, _ in self.stub.requests if m == "conversations.history"]
        self.assertTrue(polls and all(f.get("include_all_metadata") == "true" for f in polls),
                        "a history poll did not ask for message metadata")
        # a bot message WITHOUT the engine's metadata is an answer like any other
        self.stub.seed(self.DM, "dashboard: http://127.0.0.1:8502", user="U_BOT")
        self.assertEqual(sched.cycle()["answered"], 1)
        self.assertTrue(self.journal.get(self.DM, "1.000000")["response_key"].startswith("dm-reply:"))

    def test_the_surfaces_thread_reply_never_closes_another_ask_under_the_same_root(self):
        self.stub.seed("C_ONE", "where is the dashboard?", user="U_A", ts="10.000000")
        self.stub.seed("C_ONE", "which dashboard do you mean?", user="U_B", ts="11.000000",
                       thread_ts="10.000000")
        adapter = self.adapter(token="xoxb-stub")
        sched = self.scheduler(adapter, channels=("C_ONE",))
        sched.cycle()                                   # the sweep journals U_B's question
        self.assertIsNotNone(self.journal.get("C_ONE", "11.000000"))
        box = self.outbox(adapter, {"C_ONE": {"thread": "staged"}})
        key = box.stage("C_ONE", "10.000000", "[AGENT] the comms one, at http://127.0.0.1:8502",
                        thread_id="10.000000")["key"]
        self.assertEqual(box.release(key)["state"], "COMMITTED")
        self.journal.mark_responded("C_ONE", "10.000000", key, at=self.now)
        self.assertEqual(sched.cycle()["answered"], 0,
                         "the engine's thread reply to U_A closed U_B's question")
        self.assertIsNone(self.journal.get("C_ONE", "11.000000")["responded_at"])
        replies = [f for m, f, _ in self.stub.requests if m == "conversations.replies"]
        self.assertTrue(replies and all(f.get("include_all_metadata") == "true" for f in replies),
                        "a sweep fetch did not ask for message metadata")
        # a human's answer under the root still closes U_B's question
        self.stub.seed("C_ONE", "the comms one", user="U_A", ts="13.000000", thread_ts="10.000000")
        self.assertEqual(sched.cycle()["answered"], 1)
        self.assertEqual(self.journal.get("C_ONE", "11.000000")["response_key"],
                         "thread-reply:13.000000")

    def test_a_mismatched_post_never_becomes_the_answer_a_cycle_later(self):
        """Review R2-3: the card promised 'nothing is recorded as answered'; one cycle later
        the mangled post closed the ask through the poll. The engine's post, mangled or
        not, is tied only by the outbox's proof — which a MISMATCH never earns."""
        from core.outbox import MISMATCH, SendBlocked
        self.stub.seed(self.DM, "where is the dashboard?", user="U_HUMAN", ts="1.000000")
        adapter = self.adapter(channels=self.DM, token="xoxb-stub")
        sched = self.scheduler(adapter, channels=(self.DM,), dm_channels=(self.DM,))
        sched.cycle()
        self.stub.truncate_posts_to = 20
        box = self.outbox(adapter, {self.DM: "staged"})
        key = box.stage(self.DM, "1.000000", "[AGENT] dashboard: http://127.0.0.1:8502")["key"]
        with self.assertRaises(SendBlocked):
            box.release(key)
        self.assertEqual(box.get(key)["state"], MISMATCH)
        self.assertEqual(sched.cycle()["answered"], 0, "a MISMATCH post was recorded as the answer")
        self.assertIsNone(self.journal.get(self.DM, "1.000000")["responded_at"])
