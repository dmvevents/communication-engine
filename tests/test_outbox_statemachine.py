"""Property-based exploration of the outbox state machine (ENH-11; gate G2; R1, R2).

test_outbox_faults.py pins the four HAND-ENUMERATED seams (`_crash_at`). This module
removes the enumeration: it crashes the process at EVERY boundary where the world can
observe us, in send AND in recovery, alone and interleaved, and asserts the one property
the ladder exists for — a message the caller wants delivered lands EXACTLY once.

Why boundary enumeration covers "arbitrary crash points": a crash only matters at the
moments the outside world changes — an adapter call (remote state) or a sqlite commit
(local durable state). A crash between two pure in-memory statements leaves byte-identical
recoverable state to a crash at the next such boundary, so sweeping the boundaries IS the
full crash space. The harness therefore wraps the adapter and the connection's commit();
core/outbox.py is exercised unmodified, with no cooperation from `_crash_at`.

The sweep is exhaustive where the space is small (every crash step in a send; every crash
step in the recovery AFTER each of those — the nested case no hand-written test reaches)
and seeded-random where it is not (interleavings of sends, recoveries, and restarts across
several messages, channels, placements and policies). Seeds are fixed integers: a failure
reproduces by seed, never by luck.

Stdlib only, deliberately: `hypothesis` would shrink counterexamples for us, but a new
dependency for the test tier is exactly what the repo's no-new-deps rule exists to refuse.
"""
import random
import sys
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.outbox import (COMMITTED, Outbox, PolicyError,  # noqa: E402
                         VERIFIED, idempotency_key)


class _SimCrash(BaseException):
    """Simulated process death at an instrumented boundary. BaseException for the same
    reason core's _Crash is one: an `except Exception` in the code under test must not
    be able to swallow a death and quietly finish the ladder."""


class CrashHook:
    """Counts observable-effect boundaries and dies at a chosen one.

    step() is called at every instrumented boundary; arming crash_at=N kills the process
    at the Nth. step_no after an un-crashed operation is the measured size of that
    operation's crash space — the exhaustive tests sweep exactly [1, step_no].
    """

    def __init__(self):
        self.step_no = 0
        self.crash_at = None

    def arm(self, crash_at):
        self.step_no = 0
        self.crash_at = crash_at

    def step(self):
        self.step_no += 1
        if self.crash_at is not None and self.step_no == self.crash_at:
            raise _SimCrash(f"simulated process death at boundary {self.step_no}")


class CrashConn:
    """Wraps the outbox's sqlite connection so durability itself is a crash point.

    Death BEFORE the commit loses the open transaction (rolled back here, exactly what
    the OS does to an uncommitted transaction when the process dies); death AFTER it
    leaves the write durable but nothing later executed.
    """

    def __init__(self, real, hook):
        self._real = real
        self._hook = hook

    def commit(self):
        try:
            self._hook.step()
        except _SimCrash:
            self._real.rollback()
            raise
        self._real.commit()
        self._hook.step()

    def __getattr__(self, name):
        return getattr(self._real, name)


class CrashAdapter:
    """The remote side. `delivered` is the target's ground truth: it survives our
    death, which is the whole difficulty — death before send() and death after it are
    different WORLDS, and only read-back can tell them apart."""

    def __init__(self, hook):
        self.hook = hook
        self.delivered = []      # (target, text, key, thread_id) actually on the target
        self.send_calls = 0

    def send(self, target, text, key=None, thread_id=None):
        self.hook.step()         # die with the message never leaving
        self.send_calls += 1
        self.delivered.append((target, text, key, thread_id))
        self.hook.step()         # die with the message landed and nothing recorded
        return {"ts": f"r{self.send_calls}", "key": key}

    def read_back(self, target, key):
        self.hook.step()
        return any(t == target and k == key for t, _, k, _ in self.delivered)


class FakeTime:
    """Pacing must not really sleep: sleeping advances this clock instead."""

    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


Msg = namedtuple("Msg", "target ts text thread")

DIRECT = (
    Msg("C_ALPHA", "1700000000.000100", "[AGENT] alpha: run complete.", None),
    Msg("C_ALPHA", "1700000000.000200", "[AGENT] alpha: follow-up.", None),
    Msg("C_BETA", "1700000000.000300", "[AGENT] beta: ack.", None),
    Msg("C_ALPHA", "1700000000.000100", "[AGENT] alpha: threaded detail.",
        "1700000000.000100"),
)
STAGED_MSG = Msg("C_STAGED_GATE", "1700000000.000400", "[AGENT] draft for the gate.", None)
NEVER_MSG = Msg("C_UNLISTED", "1700000000.000500", "[AGENT] must never leave.", None)

POLICIES = {"C_ALPHA": "direct", "C_BETA": "direct", "C_STAGED_GATE": "staged"}


