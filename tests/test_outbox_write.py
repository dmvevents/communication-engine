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
import time
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



class NoSendAdapter:
    """A read-only adapter in the contract's shape: capabilities() says send is False and
    there is no send attribute at all (every shipped live adapter)."""

    def __init__(self):
        self.touched = 0

    def capabilities(self):
        return {"read": True, "history": True, "search": False, "send": False,
                "react": False, "threads": False}

    def read_back(self, target, key):
        self.touched += 1
        return False


class ReleaseRaceTest(unittest.TestCase):
    """The click is a CLAIM, not a read-then-write (independent merge review, B1/B3).

    release() used to read STAGED and then UPDATE unconditionally, so two Sends landing
    between the read and the write both reached the adapter — one approved draft, two
    messages — and a Discard interleaved with a Send still sent. The flip is now a
    compare-and-swap on state; the loser reads what the row became and reports it.
    And a draft whose adapter cannot send is refused BEFORE the flip: flipping first and
    failing in the adapter wedged the row in INTENT, undiscardable and unrecoverable."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "outbox.db"
        self.adapter = FakeAdapter()

    def box(self):
        return Outbox(self.db, self.adapter, {TARGET: "staged"}, send_interval=0.0)

    def test_two_concurrent_releases_deliver_exactly_once(self):
        a, b = self.box(), self.box()
        key = a.stage(TARGET, TS, TEXT)["key"]
        fired = []
        real = a.policy_for

        def interleave(target, scope="channel"):
            # Another tab's click lands between a's read and a's flip.
            if not fired:
                fired.append(1)
                b.release(key)
            return real(target, scope)

        a.policy_for = interleave
        res = a.release(key)
        n = sum(1 for d in self.adapter.delivered if d[2] == key)
        self.assertEqual(n, 1, f"ONE approved draft was delivered {n} times")
        self.assertTrue(res.get("deduped") or res.get("in_flight"),
                        "the losing click must report what the row became, not "
                        "pretend it sent")
        self.assertEqual(a.get(key)["state"], COMMITTED)

    def test_a_discard_interleaved_with_a_send_sends_nothing(self):
        a, b = self.box(), self.box()
        key = a.stage(TARGET, TS, TEXT)["key"]
        fired = []
        real = a.policy_for

        def interleave(target, scope="channel"):
            if not fired:
                fired.append(1)
                b.discard(key)
            return real(target, scope)

        a.policy_for = interleave
        with self.assertRaises(ReleaseError):
            a.release(key)
        self.assertEqual(self.adapter.delivered, [], "a DISCARDED draft was delivered")
        self.assertEqual(a.get(key)["state"], DISCARDED)

    def test_a_send_interleaved_with_a_discard_keeps_the_delivery_record(self):
        a, b = self.box(), self.box()
        key = a.stage(TARGET, TS, TEXT)["key"]
        b.release(key)                      # delivered first
        with self.assertRaises(ReleaseError):
            a.discard(key)                  # must not relabel a delivered row
        self.assertEqual(a.get(key)["state"], COMMITTED)

    def test_release_refuses_before_the_flip_when_the_adapter_cannot_send(self):
        ro = NoSendAdapter()
        ob = Outbox(self.db, ro, {TARGET: "staged"}, send_interval=0.0)
        key = ob.stage(TARGET, TS, TEXT)["key"]
        with self.assertRaises(ReleaseError) as ctx:
            ob.release(key)
        self.assertIn("send", str(ctx.exception).lower())
        self.assertEqual(ob.get(key)["state"], STAGED,
                         "the row left STAGED — a wedge in INTENT is undiscardable")
        self.assertEqual(ro.touched, 0, "the adapter was touched by a refused release")
        ob.discard(key)                     # still a draft: still discardable
        self.assertEqual(ob.get(key)["state"], DISCARDED)


class NoThreadsAdapter:
    """Send-capable but thread-INCAPABLE, in a shape the contract permits: send() takes
    no thread_id (channels/CONTRACT.md), and capabilities() says threads are off."""

    def __init__(self):
        self.delivered = []

    def capabilities(self):
        return {"read": True, "history": True, "search": False, "send": True,
                "react": False, "threads": False}

    def send(self, target, text, key=None):
        self.delivered.append((target, text, key))
        return {"ts": f"receipt-{len(self.delivered)}", "key": key}

    def read_back(self, target, key):
        return any(t == target and k == key for t, _, k in self.delivered)


class ThreadPlacementRefusalTest(unittest.TestCase):
    """A thread reply the adapter cannot place is refused BEFORE anything is written or
    claimed (independent delta review 2026-10-13, D1 — B3's class on the thread
    dimension).

    _deliver() refused it already, but release() had flipped STAGED -> INTENT first, so
    the row wedged: Discard refuses an INTENT row, and recover() re-ran the same refusal
    forever. Nothing here offers top-level instead — flattening a thread reply into the
    main channel is the one outcome a thread-scoped policy forbids. No shipped adapter
    has this shape; any adopter's may."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "outbox.db"
        self.adapter = NoThreadsAdapter()

    def box(self, adapter=None, policy="staged"):
        return Outbox(self.db, adapter or self.adapter, {TARGET: policy},
                      send_interval=0.0)

    def test_release_refuses_before_the_flip_and_the_draft_stays_discardable(self):
        # Staged while the connection's adapter could thread; the adapter was then
        # swapped for one that cannot (the connection-change shape review S3 describes).
        key = self.box(FakeAdapter()).stage(TARGET, TS, TEXT, thread_id=TS)["key"]
        box = self.box()
        with self.assertRaises(ReleaseError) as cm:
            box.release(key)
        self.assertIn("adapter NoThreadsAdapter cannot place a reply in thread",
                      str(cm.exception), "a test double is named by its class")
        self.assertEqual(box.get(key)["state"], STAGED,
                         "the refused thread draft left STAGED — wedged in INTENT, "
                         "undiscardable, and unrecoverable with the same adapter")
        self.assertEqual(self.adapter.delivered, [], "the adapter was called")
        self.assertEqual(box.discard(key)["state"], DISCARDED)
        box.recover()                                   # nothing in flight: no raise
        self.assertEqual(box.get(key)["state"], DISCARDED)

    def test_a_direct_thread_send_writes_no_row_the_adapter_could_never_deliver(self):
        box = self.box(policy="direct")
        with self.assertRaises(SendBlocked):
            box.send(TARGET, TS, TEXT, thread_id=TS)
        self.assertIsNone(box.get(idempotency_key(TARGET, TS, TEXT, TS)),
                          "an INTENT row was written for a send the adapter could never "
                          "place — recover() would refuse it on every restart")
        self.assertEqual(self.adapter.delivered, [])

    def test_stage_refuses_a_thread_draft_the_adapter_could_never_place(self):
        with self.assertRaises(SendBlocked):
            self.box().stage(TARGET, TS, TEXT, thread_id=TS)
        self.assertEqual(self.box().staged(), [],
                         "a draft no click could ever send was put at the gate")

    def test_top_level_replies_through_the_same_adapter_still_flow(self):
        box = self.box()
        key = box.stage(TARGET, TS, TEXT)["key"]
        self.assertEqual(box.release(key)["state"], COMMITTED)
        self.assertEqual([d[:2] for d in self.adapter.delivered], [(TARGET, TEXT)])

    def test_a_declared_no_threads_adapter_is_refused_even_with_a_kwargs_send(self):
        """CONTRACT.md obliges only an adapter DECLARING threads to honour thread_id: a
        **kwargs send() under `threads: False` would swallow the placement and post
        top-level, reporting success (review F5). The declaration is consulted too."""
        class DeclaredNoThreads(NoThreadsAdapter):
            def send(self, target, text, key=None, **opts):
                self.delivered.append((target, text, key))
                return {"ts": f"receipt-{len(self.delivered)}", "key": key}
        adapter = DeclaredNoThreads()
        key = self.box(FakeAdapter()).stage(TARGET, TS, TEXT, thread_id=TS)["key"]
        box = self.box(adapter)
        with self.assertRaises(ReleaseError) as cm:
            box.release(key)
        self.assertIn("declares no thread support", str(cm.exception))
        self.assertEqual((box.get(key)["state"], adapter.delivered), (STAGED, []))
        with self.assertRaises(SendBlocked):
            box.stage(TARGET, "2.0", TEXT, thread_id="2.0")

    def test_a_replay_of_a_delivered_thread_reply_is_answered_from_its_row(self):
        """R2: the same reply sent twice returns the first receipt. The placement check
        runs only when a NEW row would be minted, so a connection since swapped to a
        thread-incapable adapter still answers the replay from the row (review F9)."""
        capable = self.box(FakeAdapter())
        key = capable.stage(TARGET, TS, TEXT, thread_id=TS)["key"]
        self.assertEqual(capable.release(key)["state"], COMMITTED)
        box = self.box()                                    # NoThreadsAdapter, same db
        replay = box.send(TARGET, TS, TEXT, thread_id=TS)
        self.assertEqual((replay["deduped"], replay["state"]), (True, COMMITTED))
        self.assertTrue(box.stage(TARGET, TS, TEXT, thread_id=TS)["deduped"])
        self.assertEqual(self.adapter.delivered, [])


