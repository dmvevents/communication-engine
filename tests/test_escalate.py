"""Tests for core/escalate.py — edge-triggered operator escalation (gate G11; R20).

The acceptance being encoded: feeding the same degraded condition N times produces exactly
ONE notification, and the recovery edge produces exactly one more. The evidence behind it:
the live probe fires once per minute, so a level-triggered notifier would emit 1,440
identical alerts a day for a single stuck condition — and a monitor that spams gets muted.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.escalate import Escalator  # noqa: E402


class EscalateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "escalate.db"
        self.sent = []

    def tearDown(self):
        self.tmp.cleanup()

    def esc(self):
        return Escalator(self.db, notify=self.sent.append)

    # ---- THE acceptance: N identical conditions -> 1 notification ----------
    def test_n_identical_degraded_observations_notify_exactly_once(self):
        e = self.esc()
        fired = [e.observe("watchdog", ok=False, detail="no heartbeat 900s")
                 for _ in range(20)]
        self.assertEqual(self.sent[0].count("watchdog"), 1)
        self.assertEqual(len(self.sent), 1,
                         "a level was re-notified — this is the 1,440-alerts/day trap")
        self.assertEqual(fired, [True] + [False] * 19)

    def test_recovery_edge_notifies_exactly_once_more(self):
        e = self.esc()
        for _ in range(5):
            e.observe("watchdog", ok=False, detail="no heartbeat 900s")
        for _ in range(5):
            e.observe("watchdog", ok=True, detail="heartbeat back")
        self.assertEqual(len(self.sent), 2,
                         "expected exactly one degraded edge + one recovery edge")
        self.assertIn("watchdog", self.sent[1])

    def test_recovery_is_announced_not_silently_recorded(self):
        """The all-clear IS news: an operator paged about a failure must be told it ended,
        or they keep investigating a problem that no longer exists."""
        e = self.esc()
        e.observe("watchdog", ok=False)
        e.observe("watchdog", ok=True)
        self.assertEqual(len(self.sent), 2)

    # ---- what is NOT news ---------------------------------------------------
    def test_first_healthy_sighting_is_not_news(self):
        """Bring-up of a healthy condition must not page anyone."""
        e = self.esc()
        for _ in range(5):
            self.assertFalse(e.observe("outbox", ok=True, detail="0 stuck rows"))
        self.assertEqual(self.sent, [])

    def test_a_changed_detail_is_still_a_level(self):
        """Identity is the condition NAME. Real details wobble (ages, percentages);
        keying dedupe on the detail string would defeat it entirely."""
        e = self.esc()
        e.observe("disk", ok=False, detail="91% full")
        e.observe("disk", ok=False, detail="92% full")
        e.observe("disk", ok=False, detail="93% full")
        self.assertEqual(len(self.sent), 1)

    def test_first_sighting_degraded_IS_news(self):
        e = self.esc()
        self.assertTrue(e.observe("parity", ok=False, detail="3 missed"))
        self.assertEqual(len(self.sent), 1)

    # ---- the cron shape: every poll is a fresh process ----------------------
    def test_edges_survive_a_process_restart(self):
        """The 1,440/day failure mode is cron-shaped: each poll is a NEW process, so an
        in-memory 'already alerted' flag dedupes nothing. State must be durable."""
        e1 = self.esc()
        e1.observe("watchdog", ok=False, detail="stale")
        e1.close_db()
        e2 = self.esc()                                 # the next minute's fire
        self.assertFalse(e2.observe("watchdog", ok=False, detail="stale"))
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(e2.observe("watchdog", ok=True))    # recovery still edges
        self.assertEqual(len(self.sent), 2)

    # ---- alert loss is worse than alert duplication --------------------------
    def test_a_failed_notify_does_not_swallow_the_edge(self):
        """Opposite trade from the outbox: a duplicate operator page is an annoyance, a
        LOST page is an unwatched outage. A notify that raised must leave the edge
        uncommitted so the next observation retries it."""
        delivered = []
        calls = {"n": 0}

        def flaky(msg):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("notification channel down")
            delivered.append(msg)

        e = Escalator(self.db, notify=flaky)
        with self.assertRaises(RuntimeError):
            e.observe("watchdog", ok=False, detail="stale")
        self.assertTrue(e.observe("watchdog", ok=False, detail="stale"),
                        "the edge was marked reported even though notify failed — "
                        "the alert is lost forever")
        self.assertEqual(len(delivered), 1)
        self.assertFalse(e.observe("watchdog", ok=False, detail="stale"),
                         "once actually delivered, the level must go quiet again")

    # ---- independence and content --------------------------------------------
    def test_conditions_do_not_mask_each_other(self):
        e = self.esc()
        e.observe("watchdog", ok=False)
        e.observe("outbox", ok=False)
        self.assertEqual(len(self.sent), 2)
        e.observe("watchdog", ok=True)
        self.assertEqual(len(self.sent), 3)

    def test_flapping_notifies_on_every_real_edge(self):
        """Edge-triggered means every TRANSITION is news; damping a flap is a different
        feature and silently eating real transitions would hide an oscillating fault."""
        e = self.esc()
        for ok in (False, True, False, True):
            e.observe("link", ok=ok)
        self.assertEqual(len(self.sent), 4)

    def test_notification_names_the_condition_and_carries_the_detail(self):
        """An alert that does not say WHAT degraded just moves the outage to the operator's
        grep. The name and the evidence must ride in the message itself."""
        e = self.esc()
        e.observe("parity", ok=False, detail="3 missed, 1 extra")
        self.assertIn("parity", self.sent[0])
        self.assertIn("3 missed, 1 extra", self.sent[0])
        e.observe("parity", ok=True, detail="clean diff")
        self.assertNotEqual(self.sent[0], self.sent[1],
                            "recovery must be distinguishable from degradation")

    def test_degraded_since_is_the_edge_not_the_latest_level(self):
        """`changed_at` answers 'degraded since WHEN' — repeated levels must not slide it
        forward, or a 6-hour outage reads as 60 seconds old."""
        e = self.esc()
        e.observe("watchdog", ok=False, now=1000.0)
        e.observe("watchdog", ok=False, now=4600.0)
        row = e.state_of("watchdog")
        self.assertEqual(row["changed_at"], 1000.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
