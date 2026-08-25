"""Extensibility tests (gate G5; requirement R11).

The incumbent grew its second channel type as a hand-mirrored COPY of the first — watchdogs,
send scripts and bridges duplicated per channel (R11's measured evidence). The engine's
counter-property: a channel type is a DIRECTORY under the configured `channels_dir`
containing `adapter.py`, discovered at config load. Core never enumerates platform names.

The acceptance criterion — "the third adapter lands with an empty git diff under core/" —
is made mechanical here: a first, second and THIRD adapter type are authored INSIDE these
tests with invented names, so core cannot have been edited for them. If anyone reintroduces
a hardcoded adapter whitelist in core/ (the property removed), every landing below is
refused and this module goes red.
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import (  # noqa: E402
    ConfigError, discover_adapters, ensure_dirs, from_dict, load_adapter_class)
from core.outbox import Outbox  # noqa: E402

# Contract-shaped in-memory adapter (channels/CONTRACT.md). Authored by the test, not
# shipped — the whole point is that core has never seen this code or these type names.
MINIMAL_ADAPTER = '''\
class Adapter:
    def __init__(self, auth=None):
        self.auth = auth or {}
        self.delivered = []

    def capabilities(self):
        return {"read": True, "history": False, "search": False,
                "send": True, "react": False, "threads": False}

    def poll(self, cursor):
        return [], cursor

    def resolve(self, ref):
        return ref

    def send(self, channel_id, text, key=None):
        self.delivered.append((channel_id, text, key))
        return {"ts": str(len(self.delivered)), "key": key}

    def read_back(self, target, key):
        return any(t == target and k == key for t, _, k in self.delivered)

    def health(self):
        return {"reachable": True, "auth_ok": True, "detail": "in-memory"}
'''


def land_adapter(channels_dir: Path, name: str, body: str = MINIMAL_ADAPTER) -> None:
    """A 'landing' is exactly what an adapter author does: mkdir + adapter.py. Nothing else."""
    d = Path(channels_dir) / name
    d.mkdir(parents=True)
    (d / "adapter.py").write_text(body)


class ThirdAdapterZeroCoreChangeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.channels = self.base / "channels"

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, adapter: str):
        return from_dict({
            "engine": {"state_dir": "state"},
            "instances": [{
                "name": "t", "adapter": adapter,
                "channels": [{"id": "C_T", "reply_policy": "direct"}],
            }],
        }, base_dir=self.base)

    def test_first_second_and_third_adapter_types_land_by_directory_alone(self):
        """R11. Each landing is a dir-drop + a config edit; no other file changes."""
        for nth, name in enumerate(("aviary", "bakelite", "carrierpigeon"), 1):
            land_adapter(self.channels, name)
            cfg = self.cfg(name)
            cls = load_adapter_class(cfg.channels_dir, name)
            self.assertTrue(cls().health()["reachable"],
                            f"adapter #{nth} ({name!r}) landed but is not usable")

    def test_the_third_adapter_carries_a_real_send_through_the_outbox(self):
        """Parsing is not proof — the never-before-seen type must ride the full G2 ladder."""
        for name in ("aviary", "bakelite", "carrierpigeon"):
            land_adapter(self.channels, name)
        cfg = self.cfg("carrierpigeon")
        ensure_dirs(cfg)
        adapter = load_adapter_class(cfg.channels_dir, "carrierpigeon")()
        outbox = Outbox(cfg.outbox_path, adapter, cfg.instance("t").policies())
        r = outbox.send("C_T", "1.1", "sent by the third adapter, core untouched")
        self.assertEqual(r["state"], "COMMITTED")
        self.assertTrue(adapter.read_back("C_T", r["key"]))
        outbox.close()

    def test_a_misspelled_adapter_is_refused_and_told_what_was_discovered(self):
        """Discovery must not cost the loud-refusal property (the inert-instance class)."""
        land_adapter(self.channels, "aviary")
        with self.assertRaises(ConfigError) as ctx:
            self.cfg("avairy")
        self.assertIn("avairy", str(ctx.exception))
        # The discovered list is the adopter's map back to the right spelling.
        self.assertIn("aviary", str(ctx.exception))

    def test_a_directory_without_the_entry_point_is_not_a_channel_type(self):
        """channels/slack today is README-only; offering it as loadable would recreate the
        silently-inert-instance defect the loud refusal exists to prevent."""
        d = self.channels / "docsonly"
        d.mkdir(parents=True)
        (d / "README.md").write_text("design notes, no adapter.py")
        self.assertNotIn("docsonly", discover_adapters(self.channels))
        with self.assertRaises(ConfigError):
            self.cfg("docsonly")

    def test_an_adapter_without_the_entry_class_fails_at_load_not_at_first_send(self):
        land_adapter(self.channels, "hollow", body="x = 1\n")
        with self.assertRaises(ConfigError) as ctx:
            load_adapter_class(self.channels, "hollow")
        self.assertIn("Adapter", str(ctx.exception))

    def test_a_missing_channels_dir_discovers_nothing_and_refuses_loudly(self):
        with self.assertRaises(ConfigError):
            self.cfg("anything")


class ShippedFakeAdapterTest(unittest.TestCase):
    """The one adapter the repo ships must itself obey the discovery convention and the
    contract — it is the reference an adapter author copies, and the dry-run adapter
    QUICKSTART points adopters at."""

    def setUp(self):
        self.adapter = load_adapter_class(ROOT / "channels", "fake")()

    def test_fake_is_discovered_from_the_shipped_tree(self):
        self.assertIn("fake", discover_adapters(ROOT / "channels"))

    def test_capabilities_answer_every_contract_key(self):
        caps = self.adapter.capabilities()
        for k in ("read", "history", "search", "send", "react", "threads"):
            self.assertIn(k, caps)

    def test_poll_is_idempotent_from_the_same_cursor(self):
        """CONTRACT.md: re-polling with the same cursor may duplicate, never lose."""
        msg = {"channel_type": "fake", "channel_id": "C_F", "sender_id": "U_F",
               "ts": "1.0", "text": "hello", "thread_id": None, "raw": {}}
        self.adapter.seed([msg])
        first, cur = self.adapter.poll(None)
        again, _ = self.adapter.poll(None)
        self.assertEqual(first, again)
        after, _ = self.adapter.poll(cur)
        self.assertEqual(after, [])

    def test_send_and_read_back_agree(self):
        self.adapter.send("C_F", "hi", key="k1")
        self.assertTrue(self.adapter.read_back("C_F", "k1"))
        self.assertFalse(self.adapter.read_back("C_F", "k2"))

    def test_health_can_actually_fail(self):
        """Contract rule 5 — a health check that can only pass is a defect."""
        self.assertTrue(self.adapter.health()["reachable"])
        self.adapter.fail_health = True
        self.assertFalse(self.adapter.health()["reachable"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
