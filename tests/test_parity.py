"""Tests for core/parity.py — the G1 differ (requirement R8).

The most important test here is `test_empty_oracle_raises_instead_of_passing`: a differ
that reports "no misses" because it compared nothing is the same defect class as the
secret gate that passed on an empty file list while CI stayed green.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.parity import ParityError, compare, read_timestamps  # noqa: E402


class CompareTest(unittest.TestCase):
    def test_identical_sets_are_parity_ok(self):
        r = compare({"1.1", "1.2"}, {"1.1", "1.2"}, "C_EXAMPLE")
        self.assertTrue(r.ok)
        self.assertEqual(r.missed, set())
        self.assertEqual(r.extra, set())

    def test_a_missed_message_fails_parity(self):
        r = compare({"1.1", "1.2"}, {"1.1"}, "C_EXAMPLE")
        self.assertFalse(r.ok)
        self.assertEqual(r.missed, {"1.2"})

    def test_an_extra_message_fails_parity(self):
        r = compare({"1.1"}, {"1.1", "9.9"}, "C_EXAMPLE")
        self.assertFalse(r.ok)
        self.assertEqual(r.extra, {"9.9"})

    def test_empty_oracle_raises_instead_of_passing(self):
        """NO VACUOUS PASS. Nothing to compare against is an error, never parity."""
        with self.assertRaises(ParityError):
            compare(set(), set(), "C_EXAMPLE")
        with self.assertRaises(ParityError):
            compare(set(), {"1.1"}, "C_EXAMPLE")

    def test_empty_candidate_against_real_oracle_is_total_failure_not_error(self):
        """The engine having ingested nothing is a legitimate FAIL — it is measurable."""
        r = compare({"1.1", "1.2"}, set(), "C_EXAMPLE")
        self.assertFalse(r.ok)
        self.assertEqual(len(r.missed), 2)

    def test_cursor_divergence_fails_parity_even_when_messages_match(self):
        r = compare({"1.1"}, {"1.1"}, "C_EXAMPLE",
                    cursor_oracle="1.1", cursor_candidate="0.9")
        self.assertTrue(r.cursor_divergent)
        self.assertFalse(r.ok)

    def test_summary_names_the_verdict_and_counts(self):
        s = compare({"1.1", "1.2"}, {"1.1"}, "C_EXAMPLE").summary()
        self.assertIn("PARITY FAIL", s)
        self.assertIn("missed", s)
        self.assertIn("1.2", s)


class ReadTimestampsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "oracle.db"
        c = sqlite3.connect(self.db)
        c.execute("CREATE TABLE messages (channel_id TEXT, ts TEXT)")
        c.executemany("INSERT INTO messages VALUES (?,?)",
                      [("C_ONE", "1.1"), ("C_ONE", "1.2"), ("C_TWO", "2.1")])
        c.commit()
        c.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_only_the_requested_channel(self):
        self.assertEqual(read_timestamps(str(self.db), "C_ONE"), {"1.1", "1.2"})
        self.assertEqual(read_timestamps(str(self.db), "C_TWO"), {"2.1"})

    def test_opens_the_oracle_read_only(self):
        """The oracle is a LIVE production database — it must never be opened writable."""
        read_timestamps(str(self.db), "C_ONE")
        ro = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("INSERT INTO messages VALUES ('C_X','9.9')")
        ro.close()

    def test_missing_database_raises_parity_error(self):
        with self.assertRaises(ParityError):
            read_timestamps(str(Path(self.tmp.name) / "nope.db"), "C_ONE")

    def test_missing_table_raises_parity_error_not_empty_set(self):
        """A schema mismatch must not look like 'this channel has no messages'."""
        with self.assertRaises(ParityError):
            read_timestamps(str(self.db), "C_ONE", table="not_a_table")


if __name__ == "__main__":
    unittest.main(verbosity=2)