def key_of(msg):
    return idempotency_key(msg.target, msg.ts, msg.text, msg.thread)


class Machine:
    """One simulated host: a durable database file, a remote target, and a sequence of
    processes over them of which at most one is ever alive."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "outbox.db"
        self.hook = CrashHook()
        self.adapter = CrashAdapter(self.hook)
        self.time = FakeTime()
        self.box = None

    def process(self):
        if self.box is None:
            box = Outbox(self.db, self.adapter, POLICIES,
                         clock=self.time.now, sleep=self.time.sleep)
            # Durability in this simulation lives at the commit() boundary (CrashConn),
            # not on the platter — hundreds of Machines fsyncing real files would only
            # make the sweep slow, not more honest.
            box.conn.execute("PRAGMA synchronous=OFF")
            # wrap AFTER __init__: schema creation is not part of the state machine
            box.conn = CrashConn(box.conn, self.hook)
            self.box = box
        return self.box

    def kill(self):
        """Process death: the connection closes, discarding any open transaction."""
        if self.box is not None:
            self.box.close()
            self.box = None

    def restart(self):
        """Clean process replacement — same durable state, fresh in-memory state."""
        self.kill()

    def send(self, msg, crash_at=None):
        self.hook.arm(crash_at)
        try:
            return self.process().send(msg.target, msg.ts, msg.text, msg.thread)
        except _SimCrash:
            self.kill()
            return "crashed"
        except PolicyError:
            return "refused"

    def recover(self, crash_at=None):
        self.hook.arm(crash_at)
        try:
            return self.process().recover()
        except _SimCrash:
            self.kill()
            return "crashed"

    def deliveries_of(self, key):
        return [d for d in self.adapter.delivered if d[2] == key]

    def close(self):
        self.kill()
        self.tmp.cleanup()


class StateMachineProperty(unittest.TestCase):
    # ---- invariants ---------------------------------------------------------

    def assert_no_duplicates(self, m, ctx):
        """The unrecoverable direction, checked mid-flight: a loss can still be healed
        by recovery, a duplicate is already on a customer's screen."""
        keys = [k for _, _, k, _ in m.adapter.delivered]
        dups = sorted({k for k in keys if keys.count(k) > 1})
        self.assertFalse(dups, f"{ctx}: DUPLICATE delivery of {dups}")

    def assert_gates_held(self, m, ctx):
        for msg, gate in ((STAGED_MSG, "staged"), (NEVER_MSG, "never")):
            self.assertEqual(
                len(m.deliveries_of(key_of(msg))), 0,
                f"{ctx}: a '{gate}' target was delivered to — the {gate} gate broke "
                "under crashes")

    def finish_and_check(self, m, ctx):
        """Drive the machine to quiescence the way the real loop would — recover, then
        retry every wanted message — and assert the terminal property: each DIRECT
        message delivered exactly once, proven, with nothing left pending."""
        m.restart()
        m.hook.arm(None)
        box = m.process()
        box.recover()
        for msg in DIRECT:
            box.send(msg.target, msg.ts, msg.text, msg.thread)
            # Liveness half of exactly-once: the retry must LEARN the message is
            # delivered (deduped), or a caller that never hears so retries forever.
            again = box.send(msg.target, msg.ts, msg.text, msg.thread)
            self.assertTrue(again.get("deduped"),
                            f"{ctx}: retry of {msg.text!r} reported "
                            f"{again} instead of a dedupe")
        box.recover()

        for msg in DIRECT:
            key = key_of(msg)
            n = len(m.deliveries_of(key))
            self.assertEqual(n, 1, f"{ctx}: {msg.text!r} delivered {n} times, "
                                   "expected exactly once")
            row = box.get(key)
            self.assertIn(row["state"], (VERIFIED, COMMITTED),
                          f"{ctx}: terminal state {row['state']} for {msg.text!r}")
        # A thread reply must still be IN the thread after any recovery path (ENH-3:
        # resuming a thread reply as a top-level post is the placement bug).
        threaded = DIRECT[3]
        (_, _, _, tid), = m.deliveries_of(key_of(threaded))
        self.assertEqual(tid, threaded.thread,
                         f"{ctx}: thread reply resumed with placement {tid!r}")
        self.assertEqual(box.pending(), [], f"{ctx}: unfinished rows left behind")
        self.assert_no_duplicates(m, ctx)
        self.assert_gates_held(m, ctx)

    # ---- the harness must be measuring something ----------------------------

    def test_the_crashable_surface_is_as_wide_as_the_ladder(self):
        """Vacuous-pass guard for the harness itself: if a refactor stops routing
        durability through conn.commit() or the adapter wrapper, every sweep below
        would silently shrink to nothing while staying green. The clean send must
        expose at least the ladder's own boundaries (INTENT commit, adapter send,
        SENT/VERIFIED/COMMITTED commits, read-back)."""
        m = Machine()
        try:
            m.hook.arm(None)
            r = m.send(DIRECT[0])
            self.assertEqual(r["state"], COMMITTED)
            self.assertGreaterEqual(m.hook.step_no, 9,
                                    "the instrumented crash space collapsed — the "
                                    "sweeps below are no longer exhaustive")
            self.assertEqual(len(m.deliveries_of(key_of(DIRECT[0]))), 1)
        finally:
            m.close()

    # ---- exhaustive sweeps --------------------------------------------------

    def _send_steps(self):
        m = Machine()
        try:
            m.send(DIRECT[0])
            return m.hook.step_no
        finally:
            m.close()

    def test_every_crash_point_in_send_yields_exactly_once(self):
        for k in range(1, self._send_steps() + 1):
            with self.subTest(crash_at=k):
                m = Machine()
                try:
                    self.assertEqual(m.send(DIRECT[0], crash_at=k), "crashed")
                    self.assertLessEqual(len(m.deliveries_of(key_of(DIRECT[0]))), 1,
                                         f"send crash@{k} already duplicated")
                    self.finish_and_check(m, f"send crash@{k}")
                finally:
                    m.close()

    def test_every_crash_point_in_recovery_after_every_send_crash(self):
        """The nested case the hand-enumerated seams never reach: recovery is a process
        too, and it dies at arbitrary points with its own half-done ladder. Whatever a
        crashed recovery leaves behind, the NEXT recovery must still converge to
        exactly-once — including the re-send window where the message is on the target
        but recovery hasn't recorded anything yet."""
        max_recovery_steps = 0
        for k in range(1, self._send_steps() + 1):
            probe = Machine()
            try:
                probe.send(DIRECT[0], crash_at=k)
                probe.restart()
                probe.recover()
                recovery_steps = probe.hook.step_no
                max_recovery_steps = max(max_recovery_steps, recovery_steps)
            finally:
                probe.close()
            for j in range(1, recovery_steps + 1):
                with self.subTest(send_crash=k, recovery_crash=j):
                    m = Machine()
                    try:
                        m.send(DIRECT[0], crash_at=k)
                        m.restart()
                        m.recover(crash_at=j)
                        self.assert_no_duplicates(m, f"send@{k} then recover@{j}")
                        self.finish_and_check(m, f"send@{k} then recover@{j}")
                    finally:
                        m.close()
        # harness guard, recovery half: at least one send-crash must leave recovery
        # real work (read-back, re-send, receipt/VERIFIED/COMMITTED commits) to die in
        self.assertGreaterEqual(max_recovery_steps, 8,
                                "no recovery path was actually swept")

    def test_a_staged_draft_never_escapes_at_any_crash_point(self):
        """The operator gate under the same storm: no crash point in a staged send may
        leave a state recovery later 'finishes' by sending."""
        m = Machine()
        try:
            m.hook.arm(None)
            m.send(STAGED_MSG)
            staged_steps = m.hook.step_no
        finally:
            m.close()
        for k in range(1, staged_steps + 1):
            with self.subTest(crash_at=k):
                m = Machine()
                try:
                    m.send(STAGED_MSG, crash_at=k)
                    m.restart()
                    m.recover()
                    self.assertEqual(m.adapter.send_calls, 0,
                                     f"staged crash@{k}: the draft reached the adapter")
                finally:
                    m.close()

    # ---- randomized interleavings -------------------------------------------

    def test_random_interleavings_of_sends_recoveries_and_restarts(self):
        """The space too big to enumerate: several messages, two channels, a thread
        placement, a staged and a denied target, with sends, recoveries and clean
        restarts interleaved and each operation dying at an arbitrary boundary (or
        none). 60 fixed seeds; a failure names its seed and replays exactly."""
        every = DIRECT + (STAGED_MSG, NEVER_MSG)
        for seed in range(60):
            rng = random.Random(seed)
            m = Machine()
            try:
                for step in range(rng.randint(4, 12)):
                    ctx = f"seed={seed} step={step}"
                    crash = rng.choice([None, None, None] + list(range(1, 14)))
                    op = rng.random()
                    if op < 0.60:
                        m.send(rng.choice(every), crash_at=crash)
                    elif op < 0.85:
                        m.recover(crash_at=crash)
                    else:
                        m.restart()
                    self.assert_no_duplicates(m, ctx)
                    self.assert_gates_held(m, ctx)
                self.finish_and_check(m, f"seed={seed}")
                with self.assertRaises(PolicyError,
                                       msg=f"seed={seed}: default-deny lapsed"):
                    m.process().send(NEVER_MSG.target, NEVER_MSG.ts, NEVER_MSG.text)
            finally:
                m.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
