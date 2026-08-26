"""Fault injection at the three send seams (gate G2; requirements R1, R2, R10).

This is the harness the GA plan says must exist BEFORE the send path, because a bug here
is unrecoverable and customer-visible: it either double-messages a customer in Anton's
name or silently drops a reply.

The seams, taken from the incumbent's actual failure (24 auto-reconciles):
    after_intent     — intent durable, adapter never called
    after_send       — the message IS on the target, we never recorded it  (the nasty one)
    before_readback  — recorded SENT, never proved delivery
    before_commit    — proved delivery, never committed

For every seam: kill the process, construct a NEW Outbox over the same database, run
recover(), and assert **exactly one delivery and zero losses**.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.outbox import (COMMITTED, Outbox, PolicyError, STAGED,  # noqa: E402
                         SendBlocked, _Crash, idempotency_key)

TARGET = "D_EXAMPLE_DM"
TS = "1700000000.000100"
TEXT = "[AGENT] status: three legs proven."


class FakeAdapter:
    """Stands in for a channel. `delivered` is the remote side's ground truth."""

    def __init__(self):
        self.delivered = []      # list of (target, text, key) actually on the target
        self.send_calls = 0

    def send(self, target, text, key=None):
        self.send_calls += 1
        self.delivered.append((target, text, key))
        return {"ts": f"receipt-{self.send_calls}", "key": key}

    def read_back(self, target, key):
        return any(t == target and k == key for t, _, k in self.delivered)


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "outbox.db"
        self.adapter = FakeAdapter()
        self.policies = {TARGET: "direct"}

    def tearDown(self):
        self.tmp.cleanup()

    def box(self):
        """A FRESH Outbox over the same DB — models a new process after a crash."""
        return Outbox(self.db, self.adapter, self.policies)

    def deliveries_of(self, key):
        return [d for d in self.adapter.delivered if d[2] == key]

    # ---- the happy path ---------------------------------------------------
    def test_clean_send_delivers_exactly_once_and_commits(self):
        r = self.box().send(TARGET, TS, TEXT)
        self.assertEqual(r["state"], COMMITTED)
        self.assertEqual(len(self.deliveries_of(r["key"])), 1)

    def test_repeat_send_is_deduped_without_touching_the_adapter(self):
        b = self.box()
        first = b.send(TARGET, TS, TEXT)
        before = self.adapter.send_calls
        second = b.send(TARGET, TS, TEXT)
        self.assertTrue(second["deduped"])
        self.assertEqual(self.adapter.send_calls, before,
                         "a duplicate send reached the adapter")
        self.assertEqual(first["key"], second["key"])

    # ---- THE THREE SEAMS --------------------------------------------------
    def _crash_then_recover(self, seam):
        key = idempotency_key(TARGET, TS, TEXT)
        with self.assertRaises(_Crash):
            self.box().send(TARGET, TS, TEXT, _crash_at=seam)
        # a brand-new process picks up the same durable state
        counts = self.box().recover()
        return key, counts

    def test_seam_after_intent_delivers_exactly_once(self):
        key, counts = self._crash_then_recover("after_intent")
        self.assertEqual(len(self.deliveries_of(key)), 1,
                         "crash after INTENT: expected exactly one delivery, got "
                         f"{len(self.deliveries_of(key))}")
        self.assertEqual(counts["resent"], 1, "recovery should have sent the never-sent message")
        self.assertEqual(self.box().get(key)["state"], COMMITTED)

    def test_seam_after_send_does_not_duplicate(self):
        """The dangerous one: the message landed but we never recorded it."""
        key, counts = self._crash_then_recover("after_send")
        self.assertEqual(len(self.deliveries_of(key)), 1,
                         "crash after adapter.send DUPLICATED the message — this is the "
                         "24-auto-reconcile bug")
        self.assertEqual(counts["already_delivered"], 1,
                         "recovery re-sent instead of proving prior delivery by read-back")
        self.assertEqual(counts["resent"], 0)

    def test_seam_before_readback_does_not_duplicate(self):
        key, counts = self._crash_then_recover("before_readback")
        self.assertEqual(len(self.deliveries_of(key)), 1)
        self.assertEqual(counts["already_delivered"], 1)
        self.assertEqual(self.box().get(key)["state"], COMMITTED)

    def test_seam_before_commit_does_not_duplicate_or_lose(self):
        key = idempotency_key(TARGET, TS, TEXT)
        with self.assertRaises(_Crash):
            self.box().send(TARGET, TS, TEXT, _crash_at="before_commit")
        # VERIFIED rows are not pending work; a later send must dedupe, not re-send
        self.assertEqual(self.box().pending(), [])
        r = self.box().send(TARGET, TS, TEXT)
        self.assertTrue(r["deduped"])
        self.assertEqual(len(self.deliveries_of(key)), 1)

    def test_every_seam_yields_exactly_one_delivery(self):
        """The whole gate in one assertion, across a fresh DB per seam."""
        for seam in ("after_intent", "after_send", "before_readback", "before_commit"):
            with self.subTest(seam=seam):
                self.tearDown()
                self.setUp()
                key = idempotency_key(TARGET, TS, TEXT)
                with self.assertRaises(_Crash):
                    self.box().send(TARGET, TS, TEXT, _crash_at=seam)
                self.box().recover()
                self.box().send(TARGET, TS, TEXT)      # a retry after recovery
                n = len(self.deliveries_of(key))
                self.assertEqual(n, 1, f"seam {seam}: {n} deliveries, expected exactly 1")

    def test_no_message_is_lost_at_any_seam(self):
        for seam in ("after_intent", "after_send", "before_readback", "before_commit"):
            with self.subTest(seam=seam):
                self.tearDown()
                self.setUp()
                key = idempotency_key(TARGET, TS, TEXT)
                with self.assertRaises(_Crash):
                    self.box().send(TARGET, TS, TEXT, _crash_at=seam)
                self.box().recover()
                self.assertTrue(self.adapter.read_back(TARGET, key),
                                f"seam {seam}: message LOST — recovery left it undelivered")

    def test_recovery_is_idempotent(self):
        key, _ = self._crash_then_recover("after_intent")
        self.box().recover()
        self.box().recover()
        self.assertEqual(len(self.deliveries_of(key)), 1,
                         "repeated recovery duplicated the message")

    # ---- policy (R10) -----------------------------------------------------
    def test_unknown_target_defaults_to_never(self):
        with self.assertRaises(PolicyError):
            Outbox(self.db, self.adapter, {}).send("C_UNLISTED", TS, TEXT)
        self.assertEqual(self.adapter.send_calls, 0)

    def test_never_policy_refuses_and_never_calls_the_adapter(self):
        b = Outbox(self.db, self.adapter, {TARGET: "never"})
        with self.assertRaises(PolicyError):
            b.send(TARGET, TS, TEXT)
        self.assertEqual(self.adapter.send_calls, 0,
                         "a 'never' target reached the adapter")

    def test_staged_policy_writes_a_draft_and_never_calls_the_adapter(self):
        b = Outbox(self.db, self.adapter, {TARGET: "staged"})
        r = b.send(TARGET, TS, TEXT)
        self.assertEqual(r["state"], STAGED)
        self.assertEqual(self.adapter.send_calls, 0,
                         "a 'staged' target reached the adapter — the operator gate was bypassed")
        self.assertEqual(len(b.staged()), 1)

    def test_staged_rows_are_not_swept_up_by_recovery(self):
        b = Outbox(self.db, self.adapter, {TARGET: "staged"})
        b.send(TARGET, TS, TEXT)
        counts = Outbox(self.db, self.adapter, {TARGET: "staged"}).recover()
        self.assertEqual(counts["resumed"], 0,
                         "recovery sent a STAGED draft that was waiting on an operator gate")
        self.assertEqual(self.adapter.send_calls, 0)

    # ---- read-back failure ------------------------------------------------
    def test_unprovable_delivery_raises_instead_of_claiming_success(self):
        class Blackhole(FakeAdapter):
            def read_back(self, target, key):
                return False
        b = Outbox(self.db, Blackhole(), self.policies)
        with self.assertRaises(SendBlocked):
            b.send(TARGET, TS, TEXT)

    # ---- concurrent senders (the INTENT insert is the claim) ---------------
    # The 6-thread probe in test_journal.ConcurrencyTest caught this racing in the
    # wild (3 deliveries from 6 senders, fire=13); these two force each window
    # deterministically so the property can never again hold only by timing luck.

    def test_a_second_sender_mid_flight_does_not_double_deliver(self):
        """While one sender is inside adapter.send, a second sender of the SAME
        message must not reach the adapter — its row is INTENT, and only read-back
        (recover()'s job) can ever prove whether that in-flight send landed."""
        import threading
        in_flight, release = threading.Event(), threading.Event()
        outer = self

        class MidFlight(FakeAdapter):
            def send(self, target, text, key=None):
                in_flight.set()
                release.wait(5)
                return super().send(target, text, key=key)

        adapter = MidFlight()

        def claimant():   # own Outbox in-thread: sqlite connections are thread-bound
            Outbox(outer.db, adapter, outer.policies).send(TARGET, TS, TEXT)

        t = threading.Thread(target=claimant)
        t.start()
        try:
            self.assertTrue(in_flight.wait(5), "claimant never reached the adapter")
            r = Outbox(self.db, adapter, self.policies).send(TARGET, TS, TEXT)
        finally:
            release.set()
            t.join(5)
        self.assertEqual(adapter.send_calls, 1,
                         "a second sender reached the adapter while the first was "
                         "mid-flight — this double-messages a customer")
        self.assertTrue(r.get("in_flight"),
                        "the losing sender must say the send is in flight, not "
                        "pretend it delivered")
        self.assertEqual(r["state"], "INTENT")

    def test_losing_the_intent_insert_race_neither_raises_nor_double_delivers(self):
        """The SELECT→INSERT window: our get() saw no row, but another sender's
        INSERT lands before ours. The primary key must arbitrate to a clean dedupe,
        not an IntegrityError crash and not a second delivery."""
        winner = self.box()
        winner.send(TARGET, TS, TEXT)          # the racing winner completed first

        class StaleRead(Outbox):
            stale = True

            def get(self, key):
                if StaleRead.stale:            # our SELECT ran before their INSERT
                    StaleRead.stale = False
                    return None
                return super().get(key)

        r = StaleRead(self.db, self.adapter, self.policies).send(TARGET, TS, TEXT)
        self.assertEqual(self.adapter.send_calls, 1,
                         "losing the insert race re-delivered the message")
        self.assertTrue(r["deduped"])
        self.assertEqual(r["state"], COMMITTED)

    # ---- idempotency key --------------------------------------------------
    def test_key_is_stable_and_content_sensitive(self):
        k1 = idempotency_key(TARGET, TS, TEXT)
        self.assertEqual(k1, idempotency_key(TARGET, TS, TEXT))
        self.assertNotEqual(k1, idempotency_key(TARGET, TS, TEXT + "!"))
        self.assertNotEqual(k1, idempotency_key("OTHER", TS, TEXT))
        self.assertNotEqual(k1, idempotency_key(TARGET, "1700000009.9", TEXT))


if __name__ == "__main__":
    unittest.main(verbosity=2)
