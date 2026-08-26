"""Push-vs-poll parity watch tests (ENH-14: push for latency, poll for truth).

Socket Mode push can MISS events (10-connection cap, connections cycle, mid-flow
enablement drops) — so push output is trusted only while a continuous differ proves it
agrees with the polling store. scripts/push-poll-parity.py is that differ, built on
core/parity.py (whose vacuous-pass discipline it must PRESERVE, never dilute):

* a divergence FAILS the run (exit 1) — THE acceptance property;
* push running AHEAD of poll between poll cycles is expected, never a false alarm:
  each cycle compares only the settled window (rows at or below the poll store's
  newest ts per channel);
* it runs CONTINUOUSLY, re-reading both stores every cycle, so a loss that happens
  after startup is still caught;
* an empty poll oracle is an ERROR (exit 2), never a pass — inherited from R8.
"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.parity import ParityError  # noqa: E402
from core.store import Store  # noqa: E402

SCRIPT = ROOT / "scripts" / "push-poll-parity.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("push_poll_parity_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fill(db_path, ts_list, channel="C_ONE", channel_type="slack"):
    """Ingest rows the way the real adapters do — through the pinned store contract."""
    store = Store(db_path)
    try:
        store.upsert_messages([
            {"channel_type": channel_type, "channel_id": channel,
             "sender_id": "U_A", "ts": t, "text": f"m{t}"} for t in ts_list])
    finally:
        store.close()


class ParityWatchTestCase(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module()
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.poll_db = str(base / "poll.db")
        self.push_db = str(base / "push.db")
        self.slept = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_watch(self, cycles=1, channels=("C_ONE",), interval_s=60.0):
        return self.mod.run(self.poll_db, self.push_db, list(channels),
                            interval_s=interval_s, cycles=cycles,
                            sleep=self.slept.append, out=lambda *_: None)


class DivergenceFailsTest(ParityWatchTestCase):
    def test_push_missing_a_settled_message_fails_the_run(self):
        """THE acceptance: a message the poll (truth) store has, at or below its own
        watermark, that push never delivered — the exact silent-loss class Socket Mode
        is documented to produce. The run must FAIL, not average it away."""
        fill(self.poll_db, ["1.0", "2.0", "3.0"])
        fill(self.push_db, ["1.0", "3.0"], channel_type="slack_socket")
        self.assertEqual(self.run_watch(cycles=1), 1)

    def test_push_inventing_a_settled_row_fails_the_run(self):
        """An extra push row at/below the watermark means the POLLER gapped (or push
        fabricated a row) — either way the pair can no longer be trusted."""
        fill(self.poll_db, ["1.0", "3.0"])
        fill(self.push_db, ["1.0", "2.0", "3.0"], channel_type="slack_socket")
        self.assertEqual(self.run_watch(cycles=1), 1)

    def test_identical_stores_pass(self):
        fill(self.poll_db, ["1.0", "2.0"])
        fill(self.push_db, ["1.0", "2.0"], channel_type="slack_socket")
        self.assertEqual(self.run_watch(cycles=1), 0)


class WatermarkTest(ParityWatchTestCase):
    def test_push_running_ahead_of_poll_is_not_a_divergence(self):
        """Push is sub-second, poll is minutes: between poll cycles push is ALWAYS
        ahead. Rows above the poll watermark are 'not yet confirmable', never a
        failure — without this rule the watch would cry wolf on every fresh message
        and divergence alarms would be muted as noise."""
        fill(self.poll_db, ["1.0", "2.0"])
        fill(self.push_db, ["1.0", "2.0", "3.0", "4.0"], channel_type="slack_socket")
        self.assertEqual(self.run_watch(cycles=2), 0)


class ContinuityTest(ParityWatchTestCase):
    def test_the_watch_rereads_both_stores_every_cycle(self):
        """'Runs continuously' must mean live re-reads: a loss that happens AFTER
        startup (poll settles a message push never got) is caught on the next cycle,
        not frozen out by a snapshot taken at start."""
        fill(self.poll_db, ["1.0"])
        fill(self.push_db, ["1.0"], channel_type="slack_socket")

        def sleep_then_diverge(seconds):
            self.slept.append(seconds)
            fill(self.poll_db, ["2.0"])   # poll settles a message push never delivered

        rc = self.mod.run(self.poll_db, self.push_db, ["C_ONE"], interval_s=30.0,
                          cycles=5, sleep=sleep_then_diverge, out=lambda *_: None)
        self.assertEqual(rc, 1)
        self.assertEqual(self.slept, [30.0],
                         "the divergence must stop the run on the cycle that saw it")

    def test_clean_cycles_sleep_between_reads_and_then_exit_zero(self):
        fill(self.poll_db, ["1.0"])
        fill(self.push_db, ["1.0"], channel_type="slack_socket")
        self.assertEqual(self.run_watch(cycles=3, interval_s=7.0), 0)
        self.assertEqual(self.slept, [7.0, 7.0])


class VacuousPassTest(ParityWatchTestCase):
    def test_an_empty_poll_oracle_is_an_error_never_a_pass(self):
        """R8's hard rule, inherited: a differ that compares nothing must not pass.
        Zero poll rows means the TRUTH side is broken; reporting parity would bless
        whatever push does next."""
        fill(self.poll_db, ["1.0"], channel="C_OTHER")   # db exists; channel empty
        fill(self.push_db, ["1.0"], channel_type="slack_socket")
        with self.assertRaises(ParityError):
            self.run_watch(cycles=1)

    def test_main_exits_2_on_an_unusable_comparison(self):
        fill(self.poll_db, ["1.0"], channel="C_OTHER")
        fill(self.push_db, ["1.0"], channel_type="slack_socket")
        rc = self.mod.main(["--poll-db", self.poll_db, "--push-db", self.push_db,
                            "--channel", "C_ONE", "--cycles", "1"])
        self.assertEqual(rc, 2)


class MainWiringTest(ParityWatchTestCase):
    def test_main_exits_1_on_divergence_and_0_on_clean_cycles(self):
        fill(self.poll_db, ["1.0", "2.0"])
        fill(self.push_db, ["1.0", "2.0"], channel_type="slack_socket")
        argv = ["--poll-db", self.poll_db, "--push-db", self.push_db,
                "--channel", "C_ONE", "--cycles", "1", "--interval", "0"]
        self.assertEqual(self.mod.main(argv), 0)
        fill(self.poll_db, ["9.0"])   # settled in poll, absent from push
        self.assertEqual(self.mod.main(argv), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
