"""Tests for ENH-7 — multi-tenant isolation between instances sharing ONE config.

Two instances in one settings file are expressible (settings.example.json ships two),
and channel ids are only unique WITHIN a platform workspace — two Slack workspaces can
both contain a channel with the byte-identical platform id. So every per-instance
boundary is load-bearing, and each one here has a mutation in tests/mutation_check.sh
that deletes it and must turn this module red:

* cursors are namespaced by instance name — one tenant's watermark advancing must
  never move (or blind) another tenant polling the same channel id
* outbox state is per instance — recovery re-sends a row through the adapter (and so
  the credentials) that WROTE it, never through whichever instance recovers first,
  and one tenant's delivery must not dedupe away another tenant's identical reply
* credentials resolve from each instance's own auth block and nowhere else
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import ConfigError, ensure_dirs, from_dict, load_adapter_class  # noqa: E402
from core.escalate import Escalator  # noqa: E402
from core.journal import Journal  # noqa: E402
from core.outbox import COMMITTED, Outbox, _Crash  # noqa: E402
from core.owed import OwedRegistry  # noqa: E402
from core.schedule import Scheduler, Source  # noqa: E402
from core.store import Store  # noqa: E402


def seed_fake_adapter(base):
    """Adapter types are DISCOVERED on disk (R11); the stub records its auth because
    what an adapter was HANDED is exactly what the credential-bleed tests measure."""
    d = Path(base) / "channels" / "fake"
    d.mkdir(parents=True, exist_ok=True)
    (d / "adapter.py").write_text(
        "class Adapter:\n"
        "    def __init__(self, auth=None):\n"
        "        self.auth = auth or {}\n")


def two_tenant_config(**engine_over):
    """Two tenants, one config, both watching the SAME channel id — the collision case
    isolation exists for. Tenant A carries an extra auth key on purpose: a bleed that
    merges auth blocks shows up as that key appearing on tenant B."""
    return {
        "engine": {"state_dir": "state", **engine_over},
        "instances": [
            {"name": "tenant-a", "adapter": "fake",
             "auth": {"token": "env:TENANT_A_TOKEN",
                      "signing_secret": "env:TENANT_A_SIGNING"},
             "channels": [{"id": "C_SHARED", "reply_policy": "direct"}]},
            {"name": "tenant-b", "adapter": "fake",
             "auth": {"token": "env:TENANT_B_TOKEN"},
             "channels": [{"id": "C_SHARED", "reply_policy": "direct"}]},
        ],
    }


TWO_TENANT_ENV = {"TENANT_A_TOKEN": "sekret-a", "TENANT_A_SIGNING": "sekret-a-signing",
                  "TENANT_B_TOKEN": "sekret-b"}


class SendingAdapter:
    """Contract-shaped send/read_back pair; `delivered` is the audit trail the
    isolation assertions read."""

    def __init__(self):
        self.delivered = []

    def send(self, target, text, key=None):
        self.delivered.append((target, text, key))
        return {"ts": f"r{len(self.delivered)}", "key": key}

    def read_back(self, target, key):
        return any(t == target and k == key for t, _, k in self.delivered)


class RecordingPollAdapter:
    """Fake-adapter-shaped poller that RECORDS the cursor each poll was given — the
    assertion surface for 'each instance resumes from its own watermark'."""

    def __init__(self, messages):
        self.messages = list(messages)
        self.polled_with = []

    def poll(self, cursor):
        self.polled_with.append(cursor)
        cursor_ts = float(cursor) if cursor is not None else float("-inf")
        fresh = [m for m in self.messages if float(m["ts"]) > cursor_ts]
        if not fresh:
            return [], cursor
        return fresh, str(max(float(m["ts"]) for m in fresh))


def msg(ts, text):
    return {"channel_type": "fake", "channel_id": "C_SHARED", "sender_id": "U1",
            "ts": ts, "text": text}


class TwoTenantBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        seed_fake_adapter(self.base)
        self.cfg = from_dict(two_tenant_config(), base_dir=self.base,
                             env=dict(TWO_TENANT_ENV))
        ensure_dirs(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()


class CursorIsolationTest(TwoTenantBase):
    def test_one_tenants_cursor_write_never_moves_the_others(self):
        store = Store(self.cfg.store_path)
        try:
            store.cursor_set("tenant-a", "C_SHARED", "100.1")
            self.assertIsNone(store.cursor_get("tenant-b", "C_SHARED"),
                              "tenant-b saw a watermark it never wrote")
            store.cursor_set("tenant-b", "C_SHARED", "200.2")
            self.assertEqual(store.cursor_get("tenant-a", "C_SHARED"), "100.1",
                             "tenant-b's cursor write moved tenant-a's watermark")
            self.assertEqual(store.cursor_get("tenant-b", "C_SHARED"), "200.2")
        finally:
            store.close()

    def test_the_scheduler_polls_each_instance_from_its_own_watermark(self):
        """The loop must commit AND read the cursor under the instance's own name.
        Either half collapsing to a shared key blinds one tenant or re-polls the other
        forever — same channel id, different workspaces, different histories."""
        store = Store(self.cfg.store_path)
        journal = Journal(self.base / "state" / "journal.db")
        owed = OwedRegistry(self.base / "state" / "owed.db")
        escalator = Escalator(self.base / "state" / "escalate.db",
                              notify=lambda m: None)
        a = RecordingPollAdapter([msg("1.5", "alpha status update")])
        b = RecordingPollAdapter([msg("2.5", "beta status update")])
        now = [1000.0]
        sched = Scheduler(
            store=store, journal=journal, owed=owed, escalator=escalator,
            sources=[Source(name="tenant-a", adapter=a, channels=("C_SHARED",)),
                     Source(name="tenant-b", adapter=b, channels=("C_SHARED",))],
            base_interval=1.0, clock=lambda: now[0], sleep=lambda s: None)
        try:
            sched.cycle()
            self.assertEqual(store.cursor_get("tenant-a", "C_SHARED"), "1.5",
                             "tenant-a's cursor was not committed under its own name")
            self.assertEqual(store.cursor_get("tenant-b", "C_SHARED"), "2.5",
                             "tenant-b's cursor was not committed under its own name")
            sched.cycle()
            self.assertEqual(a.polled_with[-1], "1.5",
                             "tenant-a was not polled from its OWN watermark")
            self.assertEqual(b.polled_with[-1], "2.5",
                             "tenant-b was not polled from its OWN watermark")
        finally:
            store.close(); journal.close(); owed.close_db(); escalator.close_db()


class OutboxIsolationTest(TwoTenantBase):
    def test_each_instance_gets_its_own_outbox_path(self):
        pa = self.cfg.outbox_path_for("tenant-a")
        pb = self.cfg.outbox_path_for("tenant-b")
        self.assertNotEqual(pa, pb, "two tenants were handed ONE outbox file")
        for p in (pa, pb):
            self.assertEqual(p.parent, self.cfg.outbox_path.parent,
                             "a per-instance outbox escaped the configured directory")
            self.assertEqual(p.suffix, self.cfg.outbox_path.suffix)

    def test_an_unknown_instance_name_is_refused_not_given_a_path(self):
        """A typo'd name silently minting a fresh empty outbox would hide every
        pending draft from view — the silently-inert class, so it must refuse."""
        with self.assertRaises(ConfigError):
            self.cfg.outbox_path_for("tenant-typo")

    def test_the_same_reply_identity_delivers_once_per_tenant(self):
        """The idempotency key has no instance component on purpose (it is the
        platform-visible identity), so tenant separation must come from the outbox
        FILE — sharing one file would let tenant A's delivery dedupe tenant B's."""
        a_adapter, b_adapter = SendingAdapter(), SendingAdapter()
        a = Outbox(self.cfg.outbox_path_for("tenant-a"), a_adapter,
                   self.cfg.instance("tenant-a").policies())
        b = Outbox(self.cfg.outbox_path_for("tenant-b"), b_adapter,
                   self.cfg.instance("tenant-b").policies())
        try:
            ra = a.send("C_SHARED", "5.5", "[AGENT] same reply text")
            rb = b.send("C_SHARED", "5.5", "[AGENT] same reply text")
            self.assertEqual((ra["state"], rb["state"]), (COMMITTED, COMMITTED))
            self.assertFalse(rb.get("deduped"),
                             "tenant-a's delivery deduped tenant-b's send away")
            self.assertEqual(len(a_adapter.delivered), 1)
            self.assertEqual(len(b_adapter.delivered), 1,
                             "tenant-b's reply never reached tenant-b's workspace")
        finally:
            a.close(); b.close()

    def test_recovery_never_replays_another_tenants_intent(self):
        """The bleed this boundary exists for: an INTENT row re-sent by whichever
        instance recovers first goes out through the WRONG adapter — tenant B's
        message posted with tenant A's credentials, in tenant A's workspace."""
        a_adapter, b_adapter = SendingAdapter(), SendingAdapter()
        a = Outbox(self.cfg.outbox_path_for("tenant-a"), a_adapter,
                   self.cfg.instance("tenant-a").policies())
        b = Outbox(self.cfg.outbox_path_for("tenant-b"), b_adapter,
                   self.cfg.instance("tenant-b").policies())
        try:
            with self.assertRaises(_Crash):
                b.send("C_SHARED", "6.6", "[AGENT] tenant-b in-flight reply",
                       _crash_at="after_intent")
            counts = a.recover()
            self.assertEqual(counts["resumed"], 0,
                             "tenant-a's recovery picked up tenant-b's INTENT row")
            self.assertEqual(a_adapter.delivered, [],
                             "tenant-b's message went out through tenant-a's adapter")
            counts = b.recover()
            self.assertEqual(counts["resent"], 1,
                             "the crashed send was lost instead of recovered by its owner")
            self.assertEqual(len(b_adapter.delivered), 1)
        finally:
            a.close(); b.close()

    def test_staged_drafts_are_visible_only_to_their_own_tenant(self):
        cfg = from_dict(two_tenant_config(), base_dir=self.base,
                        env=dict(TWO_TENANT_ENV))
        # Same channel id, different policy per tenant — policy is an instance
        # property, so one tenant staging must not put drafts in the other's queue.
        b_policies = dict(cfg.instance("tenant-b").policies(), C_SHARED="staged")
        a = Outbox(cfg.outbox_path_for("tenant-a"), SendingAdapter(),
                   cfg.instance("tenant-a").policies())
        b = Outbox(cfg.outbox_path_for("tenant-b"), SendingAdapter(), b_policies)
        try:
            r = b.send("C_SHARED", "7.7", "[AGENT] draft for the operator gate")
            self.assertEqual(r["state"], "STAGED")
            self.assertEqual(a.staged(), [],
                             "tenant-b's draft surfaced in tenant-a's staged queue")
            self.assertEqual(len(b.staged()), 1)
        finally:
            a.close(); b.close()


class InstanceNameBoundaryTest(unittest.TestCase):
    """The instance name IS the isolation key — it namespaces cursors and state
    filenames — so a name collision or a path-shaped name breaks every boundary
    above at once. Both must refuse at load (never at first write)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        seed_fake_adapter(self.base)

    def tearDown(self):
        self.tmp.cleanup()

    def test_duplicate_instance_names_are_refused(self):
        d = two_tenant_config()
        d["instances"][1]["name"] = "tenant-a"
        with self.assertRaises(ConfigError) as ctx:
            from_dict(d, base_dir=self.base, env=dict(TWO_TENANT_ENV))
        self.assertIn("tenant-a", str(ctx.exception))

    def test_a_path_shaped_instance_name_is_refused(self):
        for bad in ("tenants/../../etc", "a/b", "..", "a b"):
            d = two_tenant_config()
            d["instances"][1]["name"] = bad
            with self.assertRaises(ConfigError, msg=f"name {bad!r} was accepted"):
                from_dict(d, base_dir=self.base, env=dict(TWO_TENANT_ENV))


