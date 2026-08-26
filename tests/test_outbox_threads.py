"""Thread awareness (ENH-3): thread_id was declared everywhere and read nowhere.

Replying in-thread vs in-channel is a visible behavioural difference, and "answer in
thread, never the main channel" is a legitimate policy an adopter must be able to express
(a support channel where top-level posts page the whole room, but thread replies do not).
Before this, `thread_id` existed in the store schema, the normalized message, and the
adapter contract — and no code path ever read it, so every reply flattened to top-level.

Properties pinned here:

* the outbox RECORDS which scope was used (thread vs channel) plus the thread itself,
  because staged drafts and crash recovery both need the placement to survive a restart;
* policy is resolvable PER SCOPE, with the same default-DENY discipline per scope that
  targets get as a whole;
* placement is part of a reply's identity: the same text for the same trigger in-thread
  and in-channel are two different messages, not one deduped one;
* recovery re-sends into the recorded thread — a crash must not flatten a thread reply
  into the main channel, which would violate the exact policy above;
* a v1 outbox.db (no thread columns) still loads, and its old rows read as channel scope.
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config  # noqa: E402
from core.outbox import (COMMITTED, Outbox, PolicyError, STAGED,  # noqa: E402
                         SendBlocked, _Crash, idempotency_key)

ROOT = Path(__file__).resolve().parent.parent
TARGET = "C_SUPPORT"
TS = "1700000000.000100"
THREAD = "1699999999.000500"
TEXT = "[AGENT] answered where the policy says."


class ThreadAwareAdapter:
    """A channel that honours placement. `delivered` records WHERE each send landed."""

    def __init__(self):
        self.delivered = []      # (target, text, key, thread_id)
        self.send_calls = 0

    def send(self, target, text, key=None, thread_id=None):
        self.send_calls += 1
        self.delivered.append((target, text, key, thread_id))
        return {"ts": f"receipt-{self.send_calls}", "key": key}

    def read_back(self, target, key):
        return any(t == target and k == key for t, _, k, _ in self.delivered)


class V1Adapter:
    """An adapter written before thread awareness — send() has NO thread_id parameter.

    Channel-scope sends must keep working against it unchanged; only a thread send may
    require the new parameter.
    """

    def __init__(self):
        self.delivered = []      # (target, text, key)

    def send(self, target, text, key=None):
        self.delivered.append((target, text, key))
        return {"ts": "receipt", "key": key}

    def read_back(self, target, key):
        return any(t == target and k == key for t, _, k in self.delivered)


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "outbox.db"
        self.adapter = ThreadAwareAdapter()

    def tearDown(self):
        self.tmp.cleanup()

    def box(self, policies=None, adapter=None):
        return Outbox(self.db, adapter or self.adapter,
                      policies if policies is not None else {TARGET: "direct"})


class ThreadSendTest(Harness):
    def test_a_thread_send_reaches_the_adapter_with_the_thread_id(self):
        r = self.box().send(TARGET, TS, TEXT, thread_id=THREAD)
        self.assertEqual(r["state"], COMMITTED)
        self.assertEqual(self.adapter.delivered[0][3], THREAD,
                         "the reply did not land in the thread it was scoped to")

    def test_the_outbox_records_thread_scope_and_the_thread_itself(self):
        b = self.box()
        r = b.send(TARGET, TS, TEXT, thread_id=THREAD)
        row = b.get(r["key"])
        self.assertEqual(row["scope"], "thread")
        self.assertEqual(row["thread_id"], THREAD)

    def test_a_channel_send_records_channel_scope_and_no_thread(self):
        b = self.box()
        r = b.send(TARGET, TS, TEXT)
        row = b.get(r["key"])
        self.assertEqual(row["scope"], "channel")
        self.assertIsNone(row["thread_id"])

    def test_a_channel_send_keeps_the_v1_adapter_call_shape(self):
        """An adapter written before thread awareness must not need changing to keep
        doing what it always did — top-level sends."""
        v1 = V1Adapter()
        r = self.box(adapter=v1).send(TARGET, TS, TEXT)
        self.assertEqual(r["state"], COMMITTED)
        self.assertEqual(v1.delivered, [(TARGET, TEXT, r["key"])])

    def test_a_thread_send_through_a_threadless_adapter_refuses_loudly(self):
        """Never flatten. Posting top-level 'because the adapter cannot thread' is the
        one outcome a thread-scoped policy exists to forbid, and it is public."""
        v1 = V1Adapter()
        with self.assertRaises(SendBlocked):
            self.box(adapter=v1).send(TARGET, TS, TEXT, thread_id=THREAD)
        self.assertEqual(v1.delivered, [],
                         "a threadless adapter was handed a thread reply and posted it "
                         "in the main channel")

    def test_an_adapters_own_TypeError_is_not_reported_as_a_placement_failure(self):
        """Diagnosis has to stay honest: a bug INSIDE a thread-capable adapter must not
        be relabelled 'this adapter cannot thread', which sends the operator to rewrite
        a capability that was never missing."""
        class Buggy(ThreadAwareAdapter):
            def send(self, target, text, key=None, thread_id=None):
                raise TypeError("bad argument deep inside the platform client")

        with self.assertRaises(TypeError):
            self.box(adapter=Buggy()).send(TARGET, TS, TEXT, thread_id=THREAD)

    def test_an_adapter_taking_kwargs_is_treated_as_thread_capable(self):
        """**kwargs is a legitimate contract-conforming signature; refusing it would
        reject a working adapter."""
        class Kwargs:
            def __init__(self):
                self.delivered = []

            def send(self, target, text, **kw):
                self.delivered.append((target, text, kw.get("key"), kw.get("thread_id")))
                return {"ts": "1", "key": kw.get("key")}

            def read_back(self, target, key):
                return any(k == key for _, _, k, _ in self.delivered)

        ad = Kwargs()
        r = self.box(adapter=ad).send(TARGET, TS, TEXT, thread_id=THREAD)
        self.assertEqual(r["state"], COMMITTED)
        self.assertEqual(ad.delivered[0][3], THREAD)


class ScopedPolicyTest(Harness):
    def test_answer_in_thread_never_in_the_main_channel(self):
        """THE acceptance policy: an adopter can allow thread replies while the main
        channel stays read-only."""
        b = self.box(policies={TARGET: {"thread": "direct", "channel": "never"}})
        r = b.send(TARGET, TS, TEXT, thread_id=THREAD)
        self.assertEqual(r["state"], COMMITTED)
        with self.assertRaises(PolicyError):
            b.send(TARGET, TS, TEXT)
        self.assertEqual(self.adapter.send_calls, 1,
                         "a channel-scope send reached the adapter despite "
                         "channel policy 'never'")

    def test_a_scope_absent_from_a_scoped_policy_is_denied(self):
        """Default DENY applies PER SCOPE, same as it does per target."""
        b = self.box(policies={TARGET: {"thread": "direct"}})
        with self.assertRaises(PolicyError):
            b.send(TARGET, TS, TEXT)
        self.assertEqual(self.adapter.send_calls, 0)

    def test_a_plain_string_policy_applies_to_both_scopes(self):
        """Every pre-thread-awareness config keeps meaning what it meant."""
        b = self.box(policies={TARGET: "direct"})
        self.assertEqual(b.send(TARGET, TS, TEXT)["state"], COMMITTED)
        self.assertEqual(b.send(TARGET, TS, "in thread", thread_id=THREAD)["state"],
                         COMMITTED)

    def test_a_staged_thread_draft_records_its_placement_for_the_operator_gate(self):
        """The operator gating a draft must know WHERE it would post — a draft written
        for a thread that gets posted top-level is the exact visible difference this
        item exists for."""
        b = self.box(policies={TARGET: {"thread": "staged", "channel": "never"}})
        r = b.send(TARGET, TS, TEXT, thread_id=THREAD)
        self.assertEqual(r["state"], STAGED)
        self.assertEqual(self.adapter.send_calls, 0)
        drafts = b.staged()
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["scope"], "thread")
        self.assertEqual(drafts[0]["thread_id"], THREAD)


class IdentityTest(Harness):
    def test_thread_and_channel_replies_are_distinct_deliveries(self):
        """Placement is part of a reply's identity: same trigger, same text, different
        placement is two messages — dedupe must not collapse them."""
        b = self.box()
        r_thread = b.send(TARGET, TS, TEXT, thread_id=THREAD)
        r_channel = b.send(TARGET, TS, TEXT)
        self.assertNotEqual(r_thread["key"], r_channel["key"])
        self.assertEqual(self.adapter.send_calls, 2)

    def test_channel_scope_keys_are_byte_identical_to_v1_keys(self):
        """Rows written before thread awareness must still match on recovery, so a
        thread-less key must hash exactly as it always did."""
        self.assertEqual(idempotency_key(TARGET, TS, TEXT),
                         idempotency_key(TARGET, TS, TEXT, thread_id=None))

    def test_a_repeated_thread_send_dedupes(self):
        b = self.box()
        first = b.send(TARGET, TS, TEXT, thread_id=THREAD)
        second = b.send(TARGET, TS, TEXT, thread_id=THREAD)
        self.assertTrue(second["deduped"])
        self.assertEqual(first["key"], second["key"])
        self.assertEqual(self.adapter.send_calls, 1)


class RecoveryTest(Harness):
    def test_recovery_resends_into_the_recorded_thread(self):
        """A crash between INTENT and the adapter must not flatten a thread reply into
        the main channel — that would violate the thread-only policy after the fact."""
        with self.assertRaises(_Crash):
            self.box().send(TARGET, TS, TEXT, thread_id=THREAD,
                            _crash_at="after_intent")
        counts = self.box().recover()
        self.assertEqual(counts["resent"], 1)
        self.assertEqual(self.adapter.delivered[0][3], THREAD,
                         "recovery flattened a thread reply into the channel")


# The outbox schema as it stood before thread awareness — what any adopter who ran the
# quickstart before this change has on disk.
V1_SCHEMA = """
CREATE TABLE outbox (
    key         TEXT PRIMARY KEY,
    target      TEXT NOT NULL,
    trigger_ts  TEXT NOT NULL,
    text        TEXT NOT NULL,
    state       TEXT NOT NULL,
    receipt     TEXT,
    policy      TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
"""


class V1DatabaseTest(Harness):
    def test_a_v1_outbox_db_still_loads_and_its_rows_read_as_channel_scope(self):
        conn = sqlite3.connect(str(self.db))
        conn.executescript(V1_SCHEMA)
        old_key = idempotency_key(TARGET, "1690000000.0", "sent before threads existed")
        conn.execute(
            "INSERT INTO outbox (key, target, trigger_ts, text, state, policy, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,0,0)",
            (old_key, TARGET, "1690000000.0", "sent before threads existed",
             COMMITTED, "direct"))
        conn.commit()
        conn.close()

        b = self.box()
        old = b.get(old_key)
        # v1 could not express a thread, so every pre-migration row IS channel scope.
        self.assertEqual(old["scope"], "channel")
        self.assertIsNone(old["thread_id"])
        r = b.send(TARGET, TS, TEXT, thread_id=THREAD)
        self.assertEqual(b.get(r["key"])["scope"], "thread")


class ConfigScopingTest(unittest.TestCase):
    """thread_reply_policy in settings — the config surface of the same property."""

    def load(self, channel):
        raw = {"engine": {}, "instances": [
            {"name": "t", "adapter": "fake", "channels": [channel]}]}
        return config.from_dict(raw, base_dir=ROOT)

    def test_thread_reply_policy_scopes_the_policy_map(self):
        cfg = self.load({"id": TARGET, "reply_policy": "never",
                         "thread_reply_policy": "staged"})
        self.assertEqual(cfg.instance("t").policies()[TARGET],
                         {"channel": "never", "thread": "staged"})

    def test_absent_thread_reply_policy_keeps_the_plain_string_shape(self):
        """Configs written before thread awareness produce the identical policy map."""
        cfg = self.load({"id": TARGET, "reply_policy": "staged"})
        self.assertEqual(cfg.instance("t").policies()[TARGET], "staged")

    def test_an_invalid_thread_reply_policy_fails_at_load_naming_the_channel(self):
        with self.assertRaises(config.ConfigError) as ctx:
            self.load({"id": TARGET, "reply_policy": "never",
                       "thread_reply_policy": "yolo"})
        self.assertIn(TARGET, str(ctx.exception))
        self.assertIn("thread_reply_policy", str(ctx.exception))


class ReferenceAdapterTest(unittest.TestCase):
    """channels/fake is the copy-me implementation, so it must model the contract's
    send(channel_id, text, thread_id?) — including recording placement in its audit
    trail, which is what read-back-style verification of placement would build on."""

    def setUp(self):
        self.adapter = config.load_adapter_class(ROOT / "channels", "fake")()

    def test_the_reference_adapter_records_thread_placement(self):
        self.adapter.send("C_F", "in thread", key="k1", thread_id="123.4")
        self.adapter.send("C_F", "top level", key="k2")
        self.assertEqual(self.adapter.delivered[0][3], "123.4")
        self.assertIsNone(self.adapter.delivered[1][3])
        self.assertTrue(self.adapter.read_back("C_F", "k1"))

    def test_the_reference_adapter_declares_the_threads_capability(self):
        self.assertTrue(self.adapter.capabilities()["threads"],
                        "the adapter honours thread placement but does not say so — "
                        "the engine would degrade it to channel-only for no reason")


if __name__ == "__main__":
    unittest.main(verbosity=2)
