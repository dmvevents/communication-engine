"""core/config: the ENH-31 keys — `kind` (DM or channel), `answerers`, `send_marker`.

Each is validated at load, by name, because an inert or mistyped key here changes who
can close an ask or where a reply lands (ENH-17's inert-key rule). The loader's other
properties are pinned where their features live (tests/test_isolation.py,
tests/test_extensibility.py, tests/test_portability.py).
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import ChannelConfig, ConfigError, from_dict  # noqa: E402


def cfg(instance_extra=None, channel_extra=None, channels=None):
    base = tempfile.mkdtemp()
    chans = channels if channels is not None else [dict({"id": "C_CUST"}, **(channel_extra or {}))]
    raw = {"engine": {"state_dir": "state", "channels_dir": str(ROOT / "channels")},
           "instances": [dict({"name": "team", "adapter": "fake", "channels": chans},
                              **(instance_extra or {}))]}
    return from_dict(raw, base_dir=base, env={})


class KindTest(unittest.TestCase):
    def test_kind_is_inferred_from_a_slack_shaped_id_when_absent(self):
        real_shape = "D" + "7" * 10          # built at runtime: the gate refuses literal ids
        mixed = "D0" + "A" * 9
        c = cfg(channels=[{"id": real_shape}, {"id": "C_CUST"}, {"id": "G_PRIV"},
                          {"id": mixed}])
        inst = c.instances[0]
        self.assertEqual([ch.is_dm for ch in inst.channels], [True, False, False, True])
        self.assertEqual(inst.dm_channels(), (real_shape, mixed))

    def test_the_inference_is_bound_to_slacks_id_shape_not_to_a_bare_d(self):
        """Review N3: the email adapter's channel ids are mailbox names, and the DM rule
        would run on a mailbox called Drafts or Done. Only a Slack-shaped id (D plus 8+
        uppercase letters, digits or underscores) infers a DM; anything else is declared."""
        letters_only = "D" + "A" * 10                      # DEPLOYMENTS-shaped (round 2, R2-12)
        c = cfg(channels=[{"id": "Drafts"}, {"id": "Deleted Items"}, {"id": "Done"},
                          {"id": "D1234567"}, {"id": "DELETED_ITEMS"}, {"id": "D_SOMEONE"},
                          {"id": letters_only}])
        self.assertEqual([ch.is_dm for ch in c.instances[0].channels],
                         [False] * 7, "a NAME was taken for a Slack im id")
        self.assertEqual(c.instances[0].dm_channels(), ())
        declared = cfg(channels=[{"id": "Drafts", "kind": "dm"}, {"id": "D_SOMEONE", "kind": "dm"}])
        self.assertEqual([ch.is_dm for ch in declared.instances[0].channels], [True, True])

    def test_an_explicit_kind_overrides_the_inference_both_ways(self):
        c = cfg(channels=[{"id": "D_LOOKS_LIKE_DM", "kind": "channel"},
                          {"id": "C_ACTUALLY_DM", "kind": "dm"}])
        self.assertEqual([ch.is_dm for ch in c.instances[0].channels], [False, True])

    def test_an_unknown_kind_is_refused_by_name(self):
        with self.assertRaises(ConfigError) as ctx:
            cfg(channel_extra={"kind": "group"})
        self.assertIn("kind", str(ctx.exception))
        with self.assertRaises(ConfigError):
            ChannelConfig(id="C_X", kind="im")


class AnswerersTest(unittest.TestCase):
    def test_answerers_load_as_a_tuple_of_ids(self):
        c = cfg(instance_extra={"answerers": ["U_BOT", " U_OPERATOR "]})
        self.assertEqual(c.instances[0].answerers, ("U_BOT", "U_OPERATOR"))

    def test_absent_answerers_is_the_empty_rule(self):
        self.assertEqual(cfg().instances[0].answerers, ())

    def test_a_bare_string_or_an_empty_id_is_refused(self):
        """A quoted single id is a non-list and would silently credit nobody — the
        inert-key shape on the one list that closes asks."""
        for bad in ("U_BOT", ["U_BOT", ""], [42], "", None):
            with self.assertRaises(ConfigError, msg=repr(bad)) as ctx:
                cfg(instance_extra={"answerers": bad})
            self.assertIn("answerers", str(ctx.exception))


class SendMarkerTest(unittest.TestCase):
    def test_a_marker_loads_stripped(self):
        self.assertEqual(cfg(instance_extra={"send_marker": " [AGENT] "}).instances[0].send_marker,
                         "[AGENT]")

    def test_absent_marker_means_none_required(self):
        self.assertIsNone(cfg().instances[0].send_marker)

    def test_an_empty_or_non_string_marker_is_refused(self):
        for bad in ("", "   ", 7, ["[AGENT]"]):
            with self.assertRaises(ConfigError, msg=repr(bad)) as ctx:
                cfg(instance_extra={"send_marker": bad})
            self.assertIn("send_marker", str(ctx.exception))

    def test_unknown_keys_next_to_the_new_ones_are_still_refused(self):
        with self.assertRaises(ConfigError):
            cfg(instance_extra={"answerer": ["U_BOT"]})
        with self.assertRaises(ConfigError):
            cfg(channel_extra={"kinds": "dm"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