class GuardedFake(FakeAdapter):
    """FakeAdapter plus the ENH-31 primitives: a declared size limit, a pre-send
    re-read (`newest`), a thread-aware read_back that records the kwarg it was given,
    and a declaration that direct sends are off."""
    MAX_TEXT_CHARS = 60

    def __init__(self, direct=False):
        super().__init__()
        self.latest = None            # what newest() answers
        self.newest_calls = []
        self.read_back_kwargs = []
        self._direct = direct

    def capabilities(self):
        return {"read": True, "history": True, "search": False, "send": True,
                "direct": self._direct, "react": False, "threads": True}

    def newest(self, channel_id, thread_id=None):
        self.newest_calls.append((channel_id, thread_id))
        return self.latest

    def read_back(self, target, key, thread_id=None):
        self.read_back_kwargs.append(thread_id)
        return super().read_back(target, key)


class SendGuardsTest(unittest.TestCase):
    """The six guards of the gated send (ENH-31 A), each refusing BEFORE the INTENT flip —
    at stage() and direct send() before any row exists, and at release() before the
    claim — so a refused draft stays STAGED and discardable, and nothing reaches the
    adapter. Planted failures: a missing marker, an oversize text, a credential-shaped
    payload, a message newer than the draft, a direct policy on an adapter that
    declares direct off."""

    MARKED = "[AGENT] the numbers you asked for are attached."

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "outbox.db"
        self.adapter = GuardedFake()

    def box(self, policy="staged", marker="[AGENT]", adapter=None):
        return Outbox(self.db, adapter or self.adapter, {TARGET: policy},
                      send_interval=0.0, marker=marker)

    # ---- guard 1: the marker prefix ---------------------------------------------
    def test_a_draft_without_the_marker_is_refused_at_stage_and_at_direct_send(self):
        with self.assertRaises(SendBlocked) as cm:
            self.box().stage(TARGET, TS, "no marker here")
        self.assertIn("[AGENT]", str(cm.exception))
        direct = GuardedFake(direct=True)
        with self.assertRaises(SendBlocked):
            self.box(policy="direct", adapter=direct).send(TARGET, TS, "no marker here")
        self.assertEqual((self.box().staged(), self.adapter.delivered, direct.delivered),
                         ([], [], []))
        key = self.box().stage(TARGET, TS, self.MARKED)["key"]
        self.assertEqual(self.box().release(key)["state"], COMMITTED)

    def test_a_marker_configured_after_staging_refuses_the_release_before_the_flip(self):
        key = self.box(marker=None).stage(TARGET, TS, "staged before the marker existed")["key"]
        marked = self.box()
        with self.assertRaises(ReleaseError) as cm:
            marked.release(key)
        self.assertIn("marker", str(cm.exception))
        self.assertEqual(marked.get(key)["state"], STAGED)
        self.assertEqual(self.adapter.delivered, [])
        self.assertEqual(marked.discard(key)["state"], DISCARDED)

    # ---- guard 4: the adapter's size limit -----------------------------------------
    def test_a_text_over_the_adapters_limit_is_refused_before_any_row(self):
        long = "[AGENT] " + "x" * 60                      # 68 > GuardedFake.MAX_TEXT_CHARS
        with self.assertRaises(SendBlocked) as cm:
            self.box().stage(TARGET, TS, long)
        self.assertIn("60-character limit", str(cm.exception))
        self.assertEqual(self.box().staged(), [])
        ok = "[AGENT] " + "x" * 52                       # exactly 60
        self.assertEqual(self.box().stage(TARGET, TS, ok)["state"], STAGED)

    # ---- guard 5: the BLOCK-tier credential scan -------------------------------------
    def test_a_credential_shaped_payload_is_refused_and_never_echoed(self):
        secret = "xo" + "xb-" + "1234567890-abcdefghij"
        with self.assertRaises(SendBlocked) as cm:
            self.box().stage(TARGET, TS, f"[AGENT] token: {secret}")
        self.assertIn("slack-token", str(cm.exception))
        self.assertNotIn(secret, str(cm.exception), "the refusal echoed the secret")
        direct = GuardedFake(direct=True)
        with self.assertRaises(SendBlocked):
            self.box(policy="direct", adapter=direct).send(TARGET, TS, f"[AGENT] token: {secret}")
        self.assertEqual((self.adapter.delivered, direct.delivered), ([], []))

    def test_a_legacy_draft_carrying_a_credential_is_refused_at_the_click(self):
        """A row older than the scan (written by earlier code) is caught at release —
        the guard runs on the text before the flip, not only at staging."""
        secret = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
        box = self.box()
        key = box.stage(TARGET, TS, self.MARKED)["key"]
        box.conn.execute("UPDATE outbox SET text=? WHERE key=?",
                         (f"[AGENT] key {secret}", key))
        box.conn.commit()
        with self.assertRaises(ReleaseError) as cm:
            box.release(key)
        self.assertIn("aws-access-key", str(cm.exception))
        self.assertEqual((box.get(key)["state"], self.adapter.delivered), (STAGED, []))

    # ---- guard 2: the pre-send re-read -----------------------------------------------
    def test_a_message_newer_than_the_draft_refuses_the_click_with_who_and_when(self):
        box = self.box()
        key = box.stage(TARGET, TS, self.MARKED)["key"]
        self.adapter.latest = {"ts": f"{time.time() + 5:.6f}", "sender_id": "U_HUMAN",
                               "text": "actually never mind"}
        with self.assertRaises(ReleaseError) as cm:
            box.release(key)
        msg = str(cm.exception)
        self.assertIn("re-read first", msg)
        self.assertIn("U_HUMAN", msg)
        self.assertNotIn("never mind", msg, "a flash renders markdown — never the text")
        self.assertEqual((box.get(key)["state"], self.adapter.delivered), (STAGED, []))
        self.assertEqual(self.adapter.newest_calls, [(TARGET, None)])

    def test_an_older_newest_message_lets_the_click_through(self):
        box = self.box()
        key = box.stage(TARGET, TS, self.MARKED)["key"]
        self.adapter.latest = {"ts": "1700000000.000100", "sender_id": "U_ASKER"}
        self.assertEqual(box.release(key)["state"], COMMITTED)

    def test_the_re_read_looks_in_the_drafts_thread(self):
        box = self.box()
        key = box.stage(TARGET, TS, self.MARKED, thread_id=TS)["key"]
        box.release(key)
        self.assertEqual(self.adapter.newest_calls, [(TARGET, TS)])

    # ---- the adapter's declaration: direct sends off ----------------------------------
    def test_a_direct_policy_on_an_adapter_declaring_direct_off_is_refused_at_construction(self):
        for policies in ({TARGET: "direct"}, {TARGET: {"channel": "staged", "thread": "direct"}}):
            with self.assertRaises(PolicyError, msg=repr(policies)) as cm:
                Outbox(self.db, GuardedFake(direct=False), policies, send_interval=0.0)
            self.assertIn("direct", str(cm.exception))
        Outbox(self.db, GuardedFake(direct=False), {TARGET: "staged"}, send_interval=0.0)
        Outbox(self.db, GuardedFake(direct=True), {TARGET: "direct"}, send_interval=0.0)
        Outbox(self.db, FakeAdapter(), {TARGET: "direct"}, send_interval=0.0)   # absent = allowed

    # ---- guard 3's plumbing: the thread travels to read_back ----------------------------
    def test_read_back_is_given_the_thread_when_it_takes_one(self):
        box = self.box()
        key = box.stage(TARGET, TS, self.MARKED, thread_id=TS)["key"]
        box.release(key)
        self.assertEqual(self.adapter.read_back_kwargs, [TS])
        plain = FakeAdapter()                                # read_back(target, key) only
        box2 = Outbox(Path(self.tmp.name) / "o2.db", plain, {TARGET: "staged"},
                      send_interval=0.0)
        self.assertEqual(box2.release(box2.stage(TARGET, TS, TEXT, thread_id=TS)["key"])["state"],
                         COMMITTED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
