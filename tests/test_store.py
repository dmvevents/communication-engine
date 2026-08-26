"""Tests for core/store.py — each asserts a property that MUST fail when removed.

Requirements covered: R9 (idempotent re-ingest, schema pinned), R5 (a schema drift must
raise rather than silently store nothing).
"""
import json
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
        self.assertIn("arrivals", names)


class ArrivalTest(unittest.TestCase):
    """ENH-2: the detection-latency SLO is judged from WHEN a message first landed in
    each store, so ingest must stamp a first-arrival time that a later re-ingest can
    never move. The poller re-reads overlapping windows constantly (R9) — if the stamp
    followed the LATEST sighting instead of the first, every re-poll would erase the
    exact push-vs-poll delta the SLO exists to measure."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.store = Store(Path(self.tmp.name) / "s.db", clock=lambda: self.now)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_ingest_records_when_a_message_first_arrived(self):
        self.store.upsert_messages([msg(ts="1.1")])
        self.assertEqual(self.store.arrivals("C_EXAMPLE"), {"1.1": 1000.0})

    def test_reingest_never_moves_the_first_arrival(self):
        self.store.upsert_messages([msg(ts="2.1")])
        self.now = 2000.0
        self.store.upsert_messages([msg(ts="2.1"), msg(ts="2.2")])
        arrivals = self.store.arrivals("C_EXAMPLE")
        self.assertEqual(arrivals["2.1"], 1000.0,
                         "a re-ingest moved the first arrival — the stamp must be "
                         "first-write-wins or re-polls corrupt every latency measurement")
        self.assertEqual(arrivals["2.2"], 2000.0)

    def test_an_edit_does_not_reset_the_arrival(self):
        """Message rows are REPLACEd on edit (the text updates); the arrival is when the
        message was first SEEN, which an edit does not change."""
        self.store.upsert_messages([msg(ts="3.1", text="before")])
        self.now = 3000.0
        self.store.upsert_messages([msg(ts="3.1", text="after")])
        self.assertEqual(self.store.arrivals("C_EXAMPLE")["3.1"], 1000.0)
        row = self.store.conn.execute(
            "SELECT text FROM messages WHERE ts='3.1'").fetchone()
        self.assertEqual(row[0], "after")

    def test_arrivals_are_scoped_to_the_channel(self):
        self.store.upsert_messages([msg(ts="4.1", channel_id="C_ONE"),
                                    msg(ts="4.2", channel_id="C_TWO")])
        self.assertEqual(set(self.store.arrivals("C_ONE")), {"4.1"})
        self.assertEqual(set(self.store.arrivals("C_TWO")), {"4.2"})


class AttachmentFieldTest(unittest.TestCase):
    """ENH-4: the live system downloads screenshots and treats them as content; a store
    whose contract has no attachments field forces every adapter to drop them at ingest
    — the message survives, the content does not."""

    IMAGE = {"kind": "image", "name": "screenshot.png",
             "mimetype": "image/png", "url": "https://files.example/screenshot.png"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "s.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def stored(self, ts):
        return self.store.conn.execute(
            "SELECT attachments FROM messages WHERE ts=?", (ts,)).fetchone()[0]

    def test_an_attachment_carrying_message_round_trips(self):
        self.store.upsert_messages([msg(ts="1.1", text="", attachments=[self.IMAGE])])
        self.assertEqual(json.loads(self.stored("1.1")), [self.IMAGE],
                         "the attachment did not survive persistence — the silent "
                         "drop just moved from the adapter to the store")

    def test_known_empty_is_distinct_from_never_recorded(self):
        """Same rule the journal keeps for cues (R22): [] means 'the adapter looked and
        found none', NULL means 'this row predates the field' — a reader must be able
        to tell an old row from an attachment-free one."""
        self.store.upsert_messages([msg(ts="2.1", attachments=[]),
                                    msg(ts="2.2")])
        self.assertEqual(self.stored("2.1"), "[]")
        self.assertIsNone(self.stored("2.2"))

    def test_a_non_list_attachments_value_raises(self):
        """A JSON string or a lone dict here would persist as junk and answer the
        classifier's has-attachments question with its truthiness."""
        with self.assertRaises(SchemaError):
            self.store.upsert_messages([msg(ts="3.1", attachments=self.IMAGE)])
        with self.assertRaises(SchemaError):
            self.store.upsert_messages([msg(ts="3.2", attachments="[]")])

    def test_reingest_with_attachments_stays_idempotent(self):
        batch = [msg(ts="4.1", attachments=[self.IMAGE])]
        self.store.upsert_messages(batch)
        self.store.upsert_messages(batch)
        self.assertEqual(self.store.count(), 1)

    def test_a_pre_attachment_database_is_migrated_not_refused(self):
        """Adopters hold messages.db files created before the column existed, and
        CREATE TABLE IF NOT EXISTS never alters an existing table (the journal hit
        this with the R22 cues column). Refusing the file would destroy history to
        improve it; writing past the missing column would crash every ingest."""
        old = Path(self.tmp.name) / "old.db"
        conn = sqlite3.connect(old)
        conn.executescript("""
            CREATE TABLE messages (
                channel_type TEXT NOT NULL, channel_id TEXT NOT NULL,
                ts TEXT NOT NULL, sender_id TEXT NOT NULL, sender_name TEXT,
                text TEXT, thread_id TEXT, raw TEXT,
                PRIMARY KEY (channel_type, channel_id, ts));
            INSERT INTO messages VALUES ('slack','C_EXAMPLE','0.9','U_EXAMPLE',
                                         NULL,'legacy row',NULL,NULL);
        """)
        conn.commit()
        conn.close()
        s = Store(old)
        try:
            s.upsert_messages([msg(ts="5.1", attachments=[self.IMAGE])])
            rows = dict(s.conn.execute("SELECT ts, attachments FROM messages"))
            self.assertEqual(json.loads(rows["5.1"]), [self.IMAGE])
            self.assertIsNone(rows["0.9"],
                              "a legacy row must read back None, never a fabricated "
                              "attachment record")
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
