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
        return cls(auth={"token": "xoxb-stub", "channels": f"{CH},{DM}"},
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
        self.assertIn("<http://127.0.0.1:8502>", shown["text"],
                      "the stub stores the URL wrapped the way Slack does")
        self.assertNotEqual(shown["text"], TEXT, "read-back must have compared canonically")
        for method, form, auth in self.stub.requests:
            self.assertEqual(auth, "Bearer xoxb-stub")
            self.assertNotIn("token", form)

    def test_a_second_click_dedupes_instead_of_posting_twice(self):
        box = self.box()
        key = box.stage(CH, ASK_TS, TEXT, thread_id=ASK_TS)["key"]
        box.release(key)
        again = box.release(key)
        self.assertTrue(again["deduped"])
        self.assertEqual(len(self.posts()), 1)

    # ---- guard 3: read-back must prove the SAME text ------------------------------------
    def test_a_truncated_post_is_parked_as_a_mismatch_and_recovery_never_re_sends_it(self):
        """Review F3: the message IS on the platform, with text that is not the approved
        text. Before, a failed exact read-back read as 'never sent': an in-process
        recover() posted a duplicate and a new process committed the mangled text."""
        from core.outbox import MISMATCH
        self.stub.truncate_posts_to = 20
        box = self.box()
        key = box.stage(CH, ASK_TS, TEXT, thread_id=ASK_TS)["key"]
        with self.assertRaises(SendBlocked) as cm:
            box.release(key)
        self.assertIn("DIFFERENT text", str(cm.exception))
        self.assertEqual(box.get(key)["state"], MISMATCH)
        self.assertEqual(len(self.posts()), 1)
        box.recover()                                           # same process
        self.assertEqual((box.get(key)["state"], len(self.posts())), (MISMATCH, 1))
        counts = self.box().recover()                           # a new process
        self.assertEqual((counts["resumed"], box.get(key)["state"], len(self.posts())),
                         (0, MISMATCH, 1), "recovery re-posted or committed a mangled post")

    def test_a_dead_thread_does_not_wedge_the_other_approved_sends(self):
        """Review F7: the asker deleted the parent of one approved thread reply; its
        read-back raises thread_not_found forever. The DM reply approved next to it
        must still be delivered, and the dead row reported, not retried as a wedge."""
        box = self.box()
        from core.outbox import _Crash, INTENT
        dead = box.stage(CH, ASK_TS, TEXT, thread_id=ASK_TS)["key"]
        dm = box.stage(DM, "1699999999.000200", "[AGENT] yes, it is up")["key"]
        for k in (dead, dm):
            with self.assertRaises(_Crash):
                box.release(k, _crash_at="after_intent")
        self.stub.fail_always["conversations.replies"] = "thread_not_found"
        counts = self.box().recover()
        self.assertEqual(self.box().get(dm)["state"], COMMITTED, "the DM reply was wedged")
        # the thread's parent is gone: parked for a human at once (round 3), never retried
        from core.outbox import UNPROVEN
        self.assertEqual(self.box().get(dead)["state"], UNPROVEN)
        self.assertEqual([k for k, _ in counts["errors"]], [dead])
        self.assertIn("thread_not_found", counts["errors"][0][1])
        self.assertEqual(len(self.stub.posted(DM)), 1)
        self.assertEqual(len(self.stub.posted(CH)), 0, "a dead thread must never be posted to top-level")

    def test_a_reply_naming_a_bare_domain_or_an_email_reads_back_exactly(self):
        """Review F8: Slack auto-links `docs.example` as <http://docs.example|docs.example>
        and an email as <mailto:x|x>; the canonical comparison must see the typed text."""
        box = self.box()
        key = box.stage(CH, ASK_TS, "[AGENT] docs are at docs.example, or mail ops@example.com",
                        thread_id=ASK_TS)["key"]
        self.assertEqual(box.release(key)["state"], COMMITTED)

    # ---- review round 2: bounded recovery, refused posts, the restart half of F3 ----------
    def test_an_unprovable_send_is_re_sent_once_and_then_parked(self):
        """Review R2-6: the post lands but the platform drops its metadata, so no read-back
        can ever find the key. recover() used to post another copy on EVERY run — the
        exact thing the surface told the operator to run. Now: one re-send, then UNPROVEN."""
        from core.outbox import UNPROVEN
        self.stub.drop_post_metadata = True
        box = self.box()
        key = box.stage(CH, ASK_TS, TEXT, thread_id=ASK_TS)["key"]
        with self.assertRaises(SendBlocked):
            box.release(key)
        self.assertEqual((box.get(key)["state"], len(self.posts())), (SENT, 1))
        counts = self.box().recover()                           # run 1: the one re-send, left SENT
        self.assertEqual((counts["resent"], counts["unproven"]), (1, 0))
        self.assertEqual((self.box().get(key)["state"], len(self.posts())), (SENT, 2))
        counts = self.box().recover()                           # run 2: still no key -> parked
        self.assertEqual((counts["resent"], counts["unproven"]), (0, 1))
        self.assertEqual((self.box().get(key)["state"], len(self.posts())), (UNPROVEN, 2))
        for _ in range(3):                                      # later runs: nothing
            self.assertEqual(self.box().recover()["resumed"], 0)
        self.assertEqual(len(self.posts()), 2, "recovery posted a third copy")

    def test_a_refused_post_hands_the_draft_back_to_the_gate(self):
        """Review R2-8: Slack answered ok:false (the bot is not in the channel), so nothing
        was posted — like a 429, the approval returns to STAGED instead of stranding in
        INTENT with no Send and no Discard."""
        from core.delivery import NotPosted
        from core.outbox import DISCARDED
        self.stub.fail_next["chat.postMessage"] = "not_in_channel"
        box = self.box()
        key = box.stage(CH, ASK_TS, TEXT, thread_id=ASK_TS)["key"]
        with self.assertRaises(NotPosted) as cm:
            box.release(key)
        self.assertIn("not_in_channel", str(cm.exception))
        self.assertEqual((box.get(key)["state"], self.stub.posted(CH)), (STAGED, []))
        self.assertEqual(box.discard(key)["state"], DISCARDED)

    def test_a_refused_re_send_in_recovery_hands_an_intent_row_back(self):
        from core.outbox import _Crash
        box = self.box()
        key = box.stage(CH, ASK_TS, TEXT, thread_id=ASK_TS)["key"]
        with self.assertRaises(_Crash):
            box.release(key, _crash_at="after_intent")
        self.stub.fail_next["chat.postMessage"] = "is_archived"
        counts = self.box().recover()
        self.assertEqual([k for k, _ in counts["errors"]], [key])
        self.assertIn("is_archived", counts["errors"][0][1])
        self.assertEqual((self.box().get(key)["state"], self.stub.posted(CH)), (STAGED, []))

    def test_an_ambiguous_answer_to_the_post_leaves_the_row_in_flight_and_never_duplicates(self):
        """Review round 3 (R3-3): internal_error, fatal_error and a gateway 5xx are answers
        Slack documents as possibly applied in part. Treating them as 'not posted' handed a
        LANDED post back to the gate, and the next click — unstoppable by guard 2, since
        the landed post carries the engine's marker — posted a duplicate. Such a row stays
        in flight; recovery's read-back finds the post and commits it, one post in all."""
        from core.delivery import NotPosted
        for n, error in enumerate(("internal_error", "fatal_error")):
            with self.subTest(error=error):
                ask = f"1699999999.00020{n}"                     # its own ask, its own row
                self.stub.seed(CH, "and the logs?", user="U_A", ts=ask)
                self.stub.land_and_fail_next["chat.postMessage"] = error
                box = self.box()
                key = box.stage(CH, ask, TEXT, thread_id=ask)["key"]
                posts_before = len(self.stub.posted(CH))
                with self.assertRaises(RuntimeError) as cm:     # the adapter's own ApiError
                    box.release(key)
                self.assertEqual(type(cm.exception).__name__, "ApiError")
                self.assertNotIsInstance(cm.exception, NotPosted)
                self.assertEqual((box.get(key)["state"], len(self.stub.posted(CH))),
                                 (INTENT, posts_before + 1))
                again = self.box().release(key)                 # the human clicks again
                self.assertEqual((again.get("in_flight"), len(self.stub.posted(CH))),
                                 (True, posts_before + 1), "the second click posted a duplicate")
                counts = self.box().recover()
                self.assertEqual((counts["already_delivered"], counts["resent"]), (1, 0))
                self.assertEqual((self.box().get(key)["state"], len(self.stub.posted(CH))),
                                 (COMMITTED, posts_before + 1))

    def test_a_dead_thread_parks_the_row_at_once_and_an_outage_never_does(self):
        """Review round 3 (R3-5 a) and round 1's F7: the target being GONE (the thread's
        parent deleted) parks the row on the first run — nothing will ever place it — while
        a transient outage (service_unavailable on the read-back) is retried, run after run,
        never parked and never posted."""
        from core.outbox import UNPROVEN, _Crash
        box = self.box()
        dead = box.stage(CH, ASK_TS, TEXT, thread_id=ASK_TS)["key"]
        with self.assertRaises(_Crash):
            box.release(dead, _crash_at="after_intent")
        self.stub.fail_always["conversations.replies"] = "service_unavailable"
        for _ in range(4):                                       # an outage, four runs long
            counts = self.box().recover()
            self.assertIn("service_unavailable", counts["errors"][0][1])
            self.assertEqual((self.box().get(dead)["state"], counts["unproven"]), (INTENT, 0),
                             "a transient outage parked a reply that was never posted")
        self.assertEqual(self.stub.posted(CH), [])
        self.stub.fail_always["conversations.replies"] = "thread_not_found"
        counts = self.box().recover()
        self.assertEqual((self.box().get(dead)["state"], counts["unproven"]), (UNPROVEN, 1))
        self.assertIn("gone", counts["errors"][0][1])
        self.assertEqual(self.stub.posted(CH), [], "a dead thread was posted to, top-level or not")
        self.assertEqual(self.box().recover()["resumed"], 0)

    def test_a_sent_row_whose_re_send_is_refused_is_parked_not_retried_forever(self):
        """Review round 3 (R3-5 d): an unproven earlier post AND a refused re-send — nothing
        recovery can do settles it, so it is parked for a human instead of refused again on
        every run."""
        from core.outbox import UNPROVEN, _Crash
        self.stub.drop_post_metadata = True                      # the first post is unprovable
        box = self.box()
        key = box.stage(CH, ASK_TS, TEXT, thread_id=ASK_TS)["key"]
        with self.assertRaises(_Crash):
            box.release(key, _crash_at="before_readback")
        self.assertEqual((box.get(key)["state"], len(self.posts())), (SENT, 1))
        self.stub.fail_always["chat.postMessage"] = "not_in_channel"
        counts = self.box().recover()
        self.assertEqual((self.box().get(key)["state"], counts["unproven"]), (UNPROVEN, 1))
        self.assertTrue(any("refuses a re-send" in why for _, why in counts["errors"]), counts["errors"])
        self.assertEqual(self.box().recover()["resumed"], 0, "parked, yet touched again")

    def test_an_unlisted_refusal_is_handed_back_once_recovery_has_proven_the_key_absent(self):
        """Review round 4 (R4-1): the allowlist of refusals cannot be complete. At the click a
        refusal it does not know keeps the row in flight (nothing is known yet); in recovery,
        right after a read-back proved the key absent, the same refusal of the re-send is
        definite — the row goes back to the gate instead of being refused on every run."""
        # a Slack-shaped refusal the allowlist does not know (any new or unlisted code)
        self.stub.fail_always["chat.postMessage"] = "org_login_required"
        box = self.box()
        key = box.stage(CH, ASK_TS, TEXT, thread_id=ASK_TS)["key"]
        with self.assertRaises(RuntimeError) as cm:
            box.release(key)
        self.assertEqual(type(cm.exception).__name__, "ApiError")
        self.assertEqual(box.get(key)["state"], INTENT)
        counts = self.box().recover()
        self.assertEqual((self.box().get(key)["state"], self.stub.posted(CH)), (STAGED, []))
        self.assertTrue(any("re-send refused" in why for _, why in counts["errors"]), counts["errors"])
        self.assertEqual(self.box().recover()["resumed"], 0)

    def test_an_ambiguous_answer_to_recoverys_own_re_send_is_recorded_so_the_next_run_parks(self):
        """Review round 4 (R4-2): the re-send LANDED but Slack answered internal_error, and
        the platform keeps no metadata. Unrecorded, every run re-sent again — unbounded
        posting under a double fault. The re-send is recorded on an ambiguous answer, so the
        next run parks the row: two posts in all."""
        from core.outbox import UNPROVEN, _Crash
        self.stub.drop_post_metadata = True
        box = self.box()
        key = box.stage(CH, ASK_TS, TEXT, thread_id=ASK_TS)["key"]
        with self.assertRaises(_Crash):
            box.release(key, _crash_at="after_intent")             # never posted at all
        self.stub.land_and_fail_next["chat.postMessage"] = "internal_error"
        counts = self.box().recover()                              # run 1: re-send lands, answer ambiguous
        self.assertEqual(len(self.posts()), 1)
        row = self.box().get(key)
        self.assertEqual(row["state"], SENT)
        self.assertIsNotNone(row["resent_at"], "the ambiguous re-send was not recorded")
        self.assertTrue(any("internal_error" in why for _, why in counts["errors"]))
        counts = self.box().recover()                              # run 2: still no key -> parked
        self.assertEqual((self.box().get(key)["state"], counts["unproven"], len(self.posts())),
                         (UNPROVEN, 1, 1))
        for _ in range(3):
            self.assertEqual(self.box().recover()["resumed"], 0)
        self.assertEqual(len(self.posts()), 1, "recovery posted again")

    def test_a_crash_before_read_back_on_a_truncated_post_recovers_to_mismatch(self):
        """Review R2-7 (Y11), the restart half of F3: a new process must compare the
        approved text too, or it COMMITs the mangled post it finds by key."""
        from core.outbox import MISMATCH, _Crash
        self.stub.truncate_posts_to = 20
        box = self.box()
        key = box.stage(CH, ASK_TS, TEXT, thread_id=ASK_TS)["key"]
        with self.assertRaises(_Crash):
            box.release(key, _crash_at="before_readback")
        counts = self.box().recover()                           # a new process
        self.assertEqual((counts["mismatched"], counts["resent"]), (1, 0))
        self.assertEqual((self.box().get(key)["state"], len(self.posts())), (MISMATCH, 1),
                         "a restart committed, or re-sent, a mangled post")

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

    # ---- exactly once under a 429: the draft goes back to the gate, the next click posts ----
    def test_a_429_on_the_post_hands_the_draft_back_to_the_gate_and_the_next_click_posts_once(self):
        """Review N4: a 429 is answered before the platform posts, so nothing is on the
        platform and the approval is not lost — the draft returns to STAGED for another
        click. Before, it stranded in INTENT with no Send and no Discard on the surface,
        and only a Python `Outbox.recover()` from the RUNBOOK could finish it."""
        box = self.box()
        key = box.stage(CH, ASK_TS, TEXT, thread_id=ASK_TS)["key"]
        self.stub.rate_limit["chat.postMessage"] = "3"
        with self.assertRaises(RateLimited) as cm:
            box.release(key)
        self.assertNotIn("None", str(cm.exception))
        self.assertEqual(box.get(key)["state"], STAGED)
        self.assertEqual(self.stub.posted(CH), [], "the 429'd call posted nothing")
        self.assertEqual(self.box().recover()["resumed"], 0, "nothing is in flight")
        self.assertEqual(self.box().release(key)["state"], COMMITTED)      # the next click
        self.assertEqual(len(self.stub.posted(CH)), 1)

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
