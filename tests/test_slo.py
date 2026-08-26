"""Detection-latency SLO tests (ENH-2: push-vs-poll arrival delta, poll as ground truth).

The incumbent's headline win ("median 12.1min -> ~1min") only measured how fast its own
cron ran. The honest metric needs THREE timestamps per message — the platform's message
ts, the push store's first arrival, the poll store's first arrival — with the
completeness poll as ground truth. core/slo.py judges them:

* p50/p90 are EXPOSED for both deltas: detection latency (push_arrival - message_ts)
  and push lead (poll_arrival - push_arrival, how far ahead of the truth poll push ran);
* the check FAILS when the latency budget is breached — p90 alone breaching must fail,
  or a slow tail hides behind a healthy median;
* the check FAILS when push missed something the poll found (a poll-confirmed message
  with no push row), however good the percentiles look;
* an empty or unmeasurable comparison is an ERROR, never a pass — R8's discipline: a
  judge that measured nothing must not bless the push path.
"""
import sys
import unittest
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.checks import Verdict  # noqa: E402
from core.slo import SLOError, measure, percentile, slo_check, main  # noqa: E402
from core.store import Store  # noqa: E402


def fill(db_path, rows, channel="C_ONE", channel_type="slack"):
    """Ingest (ts, arrived_at) pairs through the real store, one clock step per row."""
    now = {"t": 0.0}
    store = Store(db_path, clock=lambda: now["t"])
    try:
        for ts, arrived_at in rows:
            now["t"] = arrived_at
            store.upsert_messages([
                {"channel_type": channel_type, "channel_id": channel,
                 "sender_id": "U_A", "ts": ts, "text": f"m{ts}"}])
    finally:
        store.close()


def strip_arrivals(db_path):
    """Simulate a store from before arrival tracking existed: messages, no arrivals."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM arrivals")
    conn.commit()
    conn.close()


class PercentileTest(unittest.TestCase):
    """Nearest-rank: the value at index ceil(n*p/100)-1 of the sorted series — a real
    observed value, never an interpolation between two."""

    def test_p50_is_the_nearest_rank_value(self):
        self.assertEqual(percentile([30.0, 10.0, 40.0, 20.0], 50), 20.0)

    def test_p90_rounds_the_rank_up_not_down(self):
        # ceil(4 * 90/100) = 4, so p90 of four values is the maximum. A floor here
        # (rank 3) is exactly the mutation that lets a slow tail hide.
        self.assertEqual(percentile([1.0, 2.0, 3.0, 4.0], 90), 4.0)

    def test_p90_of_ten_values_is_the_ninth(self):
        self.assertEqual(percentile([float(v) for v in range(1, 11)], 90), 9.0)

    def test_singleton_series(self):
        self.assertEqual(percentile([7.0], 50), 7.0)

    def test_empty_series_is_an_error(self):
        with self.assertRaises(SLOError):
            percentile([], 50)


class SLOTestCase(unittest.TestCase):
    """Fixture: 4 messages, push detects at +1s/+2s/+3s/+4s, poll confirms 60s after
    push each time. detect p50=2.0 p90=4.0; lead p50=p90=60.0."""

    MTS = [100.0, 200.0, 300.0, 400.0]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.poll_db = str(base / "poll.db")
        self.push_db = str(base / "push.db")
        push_rows = [(f"{t:.1f}", t + i) for i, t in enumerate(self.MTS, start=1)]
        poll_rows = [(ts, at + 60.0) for ts, at in push_rows]
        fill(self.push_db, push_rows, channel_type="slack_socket")
        fill(self.poll_db, poll_rows)

    def tearDown(self):
        self.tmp.cleanup()

    def measure(self, slo_p50_s=5.0, slo_p90_s=5.0):
        return measure(self.poll_db, self.push_db, "C_ONE",
                       slo_p50_s=slo_p50_s, slo_p90_s=slo_p90_s)


class ExposureTest(SLOTestCase):
    def test_detection_latency_p50_p90_are_exposed(self):
        report = self.measure()
        self.assertEqual(report.detect_p50_s, 2.0)
        self.assertEqual(report.detect_p90_s, 4.0)
        self.assertEqual(report.measured, 4)

    def test_push_lead_over_the_truth_poll_is_exposed(self):
        """THE headline metric: how far ahead of the ground-truth poll push ran. This
        is what 'median 12.1min -> ~1min' should have measured."""
        report = self.measure()
        self.assertEqual(report.lead_p50_s, 60.0)
        self.assertEqual(report.lead_p90_s, 60.0)

    def test_the_summary_exposes_both_percentile_pairs(self):
        s = self.measure().summary()
        for token in ("p50=2.000s", "p90=4.000s", "p50=60.000s", "p90=60.000s"):
            self.assertIn(token, s)


class FailingCheckTest(SLOTestCase):
    def test_a_push_miss_fails_even_when_latency_is_within_budget(self):
        """THE acceptance: a poll-confirmed message push never delivered fails the
        check regardless of how good the percentiles look — poll is the ground truth
        and a miss means the push path is silently losing messages."""
        fill(self.poll_db, [("500.0", 561.0)])
        report = self.measure(slo_p50_s=1e9, slo_p90_s=1e9)
        self.assertEqual(report.missed, {"500.0"})
        self.assertFalse(report.ok)

    def test_a_p50_breach_fails(self):
        report = self.measure(slo_p50_s=1.5, slo_p90_s=10.0)
        self.assertTrue(report.breached)
        self.assertFalse(report.ok)

    def test_a_p90_breach_fails_even_when_p50_holds(self):
        """p50 is 2.0 (within 2.5); p90 is 4.0 (over 3.5). Judging only the median
        lets a slow tail hide — the tail is where a degraded socket shows first."""
        report = self.measure(slo_p50_s=2.5, slo_p90_s=3.5)
        self.assertTrue(report.breached)
        self.assertFalse(report.ok)

    def test_within_budget_and_no_misses_passes(self):
        report = self.measure()
        self.assertFalse(report.breached)
        self.assertTrue(report.ok)


class VacuousPassTest(SLOTestCase):
    def test_an_empty_ground_truth_is_an_error_never_a_pass(self):
        """Zero poll-confirmed rows means the TRUTH side is broken (or was never
        arrival-tracked); reporting a healthy SLO would bless whatever push does next."""
        strip_arrivals(self.poll_db)
        with self.assertRaises(SLOError) as ctx:
            self.measure()
        self.assertIn("ground truth", str(ctx.exception))

    def test_no_measurable_pair_is_an_error_never_a_pass(self):
        """Push HAS every message but carries no arrival stamps (a store predating
        tracking): nothing is a miss, and nothing is measurable — that is 'cannot
        conclude', never 'healthy'."""
        strip_arrivals(self.push_db)
        with self.assertRaises(SLOError):
            self.measure()

    def test_an_unparseable_message_ts_is_an_error(self):
        """core/ is channel-agnostic, so ts is TEXT; a non-numeric ts cannot yield a
        latency and must refuse loudly rather than skip silently."""
        fill(self.poll_db, [("not-a-ts", 700.0)])
        fill(self.push_db, [("not-a-ts", 650.0)], channel_type="slack_socket")
        with self.assertRaises(SLOError):
            self.measure()


