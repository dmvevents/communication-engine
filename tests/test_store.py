"""Tests for core/store.py — each asserts a property that MUST fail when removed.

Requirements covered: R9 (idempotent re-ingest, schema pinned), R5 (a schema drift must
raise rather than silently store nothing).
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.store import SchemaError, Store  # noqa: E402


def msg(ts="1700000000.000100", text="hello", **kw):
    m = {"channel_type": "slack", "channel_id": "C_EXAMPLE", "sender_id": "U_EXAMPLE",
         "ts": ts, "text": text}
    m.update(kw)
    return m


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "s.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    # ---- R9: round trip + idempotency ------------------------------------
    def test_round_trip_reads_back_what_was_written(self):
        self.store.upsert_messages([msg(ts="1.1"), msg(ts="1.2")])
        self.assertEqual(self.store.count("C_EXAMPLE"), 2)
        self.assertEqual(self.store.timestamps("C_EXAMPLE"), {"1.1", "1.2"})

    def test_reingesting_the_same_batch_does_not_duplicate(self):
        """The live poller re-polls overlapping windows; duplicates would corrupt parity."""
        batch = [msg(ts="2.1"), msg(ts="2.2"), msg(ts="2.3")]
        self.store.upsert_messages(batch)
        first = self.store.count()
        self.store.upsert_messages(batch)          # same batch again
        self.store.upsert_messages(batch)          # and again
        self.assertEqual(self.store.count(), first,
                         "re-ingest changed the row count — upsert is not idempotent")

    def test_same_ts_different_channel_is_a_distinct_row(self):
        """PK is (channel_type, channel_id, ts) — a shared ts across channels is normal."""
        self.store.upsert_messages([msg(ts="3.1", channel_id="C_ONE"),
                                    msg(ts="3.1", channel_id="C_TWO")])
        self.assertEqual(self.store.count(), 2)

    def test_upsert_updates_text_for_an_existing_key(self):
        self.store.upsert_messages([msg(ts="4.1", text="before")])
        self.store.upsert_messages([msg(ts="4.1", text="after")])
        row = self.store.conn.execute(
            "SELECT text FROM messages WHERE ts='4.1'").fetchone()
        self.assertEqual(row[0], "after")
        self.assertEqual(self.store.count(), 1)

    # ---- R5: the schema is pinned, in BOTH directions ---------------------
    def test_missing_required_field_raises(self):
        """This is the `.timestamp` vs `.ts` bug: a renamed field must not pass silently."""
        bad = msg()
        del bad["ts"]
        with self.assertRaises(SchemaError):
            self.store.upsert_messages([bad])

    def test_renamed_field_raises_rather_than_storing_nothing(self):
        bad = msg()
        bad["timestamp"] = bad.pop("ts")           # the exact origin-system drift
        with self.assertRaises(SchemaError):
            self.store.upsert_messages([bad])

    def test_unknown_field_raises(self):
        with self.assertRaises(SchemaError):
            self.store.upsert_messages([msg(surprise="drift")])

    def test_none_in_a_required_field_raises(self):
        bad = msg()
        bad["sender_id"] = None
        with self.assertRaises(SchemaError):
            self.store.upsert_messages([bad])

    def test_a_bad_message_does_not_half_land_the_batch(self):
        """Validation runs before any write, so one bad row cannot leave a partial batch."""
        good, bad = msg(ts="5.1"), msg(ts="5.2")
        del bad["text"]
        with self.assertRaises(SchemaError):
            self.store.upsert_messages([good, bad])
        self.assertEqual(self.store.count(), 0,
                         "a rejected batch partially landed — validation must precede writes")

    # ---- cursors ----------------------------------------------------------
    def test_cursor_round_trip_and_overwrite(self):
        self.assertIsNone(self.store.cursor_get("inst", "C_EXAMPLE"))
        self.store.cursor_set("inst", "C_EXAMPLE", "1700000000.1")
        self.assertEqual(self.store.cursor_get("inst", "C_EXAMPLE"), "1700000000.1")
        self.store.cursor_set("inst", "C_EXAMPLE", "1700000001.9")
        self.assertEqual(self.store.cursor_get("inst", "C_EXAMPLE"), "1700000001.9")

    def test_cursors_are_isolated_per_instance(self):
        self.store.cursor_set("a", "C_EXAMPLE", "10")
        self.store.cursor_set("b", "C_EXAMPLE", "20")
        self.assertEqual(self.store.cursor_get("a", "C_EXAMPLE"), "10")
        self.assertEqual(self.store.cursor_get("b", "C_EXAMPLE"), "20")

    def test_schema_tables_exist(self):
        names = {r[0] for r in self.store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("messages", names)
        self.assertIn("cursors", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