class CredentialIsolationTest(TwoTenantBase):
    def test_each_instance_resolves_exactly_its_own_auth_block(self):
        self.assertEqual(self.cfg.instance("tenant-a").auth,
                         {"token": "sekret-a", "signing_secret": "sekret-a-signing"})
        self.assertEqual(self.cfg.instance("tenant-b").auth,
                         {"token": "sekret-b"},
                         "tenant-b's auth is not exactly what tenant-b declared")

    def test_no_tenant_holds_another_tenants_secret_value(self):
        a = self.cfg.instance("tenant-a").auth
        b = self.cfg.instance("tenant-b").auth
        for value in a.values():
            self.assertNotIn(value, b.values(),
                             "a tenant-a secret value bled into tenant-b's auth")
        self.assertNotIn("sekret-b", a.values(),
                         "tenant-b's secret value bled into tenant-a's auth")

    def test_adapters_wired_per_instance_hold_only_their_own_secrets(self):
        """The exact wiring scripts/scheduler.py ships: one adapter per instance,
        constructed with THAT instance's auth."""
        adapters = {inst.name: load_adapter_class(self.cfg.channels_dir,
                                                  inst.adapter)(auth=inst.auth)
                    for inst in self.cfg.instances}
        self.assertEqual(adapters["tenant-b"].auth, {"token": "sekret-b"},
                         "tenant-b's adapter was handed more than tenant-b's auth")
        self.assertNotIn("sekret-a", adapters["tenant-b"].auth.values())
        self.assertNotIn("sekret-b", adapters["tenant-a"].auth.values())


if __name__ == "__main__":
    unittest.main()
