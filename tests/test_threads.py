"""In-thread answers close open asks (ENH-30): the sweep, its rules, its refusals.

Measured on the aws-do-agent integration (2026-10-12): every dashboard item read
UNANSWERED although the agent had answered each one, in-thread, through its own poster.
Two facts explained it. `responded_at` was set only by Journal.mark_responded(), which no
production path called, so no ask could ever become answered; and the Slack adapter polled
conversations.history only, which never serves thread replies, so the answers were not
even ingested. The sweep is the engine-side half of the fix: for each open ask it fetches
that thread's replies, stores them, and ties the ask to the first reply from someone other
than the asker.

Properties pinned here (each has a mutation in tests/mutation_check.sh):

* a reply from ANOTHER sender answers the ask; the asker's own follow-up does not;
* the swept replies are RETURNED so the caller journals them like any other message (a
  question asked inside a thread becomes an owed ask of its own);
* STATEMENTs are never swept: they owe nobody an answer, and sweeping them would spend
  one platform call per remark;
* an adapter without replies() yields None: declared absent, never "nothing new";
* the sweep is bounded per cycle and SAYS when it truncated;
* a platform 429 stops the sweep and is reported, never swallowed.
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.classify import ACTIONABLE_KINDS  # noqa: E402
from core.config import load_adapter_class  # noqa: E402
from core.journal import Journal  # noqa: E402
from core.ratelimit import RateLimited  # noqa: E402
from core.schedule import ROUTES  # noqa: E402
from core.store import Store  # noqa: E402
from core.threads import SWEEP_MAX, answered_by, sweep  # noqa: E402

FakeAdapter = load_adapter_class(ROOT / "channels", "fake")


def msg(channel, ts, text, sender="U_ASKER", thread=None):
    return {"channel_type": "fake", "channel_id": channel, "sender_id": sender,
            "ts": ts, "text": text, "thread_id": thread}


class CountingFake(FakeAdapter):
    """The shipped fake adapter, recording every replies() call it receives."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.calls = []            # (channel, thread_ts, oldest)
        self.fail_after = None     # raise RateLimited on the Nth call (1-based)

    def replies(self, channel_id, thread_ts, oldest=None):
        self.calls.append((channel_id, thread_ts, oldest))
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise RateLimited("3", method="conversations.replies")
        return super().replies(channel_id, thread_ts, oldest=oldest)


class NoRepliesAdapter:
    """A contract-minimal adapter: polls, but has no thread capability at all."""

    def poll(self, cursor):
        return [], cursor


class SweepHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.store = Store(base / "messages.db")
        self.journal = Journal(base / "journal.db")
        self.addCleanup(self.store.close)
        self.addCleanup(self.journal.close)
        self.adapter = CountingFake()

    def ask(self, ts="1.0", text="where is the dashboard?", kind="QUESTION",
            sender="U_ASKER", thread=None, channel="C1"):
        """One journaled, stored ask — the state the scheduler leaves after a poll."""
        m = msg(channel, ts, text, sender=sender, thread=thread)
        self.store.upsert_messages([m])
        self.adapter.seed([m])
        self.journal.record(channel, ts, sender_id=sender, text=text, kind=kind)
        return m

    def reply(self, ts, sender, text="here you go", thread="1.0", channel="C1"):
        """A reply that exists on the PLATFORM only (not yet stored) — what a poller
        that reads conversations.history never sees."""
        m = msg(channel, ts, text, sender=sender, thread=thread)
        self.adapter.seed([m])
        return m

    def responded(self, ts, channel="C1"):
        row = self.journal.get(channel, ts)
        return row["responded_at"], row["response_key"]


class AnsweredByTest(unittest.TestCase):
    def test_the_first_reply_from_someone_else_is_the_answer(self):
        replies = [msg("C1", "4.0", "x", sender="U_Y", thread="1.0"),
                   msg("C1", "2.0", "bump", sender="U_ASKER", thread="1.0"),
                   msg("C1", "3.0", "answer", sender="U_X", thread="1.0")]
        self.assertEqual(answered_by("U_ASKER", replies)["ts"], "3.0",
                         "the earliest FOREIGN reply answers; the asker's own bump and "
                         "a later reply do not")

    def test_only_the_askers_own_replies_is_no_answer(self):
        replies = [msg("C1", "2.0", "bump", sender="U_ASKER", thread="1.0")]
        self.assertIsNone(answered_by("U_ASKER", replies))

    def test_a_reply_without_a_sender_cannot_answer(self):
        replies = [dict(msg("C1", "2.0", "x", thread="1.0"), sender_id=None)]
        self.assertIsNone(answered_by("U_ASKER", replies))


