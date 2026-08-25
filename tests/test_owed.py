"""Tests for core/owed.py — the owed-work edge (gate G3; R3, R4).

Reproduces the sev-high incident as a test: work is promised, no live driver exists, no new
inbound message arrives, and backoff is deep. The system must still notice.
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.owed import OwedRegistry  # noqa: E402


class OwedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "owed.db"
        self.alive = set()

    def tearDown(self):
        self.tmp.cleanup()

    def reg(self):
        return OwedRegistry(self.db, driver_alive=lambda d: d in self.alive)

    # ---- R4: the goal-triggered edge --------------------------------------
    def test_work_with_no_driver_is_unattended(self):
        r = self.reg()
        r.owe("g12", "prove the three DGD variants", deadline=time.time() + 3600)
        self.assertEqual(len(r.unattended()), 1)

    def test_work_with_a_LIVE_driver_is_not_unattended(self):
        r = self.reg()
        self.alive.add("pid-1222800")
        r.owe("g12", "prove the variants", driver="pid-1222800")
        self.assertEqual(r.unattended(), [])

    def test_work_whose_driver_DIED_is_unattended_again(self):
        """A driver string is not evidence — liveness is. This is the ephemeral-driver bug."""
        r = self.reg()
        self.alive.add("pid-1222800")
        r.owe("g12", "prove the variants", driver="pid-1222800")
        self.assertEqual(r.unattended(), [])
        self.alive.discard("pid-1222800")          # the detached session died
        self.assertEqual(len(r.unattended()), 1)

    def test_a_next_step_note_is_not_a_driver(self):
        """'NEXT STEP written to a file' was the actual failure — inert by definition."""
        r = self.reg()
        r.owe("g12", "NEXT STEP: live leg 1", driver=None)
        self.assertEqual(len(r.unattended()), 1,
                         "a note with no live process must count as unattended")

    def test_closed_work_is_not_unattended(self):
        r = self.reg()
        r.owe("g12", "x")
        r.close("g12")
        self.assertEqual(r.unattended(), [])
        self.assertEqual(r.open_items(), [])

    def test_detection_needs_no_inbound_message(self):
        """The whole point: nothing here simulates an inbound message."""
        r = self.reg()
        r.owe("g12", "promised work", deadline=time.time() + 60)
        self.assertTrue(r.should_fire(backoff_until=0))

    # ---- R3: backoff must not suppress owed work --------------------------
    def test_backoff_does_not_suppress_owed_work(self):
        r = self.reg()
        r.owe("g12", "promised work with a deadline")
        deep_backoff = time.time() + 3600      # an hour of self-gating
        self.assertTrue(r.should_fire(backoff_until=deep_backoff),
                        "backoff suppressed unattended owed work — this is the 8h17m bug")

    def test_backoff_still_applies_when_nothing_is_owed(self):
        """Backoff is correct for a genuinely quiet channel; we must not break that."""
        r = self.reg()
        self.assertFalse(r.should_fire(backoff_until=time.time() + 3600))

    def test_backoff_expiry_allows_a_routine_fire(self):
        r = self.reg()
        self.assertTrue(r.should_fire(backoff_until=time.time() - 1))

    def test_attended_work_under_backoff_does_not_force_a_fire(self):
        r = self.reg()
        self.alive.add("driver-1")
        r.owe("g12", "x", driver="driver-1")
        self.assertFalse(r.should_fire(backoff_until=time.time() + 600),
                         "a live driver is already making progress; no forced fire needed")

    # ---- deadlines --------------------------------------------------------
    def test_overdue_is_reported(self):
        r = self.reg()
        r.owe("late", "should have shipped", deadline=time.time() - 10)
        r.owe("fine", "later", deadline=time.time() + 1000)
        ids = [x["id"] for x in r.overdue()]
        self.assertEqual(ids, ["late"])

    def test_work_without_a_deadline_is_never_overdue(self):
        r = self.reg()
        r.owe("open-ended", "no deadline")
        self.assertEqual(r.overdue(), [])

    def test_registry_survives_a_restart(self):
        """Owed work must be durable — an in-memory list dies with the process."""
        self.reg().owe("g12", "promised")
        self.assertEqual(len(self.reg().unattended()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
