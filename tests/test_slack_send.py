"""The gated Slack send, end to end against the loopback stub (ENH-31 A): the real
adapter, the real outbox ladder, the six guards, exactly-once under a 429, and
placement. Nothing here can reach slack.com: COMMS_SLACK_API_BASE admits loopback only.
"""
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from core.config import load_adapter_class  # noqa: E402
from core.outbox import (COMMITTED, INTENT, Outbox, PolicyError, ReleaseError,  # noqa: E402
                         SENT, STAGED, SendBlocked)
from core.ratelimit import RateLimited  # noqa: E402
from slack_stub import SlackStub  # noqa: E402

CH, DM = "C_CUST", "D_SOMEONE"
ASK_TS = "1699999999.000100"
TEXT = "[AGENT] here: http://127.0.0.1:8502 — ping me if it does not load"


class StubSend(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stub = SlackStub(slack_escapes=True).start()
        self.addCleanup(self.stub.stop)
        self.stub.seed(CH, "where is the dashboard?", user="U_A", ts=ASK_TS)

    def adapter(self):
        cls = load_adapter_class(ROOT / "channels", "slack")
        return cls(auth={"token": "stub-tok", "channels": f"{CH},{DM}"},
                   env={"COMMS_SLACK_API_BASE": self.stub.base})

    def box(self, adapter=None, policies=None, marker="[AGENT]"):
        return Outbox(Path(self.tmp.name) / "outbox.db", adapter or self.adapter(),
                      policies or {CH: "staged", DM: "staged"}, send_interval=0.0,
                      marker=marker)

    def posts(self, method="chat.postMessage"):
        return [f for m, f, _ in self.stub.requests if m == method]

    # ---- the happy path: stage, click, post once, prove it, commit --------------------
    def test_a_staged_reply_is_posted_once_in_the_asks_thread_and_proven_by_read_back(self):
        box = self.box()
        key = box.stage(CH, ASK_TS, TEXT, thread_id=ASK_TS)["key"]
        self.assertEqual(self.posts(), [], "staging must not post")
        res = box.release(key)
        self.assertEqual(res["state"], COMMITTED)
        self.assertEqual(len(self.posts()), 1)
        form = self.posts()[0]
        self.assertEqual((form["channel"], form["text"], form["thread_ts"]),
                         (CH, TEXT, ASK_TS))
        shown = self.stub.posted(CH)[0]
        self.assertEqual(shown["metadata"]["event_payload"]["key"], key)
        self.assertEqual(shown["text"], TEXT.replace("&", "&amp;").replace("<", "&lt;")
                         .replace(">", "&gt;"), "the stub stores Slack-escaped text")
        for method, form, auth in self.stub.requests:
            self.assertEqual(auth, "Bearer stub-tok")
            self.assertNotIn("token", form)

    def test_a_second_click_dedupes_instead_of_posting_twice(self):
        box = self.box()
        key = box.stage(CH, ASK_TS, TEXT, thread_id=ASK_TS)["key"]
        box.release(key)
        again = box.release(key)
        self.assertTrue(again["deduped"])
        self.assertEqual(len(self.posts()), 1)

    # ---- guard 3: read-back must prove the SAME text ------------------------------------
    def test_a_truncated_post_is_not_committed_and_not_re_sent(self):
        self.stub.truncate_posts_to = 20
        box = self.box()
        key = box.stage(CH, ASK_TS, TEXT, thread_id=ASK_TS)["key"]
        with self.assertRaises(SendBlocked) as cm:
            box.release(key)
        self.assertIn("read-back", str(cm.exception))
        self.assertEqual(box.get(key)["state"], SENT, "SENT is durable; COMMITTED is not earned")
        self.assertEqual(len(self.posts()), 1, "an unproven send must never be re-sent blindly")

    # ---- guards 1, 4, 5 on the real adapter -----------------------------------------------
    def test_the_text_guards_refuse_before_any_post(self):
        box = self.box()
        with self.assertRaises(SendBlocked):
            box.stage(CH, ASK_TS, "no marker")
        with self.assertRaises(SendBlocked):
            box.stage(CH, ASK_TS, "[AGENT] " + "x" * 3600)
        with self.assertRaises(SendBlocked):
            box.stage(CH, ASK_TS, "[AGENT] " + "xo" + "xb-" + "1234567890-abcdefghij")
        self.assertEqual(self.posts(), [])

    # ---- guard 2: a newer message on the platform refuses the click ------------------------
    def test_a_message_posted_after_staging_refuses_the_click_until_re_read(self):
        box = self.box()
        key = box.stage(CH, ASK_TS, TEXT, thread_id=ASK_TS)["key"]
        self.stub.seed(CH, "never mind, found it", user="U_A", thread_ts=ASK_TS,
                       ts=f"{time.time() + 10:.6f}")
        with self.assertRaises(ReleaseError) as cm:
            box.release(key)
        self.assertIn("re-read first", str(cm.exception))
        self.assertIn("U_A", str(cm.exception))
        self.assertEqual((box.get(key)["state"], self.posts()), (STAGED, []))

    # ---- the grant: direct is off for Slack ------------------------------------------------
    def test_a_direct_policy_is_refused_at_construction(self):
        with self.assertRaises(PolicyError) as cm:
            self.box(policies={CH: "direct"})
        self.assertIn("direct", str(cm.exception))

    # ---- exactly once under a 429, through recovery -------------------------------------------
    def test_a_429_on_the_post_leaves_intent_and_recovery_posts_exactly_once(self):
        box = self.box()
        key = box.stage(CH, ASK_TS, TEXT, thread_id=ASK_TS)["key"]
        self.stub.rate_limit["chat.postMessage"] = "3"
        with self.assertRaises(RateLimited):
            box.release(key)
        self.assertEqual(box.get(key)["state"], INTENT)
        self.assertEqual(self.stub.posted(CH), [], "the 429'd call posted nothing")
        counts = self.box().recover()                        # a new process, same db
        self.assertEqual(counts["resent"], 1)
        self.assertEqual(len(self.stub.posted(CH)), 1)
        self.assertEqual(self.box().get(key)["state"], COMMITTED)

    def test_recovery_after_a_crash_between_post_and_sent_finds_the_message_by_key(self):
        """The nastiest seam: the message IS on the platform, the row still says INTENT.
        Recovery must find it by its metadata key — in the THREAD — and never post twice."""
        box = self.box()
        key = box.stage(CH, ASK_TS, TEXT, thread_id=ASK_TS)["key"]
        from core.outbox import _Crash
        with self.assertRaises(_Crash):
            box.release(key, _crash_at="after_send")
        self.assertEqual(len(self.stub.posted(CH)), 1)
        counts = self.box().recover()
        self.assertEqual((counts["already_delivered"], counts["resent"]), (1, 0))
        self.assertEqual(len(self.stub.posted(CH)), 1, "recovery re-posted a delivered reply")
        self.assertEqual(self.box().get(key)["state"], COMMITTED)

    # ---- placement: a DM reply is top-level ---------------------------------------------------
    def test_a_top_level_send_carries_no_thread_and_reads_back_from_history(self):
        box = self.box()
        key = box.stage(DM, "1699999999.000200", "[AGENT] yes, it is up")["key"]
        self.assertEqual(box.release(key)["state"], COMMITTED)
        self.assertNotIn("thread_ts", self.posts()[0])
        self.assertEqual(len(self.stub.posted(DM)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