class SweepTest(SweepHarness):
    def test_actionable_kinds_are_exactly_the_kinds_the_scheduler_owes(self):
        """ONE home for 'what is an ask': the dashboard's unanswered queue and this
        sweep both read ACTIONABLE_KINDS, and it must agree with the routing table,
        or a kind could be owed work the dashboard hides (or vice versa)."""
        owed = {k for k, dest in ROUTES.items() if dest.startswith("owed:")}
        self.assertEqual(set(ACTIONABLE_KINDS), owed)
        self.assertNotIn("STATEMENT", ACTIONABLE_KINDS)

    def test_a_reply_from_someone_else_answers_the_ask(self):
        self.ask()
        self.reply("2.0", "B_AGENT")
        res = sweep(self.adapter, self.store, self.journal, "C1")
        self.assertEqual(res["answered"], [("1.0", "2.0")])
        at, key = self.responded("1.0")
        self.assertIsNotNone(at, "the ask stayed unanswered after a foreign reply")
        self.assertEqual(key, "thread-reply:2.0",
                         "the response key must name WHAT answered, so the audit "
                         "trail can be followed to the reply")
        self.assertEqual([m["ts"] for m in res["replies"]], ["2.0"],
                         "the fetched reply must be returned for journaling")
        self.assertEqual(self.store.count("C1"), 2, "the reply was not stored")

    def test_the_askers_own_follow_up_does_not_answer(self):
        self.ask()
        self.reply("2.0", "U_ASKER", text="any update?")
        res = sweep(self.adapter, self.store, self.journal, "C1")
        self.assertEqual(res["answered"], [])
        self.assertIsNone(self.responded("1.0")[0],
                          "an asker bumping their own thread was read as an answer")
        self.assertEqual([m["ts"] for m in res["replies"]], ["2.0"],
                         "the bump is still a message: stored and returned")

    def test_the_first_foreign_reply_is_the_answer_not_the_latest(self):
        self.ask()
        self.reply("2.0", "U_ASKER", text="bump")
        self.reply("3.0", "U_X")
        self.reply("4.0", "U_Y")
        sweep(self.adapter, self.store, self.journal, "C1")
        self.assertEqual(self.responded("1.0")[1], "thread-reply:3.0")

    def test_statements_are_never_swept(self):
        self.ask(kind="STATEMENT", text="the deploy finished")
        self.reply("2.0", "U_X")
        res = sweep(self.adapter, self.store, self.journal, "C1")
        self.assertEqual(self.adapter.calls, [],
                         "a STATEMENT owes nobody an answer — sweeping it spends a "
                         "platform call per remark and would mark remarks 'answered'")
        self.assertEqual(res["swept"], 0)
        self.assertIsNone(self.responded("1.0")[0])

    def test_an_already_answered_ask_is_not_swept_again(self):
        self.ask()
        self.journal.mark_responded("C1", "1.0", "outbox-key")
        sweep(self.adapter, self.store, self.journal, "C1")
        self.assertEqual(self.adapter.calls, [])

    def test_an_adapter_without_replies_is_declared_absent_never_quiet(self):
        self.journal.record("C1", "1.0", sender_id="U", text="where?", kind="QUESTION")
        res = sweep(NoRepliesAdapter(), self.store, self.journal, "C1")
        self.assertIsNone(res, "an adapter that cannot see threads must read as "
                               "'cannot conclude', never as 'nothing new'")
        self.assertIsNone(self.responded("1.0")[0])

    def test_known_replies_are_not_refetched(self):
        self.ask()
        self.reply("2.0", "U_ASKER", text="bump")
        sweep(self.adapter, self.store, self.journal, "C1")
        sweep(self.adapter, self.store, self.journal, "C1")
        self.assertEqual([c[2] for c in self.adapter.calls], ["1.0", "2.0"],
                         "the second sweep must ask only for replies newer than the "
                         "newest one already stored")

    def test_an_ask_inside_a_thread_is_answered_by_a_later_reply_there(self):
        self.ask(ts="1.0", kind="STATEMENT", text="status: all green", sender="U_A")
        self.ask(ts="2.0", kind="QUESTION", text="is the dashboard up?",
                 sender="U_B", thread="1.0")
        self.reply("3.0", "U_C", text="yes", thread="1.0")
        res = sweep(self.adapter, self.store, self.journal, "C1")
        self.assertEqual(res["answered"], [("2.0", "3.0")])
        self.assertIsNone(self.responded("1.0")[0], "the STATEMENT parent is not an ask")
        self.assertEqual(self.adapter.calls[0][1], "1.0",
                         "the sweep must look in the thread the ask lives in")

    def test_the_sweep_is_bounded_and_says_so(self):
        for i in range(1, 8):
            self.ask(ts=f"{i}.0", text=f"question {i}?")
        res = sweep(self.adapter, self.store, self.journal, "C1", max_threads=3)
        self.assertEqual(res["swept"], 3)
        self.assertTrue(res["truncated"], "a bounded sweep must say it left work")
        self.assertEqual([c[1] for c in self.adapter.calls], ["7.0", "6.0", "5.0"],
                         "the newest asks are swept first — they are the ones most "
                         "likely to be getting answers right now")
        self.assertGreater(SWEEP_MAX, 0)

    def test_a_rate_limit_stops_the_sweep_and_is_reported(self):
        self.ask(ts="1.0")
        self.ask(ts="2.0", text="second question?")
        self.adapter.fail_after = 1
        res = sweep(self.adapter, self.store, self.journal, "C1")
        self.assertEqual(res["swept"], 1)
        self.assertEqual(res["rate_limited"], "conversations.replies",
                         "a 429 must be visible in the result, not swallowed")

    def test_the_fake_adapter_replies_excludes_the_parent_and_honours_oldest(self):
        a = FakeAdapter()
        a.seed([msg("C1", "1.0", "parent"),
                msg("C1", "2.0", "r1", sender="U_X", thread="1.0"),
                msg("C1", "3.0", "r2", sender="U_Y", thread="1.0"),
                msg("C2", "4.0", "other channel", sender="U_Z", thread="1.0")])
        self.assertEqual([m["ts"] for m in a.replies("C1", "1.0")], ["2.0", "3.0"])
        self.assertEqual([m["ts"] for m in a.replies("C1", "1.0", oldest="2.0")],
                         ["3.0"], "oldest is exclusive, like poll()'s cursor")


if __name__ == "__main__":
    unittest.main(verbosity=2)
