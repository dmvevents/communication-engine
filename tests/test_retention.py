"""Tests for core/retention.py — deletion detection (R26).

The point of this module is that `core/parity.py` ACCEPTS the `UNRETRIEVABLE` class, so the
differ is structurally blind to that class growing. These tests pin the two refusals that
keep the blindness from moving here: an empty previous snapshot cannot conclude "nothing
was deleted", and an empty current snapshot must not be reported as a mass deletion.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.checks import Verdict, deletion_burst_check  # noqa: E402
from core.retention import (  # noqa: E402
    RetentionError, Tombstones, newly_unretrievable, read_snapshot, reconcile)


class NewlyUnretrievableTest(unittest.TestCase):
    def test_a_row_we_hold_that_stopped_being_served_is_detected(self):
        self.assertEqual(
            newly_unretrievable({"1.0", "2.0"}, {"1.0"}, {"1.0", "2.0"}), {"2.0"})

    def test_a_deleted_row_we_never_stored_is_not_our_concern(self):
        """It is already UNRETRIEVABLE to the differ and changes nothing about our archive."""
        self.assertEqual(newly_unretrievable({"1.0", "2.0"}, {"1.0"}, {"1.0"}), set())

    def test_nothing_deleted_reports_nothing(self):
        self.assertEqual(newly_unretrievable({"1.0"}, {"1.0", "2.0"}, {"1.0"}), set())

    def test_an_empty_previous_snapshot_raises_instead_of_reporting_zero(self):
        """No recorded past means no basis for 'nothing was lost' — the vacuous pass."""
        with self.assertRaises(RetentionError):
            newly_unretrievable(set(), {"1.0"}, {"1.0"})

    def test_an_empty_current_snapshot_raises_instead_of_mass_deletion(self):
        """Identical to 'everything was deleted', but lost read access is likelier — and
        paging an operator to the wrong incident is worse than not paging."""
        with self.assertRaises(RetentionError):
            newly_unretrievable({"1.0", "2.0"}, set(), {"1.0", "2.0"})


class TombstoneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "tomb.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_records_when_a_row_stopped_being_retrievable(self):
        t = Tombstones(self.db, clock=lambda: 100.0)
        self.assertEqual(t.record("C_A", ["1.0", "2.0"]), 2)
        self.assertEqual(t.all_for("C_A"), {"1.0": 100.0, "2.0": 100.0})
        t.close()

    def test_the_detected_instant_never_moves_on_re_run(self):
        """First-write-wins: the moment the platform's copy vanished is a fact about the
        past, and re-running the reconciliation must not rewrite history."""
        clock = [100.0]
        t = Tombstones(self.db, clock=lambda: clock[0])
        t.record("C_A", ["1.0"])
        clock[0] = 900.0
        self.assertEqual(t.record("C_A", ["1.0"]), 0, "already tombstoned — not new")
        self.assertEqual(t.all_for("C_A"), {"1.0": 100.0})
        t.close()

    def test_tombstones_are_scoped_per_channel(self):
        t = Tombstones(self.db, clock=lambda: 1.0)
        t.record("C_A", ["1.0"])
        t.record("C_B", ["1.0"])
        self.assertEqual(t.count("C_A"), 1)
        self.assertEqual(t.count(), 2)
        t.close()

    def test_recording_a_tombstone_never_deletes_a_message(self):
        """The store is an ARCHIVE: the platform losing its copy must not destroy ours."""
        t = Tombstones(self.db, clock=lambda: 1.0)
        t.record("C_A", ["1.0"])
        tables = {r[0] for r in sqlite3.connect(self.db).execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {"tombstones"}, "retention owns nothing but its own record")
        t.close()


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Tombstones(Path(self.tmp.name) / "tomb.db", clock=lambda: 5.0)

    def tearDown(self):
        self.t.close()
        self.tmp.cleanup()

    def test_reports_and_records_in_one_pass(self):
        r = reconcile("C_A", {"1.0", "2.0"}, {"1.0"}, {"1.0", "2.0"}, self.t)
        self.assertEqual(r["newly_unretrievable"], {"2.0"})
        self.assertEqual(r["newly_tombstoned"], 1)
        self.assertEqual(r["total_tombstoned"], 1)

    def test_a_persistent_deletion_stops_being_NEW_on_the_next_fire(self):
        """Edge, not level (core/escalate.py's rule): the same deletion must not re-alert
        every two hours forever."""
        reconcile("C_A", {"1.0", "2.0"}, {"1.0"}, {"1.0", "2.0"}, self.t)
        again = reconcile("C_A", {"1.0", "2.0"}, {"1.0"}, {"1.0", "2.0"}, self.t)
        self.assertEqual(again["newly_unretrievable"], {"2.0"}, "still missing")
        self.assertEqual(again["newly_tombstoned"], 0, "but no longer news")
        self.assertEqual(again["total_tombstoned"], 1)

    def test_works_without_a_tombstone_store(self):
        r = reconcile("C_A", {"1.0", "2.0"}, {"1.0"}, {"1.0", "2.0"})
        self.assertEqual(r["newly_unretrievable"], {"2.0"})
        self.assertEqual(r["newly_tombstoned"], 0)


class ReadSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, body):
        p = Path(self.tmp.name) / "s.json"
        p.write_text(body)
        return p

    def test_reads_a_list(self):
        self.assertEqual(read_snapshot(self._write('["1.0","2.0"]')), {"1.0", "2.0"})

    def test_missing_file_raises(self):
        with self.assertRaises(RetentionError):
            read_snapshot(Path(self.tmp.name) / "nope.json")

    def test_malformed_json_raises_rather_than_reading_as_empty(self):
        """An empty read would become 'the previous snapshot is empty' downstream, which is
        a different and much more confusing failure than 'this file is broken'."""
        with self.assertRaises(RetentionError):
            read_snapshot(self._write("{oops"))


class DeletionBurstCheckTest(unittest.TestCase):
    def test_a_burst_over_budget_fails_and_says_parity_cannot_see_it(self):
        v = deletion_burst_check("deletions", 500, budget=10)
        self.assertFalse(v.ok)
        self.assertIn("ACCEPTS", v.detail)

    def test_within_budget_passes(self):
        self.assertTrue(deletion_burst_check("deletions", 3, budget=10).ok)

    def test_no_budget_is_a_failure_not_a_pass(self):
        """Same rule as freshness_check: an undefined threshold cannot clear a count, and a
        generous default would tolerate exactly the mass deletion this check is for."""
        v = deletion_burst_check("deletions", 0, budget=None)
        self.assertFalse(v.ok)
        self.assertIn("no deletion budget", v.detail)

    def test_a_clean_verdict_still_inspected_something(self):
        """Verdict.passed refuses inspected<=0, so this check cannot vacuously pass."""
        self.assertIsInstance(deletion_burst_check("deletions", 0, budget=1), Verdict)
        self.assertGreater(deletion_burst_check("deletions", 0, budget=1).inspected, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
