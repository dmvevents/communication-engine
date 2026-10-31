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

threaded_is_answered (2026-10-31) — the tie at INGEST, from the polled row's own thread
summary (`answered_from_summary`, through the adapter's optional `thread_summary()`):

* a replier other than the asker in the summary answers the ask, with no replies() call,
  and the key names the replier (`thread-reply:summary:<replier>`);
* a thread whose only repliers are the asker stays open; a count with no attributable
  replier stays open; the unattributed placeholder answers nothing;
* only open ASKS are tied — a STATEMENT parent and an already-answered ask are left alone;
* an adapter without thread_summary() yields None: declared absent, never "no thread";
* the tie is stamped at the sighting, so the row does not read EDITED AFTER RESPONSE.
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
from core.threads import SWEEP_MAX, answered_by, answered_from_summary, sweep  # noqa: E402

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
        self.explode_on = None     # raise a platform error for this thread_ts

    def replies(self, channel_id, thread_ts, oldest=None):
        self.calls.append((channel_id, thread_ts, oldest))
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise RateLimited("3", method="conversations.replies")
        if self.explode_on is not None and thread_ts == self.explode_on:
            raise RuntimeError("conversations.replies failed: thread_not_found")
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


class AnsweredFromSummaryTest(SweepHarness):
    """The ingest-time tie: what the polled parent row says about its thread, without a
    platform call. The fake adapter derives the summary from the messages it holds, the
    way Slack puts reply_count / reply_users / latest_reply on a history parent."""

    def polled(self, ts="1.0"):
        """The ask as the poller handed it to the journal — the normalized row."""
        return [msg("C1", ts, "where is the dashboard?")]

    def test_a_replier_other_than_the_asker_answers_at_ingest(self):
        self.ask()
        self.reply("2.0", "B_AGENT")                 # on the platform before the poll
        ties = answered_from_summary(self.adapter, self.journal, "C1", self.polled())
        self.assertEqual(ties, [("1.0", "B_AGENT")])
        at, key = self.responded("1.0")
        self.assertIsNotNone(at, "an ask whose row shows a foreign reply stayed open")
        self.assertEqual(key, "thread-reply:summary:B_AGENT",
                         "the key must name the replier, and carry the thread-reply: "
                         "prefix the dashboard counts thread answers by")
        self.assertEqual(self.adapter.calls, [], "the summary route made a replies() call")

    def test_the_first_foreign_replier_is_named_not_the_asker(self):
        self.ask()
        self.reply("2.0", "U_ASKER", text="bump")
        self.reply("3.0", "U_X")
        self.reply("4.0", "U_Y")
        answered_from_summary(self.adapter, self.journal, "C1", self.polled())
        self.assertEqual(self.responded("1.0")[1], "thread-reply:summary:U_X")

    def test_a_thread_of_the_askers_own_replies_stays_open(self):
        self.ask()
        self.reply("2.0", "U_ASKER", text="any update?")
        ties = answered_from_summary(self.adapter, self.journal, "C1", self.polled())
        self.assertEqual(ties, [])
        self.assertIsNone(self.responded("1.0")[0],
                          "an asker bumping their own thread was read as an answer")

    def test_an_unattributed_replier_answers_nothing(self):
        self.ask()
        self.reply("2.0", "unknown", text="This message was deleted.")
        self.assertEqual(answered_from_summary(self.adapter, self.journal, "C1",
                                               self.polled()), [])
        self.assertIsNone(self.responded("1.0")[0])

    def test_a_count_with_no_attributable_replier_stays_open(self):
        """A summary that says 'replies: 3' and names nobody is not evidence of who
        replied — a replier the platform did not name would be a guess."""
        self.ask()
        self.reply("2.0", "B_AGENT")

        class CountOnly(CountingFake):
            def thread_summary(self, message):
                s = super().thread_summary(message)
                return None if s is None else dict(s, repliers=[])
        adapter = CountOnly()
        adapter.seed(self.adapter.messages)
        self.assertEqual(answered_from_summary(adapter, self.journal, "C1", self.polled()), [])
        self.assertIsNone(self.responded("1.0")[0])

    def test_only_open_asks_are_tied(self):
        self.ask(ts="1.0", kind="STATEMENT", text="the deploy finished")
        self.reply("2.0", "U_X", thread="1.0")
        self.ask(ts="3.0", text="second question?")
        self.journal.mark_responded("C1", "3.0", "outbox-key")
        self.reply("4.0", "U_Y", thread="3.0")
        polled = [msg("C1", "1.0", "the deploy finished"), msg("C1", "3.0", "second question?")]
        self.assertEqual(answered_from_summary(self.adapter, self.journal, "C1", polled), [])
        self.assertIsNone(self.responded("1.0")[0], "a STATEMENT parent was marked answered")
        self.assertEqual(self.responded("3.0")[1], "outbox-key",
                         "an already-answered ask was re-tied")

    def test_a_reply_row_says_nothing_about_its_own_thread(self):
        """History serves a broadcast reply with its thread_id set; the summary of the
        thread belongs to the parent, so a reply row ties nothing."""
        self.ask(ts="1.0", kind="STATEMENT", text="status thread", sender="U_A")
        self.ask(ts="2.0", text="is the dashboard up?", sender="U_B", thread="1.0")
        self.reply("3.0", "U_C", text="yes", thread="1.0")
        polled = [msg("C1", "2.0", "is the dashboard up?", sender="U_B", thread="1.0")]
        self.assertEqual(answered_from_summary(self.adapter, self.journal, "C1", polled), [])
        self.assertIsNone(self.responded("2.0")[0])

    def test_an_adapter_without_thread_summary_is_declared_absent(self):
        self.journal.record("C1", "1.0", sender_id="U", text="where?", kind="QUESTION")
        self.assertIsNone(answered_from_summary(NoRepliesAdapter(), self.journal, "C1",
                                                self.polled()),
                          "an adapter that cannot see a thread summary must read as "
                          "'cannot conclude', never as 'no thread'")
        self.assertIsNone(self.responded("1.0")[0])

    def test_the_tie_is_stamped_at_the_sighting_not_at_the_reply(self):
        """The reply's platform time predates the row's first sighting (the reply was
        there before the poll), so a tie stamped at the reply would make revision 1,
        recorded at the sighting, read as an edit AFTER the response (R23)."""
        self.ask()
        self.reply("0.5", "B_AGENT")      # platform ts before the ask's — as a float
        answered_from_summary(self.adapter, self.journal, "C1", self.polled())
        row = self.journal.get("C1", "1.0")
        self.assertGreaterEqual(row["responded_at"], row["first_seen_at"],
                                "the tie predates the row's first sighting")
        self.assertEqual([(r["channel"], r["ts"]) for r in self.journal.edited_after_response()],
                         [], "a summary-tied ask reads as EDITED AFTER RESPONSE")

    def test_the_fake_adapters_summary_names_repliers_in_reply_order(self):
        a = FakeAdapter()
        a.seed([msg("C1", "1.0", "parent"),
                msg("C1", "2.0", "r1", sender="U_X", thread="1.0"),
                msg("C1", "3.0", "r2", sender="U_Y", thread="1.0"),
                msg("C1", "4.0", "r3", sender="U_X", thread="1.0"),
                msg("C2", "5.0", "other channel", sender="U_Z", thread="1.0")])
        self.assertEqual(a.thread_summary(msg("C1", "1.0", "parent")),
                         {"reply_count": 3, "repliers": ["U_X", "U_Y"], "latest_reply": "4.0"})
        self.assertIsNone(a.thread_summary(msg("C1", "2.0", "r1", sender="U_X", thread="1.0")),
                          "a reply row has no thread summary of its own")
        self.assertIsNone(a.thread_summary(msg("C2", "9.0", "no thread")))


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

    def test_the_fetch_floor_is_always_the_ask_itself(self):
        """R9, applied to threads: the sweep re-fetches the whole thread every cycle and
        lets the store and the journal absorb the re-sightings. Using the newest STORED
        reply as the floor was the inverted dual-write — the store is written inside
        the sweep, the journal only afterwards by the caller, so a crash between the two
        left a stored reply that was never fetched again and never journaled."""
        self.ask()
        self.reply("2.0", "U_ASKER", text="bump")
        sweep(self.adapter, self.store, self.journal, "C1")
        sweep(self.adapter, self.store, self.journal, "C1")
        self.assertEqual([c[2] for c in self.adapter.calls], ["1.0", "1.0"],
                         "the floor moved past the ask — a reply stored but not yet "
                         "journaled would never be returned for journaling again")

    def test_a_reply_stored_but_never_journaled_is_returned_again(self):
        """The crash seam itself: a reply already IN the store (from an interrupted
        sweep, or served by history as a broadcast reply) must still come back in
        result['replies'] so the caller can journal it."""
        self.ask()
        r = self.reply("2.0", "B_AGENT")
        self.store.upsert_messages([r])          # stored, never journaled
        res = sweep(self.adapter, self.store, self.journal, "C1")
        self.assertEqual([m["ts"] for m in res["replies"]], ["2.0"],
                         "a stored-but-unjournaled reply vanished from the sweep's "
                         "output — the unlisted-message bug by another route")

    def test_a_newer_broadcast_bump_does_not_hide_an_earlier_answer(self):
        """Slack serves a reply posted 'also send to channel' through history, so the
        poller stores it with thread_id set BEFORE the sweep ever fetched the thread. With
        a store-derived floor the asker's later broadcast bump hid an earlier real answer
        forever; with the ask as the floor the answer is fetched and ties."""
        self.ask()
        self.reply("2.0", "U_B", text="the real answer")           # platform only
        bump = msg("C1", "3.0", "bump (also sent to channel)", thread="1.0")
        self.adapter.seed([bump])
        self.store.upsert_messages([bump])                         # history stored it
        res = sweep(self.adapter, self.store, self.journal, "C1")
        self.assertEqual(res["answered"], [("1.0", "2.0")],
                         "the earlier in-thread answer was hidden by a newer stored bump")

    def test_a_platform_error_on_one_thread_is_reported_and_the_sweep_goes_on(self):
        """A deleted ask makes conversations.replies answer thread_not_found forever.
        Escaping the sweep would turn one dead thread into a scheduler crash loop that
        also stops every later channel from being polled; swallowing it would be the
        F-2 silent no-op. The sweep records it BY ASK and continues."""
        self.ask(ts="1.0")
        self.ask(ts="2.0", text="second question?")
        self.reply("3.0", "U_X", thread="1.0")
        self.adapter.explode_on = "2.0"
        res = sweep(self.adapter, self.store, self.journal, "C1")
        self.assertEqual(res["swept"], 1)
        self.assertEqual([e[0] for e in res["errors"]], ["2.0"],
                         "the failing ask must be named in the result")
        self.assertIn("thread_not_found", res["errors"][0][1])
        self.assertEqual(res["answered"], [("1.0", "3.0")],
                         "the healthy ask was not swept after the failing one")
        self.assertIsNone(self.responded("2.0")[0])

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

    def test_the_default_bound_is_the_one_the_scheduler_actually_uses(self):
        """SWEEP_MAX is only a bound if a sweep that passes no max_threads honours it —
        the cycle never passes one."""
        for i in range(1, SWEEP_MAX + 2):
            self.ask(ts=f"{i}.0", text=f"question {i}?")
        res = sweep(self.adapter, self.store, self.journal, "C1")
        self.assertEqual(res["swept"], SWEEP_MAX)
        self.assertTrue(res["truncated"])
        self.assertEqual(len(self.adapter.calls), SWEEP_MAX)

    def test_an_earlier_foreign_reply_does_not_answer_a_later_ask_in_the_thread(self):
        """Thread P: U_C reports 'deploy done' at 2.0; U_B then ASKS at 3.0 'is the
        dashboard up?'. Only replies AFTER the ask can answer it — 2.0 is foreign and
        in the same thread, and must not count."""
        self.ask(ts="1.0", kind="STATEMENT", text="status: all green", sender="U_A")
        self.ask(ts="2.0", kind="STATEMENT", text="deploy done", sender="U_C",
                 thread="1.0")
        self.ask(ts="3.0", kind="QUESTION", text="is the dashboard up?", sender="U_B",
                 thread="1.0")
        res = sweep(self.adapter, self.store, self.journal, "C1")
        self.assertEqual(res["answered"], [],
                         "a reply OLDER than the ask was credited as its answer")
        self.assertIsNone(self.responded("3.0")[0])

    def test_a_rate_limit_stops_the_sweep_and_is_reported(self):
        self.ask(ts="1.0")
        self.ask(ts="2.0", text="second question?")
        self.adapter.fail_after = 1
        res = sweep(self.adapter, self.store, self.journal, "C1")
        self.assertEqual(res["swept"], 1)
        self.assertEqual(res["rate_limited"], "conversations.replies",
                         "a 429 must be visible in the result, not swallowed")
        self.assertEqual(res["errors"], [], "a 429 is a hold, not a thread error")

    def test_a_reply_that_is_itself_an_ask_does_not_answer(self):
        """A teammate's follow-up question in the thread ("can you also share the
        logs?") is a new ask, not an answer — crediting it closed the original and its
        owed work (review F7). The caller supplies the classifier's verdict."""
        self.ask()
        self.reply("2.0", "U_B", text="can you also share the logs?")
        self.reply("3.0", "U_C", text="dashboard is on 8502")
        res = sweep(self.adapter, self.store, self.journal, "C1",
                    is_answer=lambda m: not m["text"].rstrip().endswith("?"))
        self.assertEqual(res["answered"], [("1.0", "3.0")],
                         "a follow-up QUESTION was credited as the answer")

    def test_an_unattributed_sender_answers_nothing(self):
        """Adapters map sender-less platform rows (bots, system notices) to the
        placeholder "unknown"; it is truthy, so it must be excluded by name."""
        self.ask()
        self.reply("2.0", "unknown", text="This message was deleted.")
        res = sweep(self.adapter, self.store, self.journal, "C1")
        self.assertEqual(res["answered"], [])
        self.assertIsNone(self.responded("1.0")[0])

    def test_two_asks_in_one_thread_cost_one_platform_call(self):
        self.ask(ts="1.0", kind="STATEMENT", text="status thread", sender="U_A")
        self.ask(ts="2.0", text="is the dashboard up?", sender="U_B", thread="1.0")
        self.ask(ts="3.0", text="and the parity run?", sender="U_C", thread="1.0")
        self.reply("4.0", "U_D", text="both green", thread="1.0")
        # the classifier's verdict, as the scheduler passes it: 3.0 is itself an ask
        res = sweep(self.adapter, self.store, self.journal, "C1",
                    is_answer=lambda m: not m["text"].rstrip().endswith("?"))
        self.assertEqual(len(self.adapter.calls), 1,
                         "every ask under one root issued its own replies() call")
        self.assertEqual(sorted(res["answered"]), [("2.0", "4.0"), ("3.0", "4.0")])

    def test_the_window_rotates_so_older_asks_are_swept_eventually(self):
        """Newest-first with a fixed window starves everything beyond it (review F9):
        an answer to ask #26 is never ingested. The caller keeps a rotating start."""
        for i in range(1, 8):
            self.ask(ts=f"{i}.0", text=f"question {i}?")
        seen, start = set(), 0
        for _ in range(3):
            res = sweep(self.adapter, self.store, self.journal, "C1", max_threads=3,
                        start=start)
            seen.update(c[1] for c in self.adapter.calls)
            start = res["next_start"]
        self.assertEqual(seen, {f"{i}.0" for i in range(1, 8)},
                         "three windows of three over seven asks must have swept them all")

    def test_a_non_orderable_id_space_is_refused_by_name(self):
        """Email Message-IDs are strings; ts_order's string fallback is lexicographic,
        not chronological (review F12). The sweep refuses rather than guesses."""
        self.ask(ts="<msg-1@example>", text="where is the dashboard?")
        res = sweep(self.adapter, self.store, self.journal, "C1")
        self.assertEqual(self.adapter.calls, [], "a non-orderable id space was swept")
        self.assertEqual([e[0] for e in res["errors"]], ["<msg-1@example>"])
        self.assertIn("non-orderable", res["errors"][0][1])

    def test_the_tie_is_stamped_at_the_replys_arrival_not_at_the_sweep(self):
        """An edit sighted between the reply's arrival and the sweep must read as
        edited AFTER the answer (R23), so the tie carries the reply's first-arrival time
        — the engine clock, comparable with revision.recorded_at — never the sweep's."""
        base = Path(self.tmp.name)
        clock = {"t": 1000.0}
        store = Store(base / "clocked.db", clock=lambda: clock["t"])
        self.addCleanup(store.close)
        m = msg("C1", "1.0", "deploy to staging?")
        store.upsert_messages([m]); self.adapter.seed([m])
        self.journal.record("C1", "1.0", sender_id="U_ASKER", text="deploy to staging?",
                            kind="QUESTION")
        clock["t"] = 2000.0                        # the reply arrives (first sighting)
        self.reply("2.0", "B_AGENT", text="deploying to staging now")
        res = sweep(self.adapter, store, self.journal, "C1")
        self.assertEqual(res["answered"], [("1.0", "2.0")])
        self.assertEqual(self.responded("1.0")[0], 2000.0,
                         "the tie must carry the reply's arrival time, not the sweep's")

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
