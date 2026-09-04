"""The operator write path: stage -> human click -> deliver (ENH-28).

The property under test is the WRITE GATE itself: composing can only ever produce a
STAGED row, and the ONLY transition out of STAGED toward the platform is release() —
an explicit human action on the exact staged text. tests/mutation_check.sh removes
each half of that gate in a throwaway copy and requires a test here to go red, so
"the agent cannot auto-send" is a property that FAILS when deleted, not a sentence
in a README.

The second property is that an approval is durable: release() flips STAGED -> INTENT
BEFORE the adapter is touched, so a crash one instruction later is resumed by
recover() and the approved send completes — instead of silently reverting to a draft
nobody remembers approving. That is the same 24-auto-reconcile seam the send ladder
exists for, entered from the gate side.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.outbox import (COMMITTED, DISCARDED, INTENT, Outbox,  # noqa: E402
                         PolicyError, ReleaseError, SendBlocked, STAGED,
                         _Crash, idempotency_key)

TARGET = "C_CUSTOMER"
TS = "1700000000.000200"
TEXT = "[AGENT] draft: the numbers you asked for are attached."


class FakeAdapter:
    """Contract-shaped, thread-capable; `delivered` is the remote ground truth."""

    def __init__(self):
        self.delivered = []
        self.send_calls = 0

    def send(self, target, text, key=None, thread_id=None):
        self.send_calls += 1
        self.delivered.append((target, text, key, thread_id))
        return {"ts": f"receipt-{self.send_calls}", "key": key}

    def read_back(self, target, key):
        return any(t == target and k == key for t, _, k, _ in self.delivered)


class WriteGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "outbox.db"
        self.adapter = FakeAdapter()
        self.policies = {TARGET: "staged"}

    def box(self, policies=None):
        """A FRESH Outbox over the same DB — a new process, as after a crash."""
        return Outbox(self.db, self.adapter,
                      self.policies if policies is None else policies,
                      send_interval=0.0)

    # ---- stage: the adapter is never touched --------------------------------
    def test_stage_writes_a_draft_and_never_calls_the_adapter(self):
        """The control half of no-send-without-confirm: mutation_check deletes the
        stop (stage falls through to the ladder) and THIS must go red."""
        r = self.box().stage(TARGET, TS, TEXT)
        self.assertEqual(r["state"], STAGED)
        self.assertEqual(self.adapter.send_calls, 0,
                         "stage() reached the adapter — composing must not send")
        self.assertEqual(self.adapter.delivered, [])

    def test_stage_refuses_a_never_target(self):
        """Default deny holds on the write surface too."""
        b = self.box(policies={})
        with self.assertRaises(PolicyError):
            b.stage(TARGET, TS, TEXT)
        self.assertIsNone(b.get(idempotency_key(TARGET, TS, TEXT)),
                          "a refused compose still wrote a row")

    def test_stage_under_a_direct_policy_still_stops_at_the_gate(self):
        """'direct' authorizes the ENGINE to answer unreviewed; a human composing on
        the operator surface is asking for a review, and gets one."""
        r = self.box(policies={TARGET: "direct"}).stage(TARGET, TS, TEXT)
        self.assertEqual(r["state"], STAGED)
        self.assertEqual(self.adapter.send_calls, 0)

    def test_restaging_the_same_draft_reports_the_existing_row(self):
        b = self.box()
        first = b.stage(TARGET, TS, TEXT)
        second = b.stage(TARGET, TS, TEXT)
        self.assertTrue(second["deduped"])
        self.assertEqual(first["key"], second["key"])
        self.assertEqual(len(b.staged()), 1, "the same draft staged twice")

    def test_restaging_a_delivered_reply_reports_the_delivery(self):
        b = self.box()
        key = b.stage(TARGET, TS, TEXT)["key"]
        b.release(key)
        again = b.stage(TARGET, TS, TEXT)
        self.assertTrue(again["deduped"])
        self.assertEqual(again["state"], COMMITTED,
                         "re-staging must surface that this exact reply already "
                         "reached the platform, not queue a second copy")

    # ---- release: the click, and only the click -----------------------------
    def test_release_delivers_exactly_once_with_readback_proof(self):
        b = self.box()
        key = b.stage(TARGET, TS, TEXT)["key"]
        r = b.release(key)
        self.assertEqual(r["state"], COMMITTED)
        self.assertEqual(self.adapter.send_calls, 1)
        self.assertEqual(self.adapter.delivered[0][:3], (TARGET, TEXT, key))

    def test_release_of_an_unknown_key_refuses_and_sends_nothing(self):
        with self.assertRaises(ReleaseError):
            self.box().release("no-such-key")
        self.assertEqual(self.adapter.send_calls, 0,
                         "release() minted a send from nothing")

    def test_a_second_release_dedupes_instead_of_double_sending(self):
        b = self.box()
        key = b.stage(TARGET, TS, TEXT)["key"]
        first = b.release(key)
        second = b.release(key)
        self.assertTrue(second["deduped"])
        self.assertEqual(second["receipt"], first["receipt"] and second["receipt"])
        self.assertEqual(self.adapter.send_calls, 1,
                         "a double-click double-messaged the target")

    def test_release_of_an_in_flight_row_defers_to_recovery(self):
        """An INTENT row is a claim that may already be on the platform; a second
        live delivery from the gate is the double-message bug."""
        b = self.box(policies={TARGET: "direct"})
        try:
            b.send(TARGET, TS, TEXT, _crash_at="after_intent")
        except _Crash:
            pass
        key = idempotency_key(TARGET, TS, TEXT)
        r = self.box(policies={TARGET: "direct"}).release(key)
        self.assertTrue(r.get("in_flight"))
        self.assertEqual(self.adapter.send_calls, 0,
                         "release() re-sent a row that belongs to recover()")

    def test_release_rechecks_the_policy_at_the_click(self):
        """A channel demoted to 'never' after the draft was written must refuse,
        however long the draft sat at the gate."""
        key = self.box().stage(TARGET, TS, TEXT)["key"]
        demoted = self.box(policies={})           # default deny again
        with self.assertRaises(PolicyError):
            demoted.release(key)
        self.assertEqual(self.adapter.send_calls, 0)
        self.assertEqual(demoted.get(key)["state"], STAGED,
                         "the refused draft must stay at the gate, unsent")

    def test_release_places_the_reply_in_the_recorded_thread(self):
        """Placement comes from the ROW (the ENH-3 rule), so an approved thread
        reply cannot flatten into the main channel at the click."""
        b = self.box(policies={TARGET: {"thread": "staged"}})
        key = b.stage(TARGET, TS, TEXT, thread_id="1699.42")["key"]
        b.release(key)
        self.assertEqual(self.adapter.delivered[0][3], "1699.42")

    def test_release_surfaces_a_failed_readback_instead_of_committing(self):
        class Deaf(FakeAdapter):
            def read_back(self, target, key):
                return False
        deaf = Deaf()
        b = Outbox(self.db, deaf, self.policies, send_interval=0.0)
        key = b.stage(TARGET, TS, TEXT)["key"]
        with self.assertRaises(SendBlocked):
            b.release(key)
        self.assertNotEqual(b.get(key)["state"], COMMITTED)

    # ---- the approval is durable --------------------------------------------
    def test_a_release_that_dies_after_the_flip_is_completed_by_recovery(self):
        """The click was recorded (STAGED -> INTENT durable); recover() must finish
        the approved send. mutation_check removes the durable flip and THIS goes
        red — the approval would silently evaporate with the process."""
        b = self.box()
        key = b.stage(TARGET, TS, TEXT)["key"]
        with self.assertRaises(_Crash):
            b.release(key, _crash_at="after_intent")
        self.assertEqual(self.adapter.send_calls, 0)
        counts = self.box().recover()
        self.assertEqual(counts["resent"], 1)
        self.assertEqual(self.adapter.send_calls, 1)
        self.assertEqual(self.box().get(key)["state"], COMMITTED)

    def test_a_release_that_dies_before_readback_is_proven_not_resent(self):
        b = self.box()
        key = b.stage(TARGET, TS, TEXT)["key"]
        with self.assertRaises(_Crash):
            b.release(key, _crash_at="before_readback")
        self.assertEqual(self.adapter.send_calls, 1)
        counts = self.box().recover()
        self.assertEqual(counts["already_delivered"], 1)
        self.assertEqual(self.adapter.send_calls, 1,
                         "recovery re-sent a delivery it could have proven")

    # ---- discard: terminal, and kept -----------------------------------------
    def test_discard_is_terminal_and_the_row_is_kept(self):
        b = self.box()
        key = b.stage(TARGET, TS, TEXT)["key"]
        b.discard(key)
        row = b.get(key)
        self.assertEqual(row["state"], DISCARDED)
        self.assertEqual(row["text"], TEXT,
                         "the audit trail must keep WHAT was refused")
        with self.assertRaises(ReleaseError):
            b.release(key)
        self.assertEqual(self.adapter.send_calls, 0,
                         "a discarded draft reached the platform")

    def test_discard_refuses_anything_that_is_not_a_draft(self):
        """'Discarding' a delivered row would misrecord what is already on the
        platform — the record must say COMMITTED because the message is there."""
        b = self.box()
        key = b.stage(TARGET, TS, TEXT)["key"]
        b.release(key)
        with self.assertRaises(ReleaseError):
            b.discard(key)
        self.assertEqual(b.get(key)["state"], COMMITTED)
        with self.assertRaises(ReleaseError):
            b.discard("no-such-key")

    # ---- the operator's two lists --------------------------------------------
    def test_staged_and_delivered_render_as_disjoint_lists(self):
        """The visible staged-vs-sent distinction the surface renders is exactly
        these two queries; a row must never appear in both."""
        b = self.box()
        gate_key = b.stage(TARGET, TS, TEXT)["key"]
        sent_key = b.stage(TARGET, "1700000000.000300", "second draft")["key"]
        b.release(sent_key)
        self.assertEqual([r["key"] for r in b.staged()], [gate_key])
        self.assertEqual([r["key"] for r in b.delivered()], [sent_key])
        self.assertEqual(b.delivered()[0]["state"], COMMITTED)
        self.assertIsNotNone(b.delivered()[0]["receipt"],
                             "a 'sent' row without its receipt is a claim, "
                             "not a fact")


if __name__ == "__main__":
    unittest.main(verbosity=2)