class CheckVerdictTest(SLOTestCase):
    """slo_check wraps measure() for core/checks.py registries: FAIL on miss, breach,
    or an unusable comparison — never a raise, never a silent skip."""

    def check(self, **kw):
        args = dict(slo_p50_s=5.0, slo_p90_s=5.0)
        args.update(kw)
        return slo_check("slo", self.poll_db, self.push_db, "C_ONE", **args)

    def test_pass_carries_the_measured_count_as_inspected(self):
        v = self.check()
        self.assertIsInstance(v, Verdict)
        self.assertTrue(v.ok)
        self.assertEqual(v.inspected, 4)

    def test_a_miss_is_a_failing_verdict(self):
        fill(self.poll_db, [("500.0", 561.0)])
        v = self.check(slo_p50_s=1e9, slo_p90_s=1e9)
        self.assertFalse(v.ok)
        self.assertIn("missed", v.detail)

    def test_a_breach_is_a_failing_verdict(self):
        v = self.check(slo_p50_s=1.5)
        self.assertFalse(v.ok)

    def test_an_unusable_comparison_is_a_failing_verdict_not_a_raise(self):
        strip_arrivals(self.poll_db)
        v = self.check()
        self.assertFalse(v.ok)


class MainWiringTest(SLOTestCase):
    def argv(self, p50="5", p90="5"):
        return ["--poll-db", self.poll_db, "--push-db", self.push_db,
                "--channel", "C_ONE", "--slo-p50", p50, "--slo-p90", p90]

    def test_exit_0_when_within_budget_and_nothing_missed(self):
        self.assertEqual(main(self.argv()), 0)

    def test_exit_1_on_a_miss(self):
        fill(self.poll_db, [("500.0", 561.0)])
        self.assertEqual(main(self.argv(p50="1000000", p90="1000000")), 1)

    def test_exit_1_on_a_breach(self):
        self.assertEqual(main(self.argv(p50="1.5", p90="10")), 1)

    def test_exit_2_on_an_unusable_comparison(self):
        strip_arrivals(self.poll_db)
        self.assertEqual(main(self.argv()), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
