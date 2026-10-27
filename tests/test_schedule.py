"""Tests for core/schedule.py — the reference scheduler loop (ENH-6; R3, R4, R20).

The loop is where the incumbent's hardest bugs lived, so each one is reproduced here as
a test against the SHIPPED loop, not left for every adopter to rediscover:

* single-instance guard — two pollers on one state directory double-classify, race the
  cursor, and (once a send path exists) double-deliver; the second instance must be
  REFUSED, and a crashed holder must not leave a stale lock behind
* cursor-commit ordering — poll-then-journal-then-commit is a dual-write (the incumbent
  auto-reconciled its send/cursor variant of this seam 24 times); a crash between the
  journal and the cursor commit must DUPLICATE work on the next fire, never lose it
* backoff suppressing owed work (R3) — idle backoff self-gated the incumbent to
  60-minute intervals exactly when owed work had stalled; unattended owed work must
  restore base cadence however deep the backoff
* the goal-triggered edge (R4) — promised work with no live driver must be detected and
  escalated with NO inbound message anywhere, and the escalation must be an edge, not a
  1,440-alerts/day level (R20)
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.escalate import Escalator  # noqa: E402
from core.journal import Journal  # noqa: E402
from core.owed import OwedRegistry  # noqa: E402
from core.schedule import (  # noqa: E402
    AlreadyRunning, Scheduler, SingleInstanceGuard, Source, route)
from core.store import Store  # noqa: E402


def msg(channel, ts, text, **extra):
    m = {"channel_type": "fake", "channel_id": channel, "sender_id": "U1",
         "ts": ts, "text": text}
    m.update(extra)
    return m


class QueueAdapter:
    """Serves one scripted (messages, new_cursor) batch per poll, then goes quiet.
    Scripting by CALL is what the route/edge tests need: cycle N's input is batch N."""

    def __init__(self, batches=()):
        self.batches = list(batches)

    def poll(self, cursor):
        if self.batches:
            return self.batches.pop(0)
        return [], cursor


class CursorKeyedAdapter:
    """Serves batches keyed by the cursor VALUE — replay is driven by the engine's own
    cursor, which is exactly the property the crash-ordering test measures."""

    def __init__(self, script):
        self.script = script          # {cursor: (messages, new_cursor)}

    def poll(self, cursor):
        return self.script.get(cursor, ([], cursor))


class CrashingJournal(Journal):
    """Raises on the Nth record() call while armed — the seam between the journal write
    and the cursor commit, where the incumbent's dual-write lived."""

    def __init__(self, path, crash_on):
        super().__init__(path)
        self.calls, self.crash_on, self.armed = 0, crash_on, True

    def record(self, *a, **k):
        self.calls += 1
        if self.armed and self.calls == self.crash_on:
            raise RuntimeError("injected crash between journal and cursor commit")
        return super().record(*a, **k)


class SchedulerHarness(unittest.TestCase):
    """Common fixture: real sqlite parts in a temp dir, injected clock/sleep/liveness."""

    BASE, MAX = 60.0, 960.0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.now = 1000.0
        self.slept = []
        self.alive = set()
        self.pages = []
        self.store = Store(base / "messages.db")
        self.journal = Journal(base / "journal.db")
        self.owed = OwedRegistry(base / "owed.db",
                                 driver_alive=lambda d: d in self.alive)
        self.escalator = Escalator(base / "escalate.db", notify=self.pages.append)
        self.lock_path = base / "scheduler.lock"

    def tearDown(self):
        for part in (self.store, self.journal):
            part.close()
        for part in (self.owed, self.escalator):
            part.close_db()
        self.tmp.cleanup()

    def scheduler(self, adapter, channels=("C1",), journal=None, lock_path=None,
                  escalate_ambiguous=False):
        return Scheduler(
            store=self.store, journal=journal or self.journal, owed=self.owed,
            escalator=self.escalator,
            sources=[Source(name="inst", adapter=adapter, channels=tuple(channels),
                            escalate_ambiguous=escalate_ambiguous)],
            base_interval=self.BASE, max_interval=self.MAX, lock_path=lock_path,
            clock=lambda: self.now,
            sleep=self._sleep)

    def _sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class SingleInstanceGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock = Path(self.tmp.name) / "scheduler.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_second_instance_is_refused_while_the_first_holds(self):
        holder = SingleInstanceGuard(self.lock).acquire()
        try:
            with self.assertRaises(AlreadyRunning) as ctx:
                SingleInstanceGuard(self.lock).acquire()
            self.assertIn(str(self.lock), str(ctx.exception),
                          "the refusal must NAME the lock so the operator can find "
                          "the holder")
        finally:
            holder.release()

    def test_the_lock_dies_with_its_holder(self):
        """The stale-lockfile failure a PID file invites: holder crashes, lock file
        remains, nothing ever runs again. The guard is an OS-held sqlite lock, so a
        dead holder releases it with no cleanup step."""
        holder = SingleInstanceGuard(self.lock).acquire()
        holder.conn.close()            # simulate a crash: no clean release() ran
        SingleInstanceGuard(self.lock).acquire().release()

    def test_release_frees_the_lock_for_the_next_run(self):
        SingleInstanceGuard(self.lock).acquire().release()
        SingleInstanceGuard(self.lock).acquire().release()

    def test_guards_on_different_state_dirs_are_independent(self):
        """Two schedulers on two DIFFERENT state directories are two deployments, not a
        conflict — the guard is per state, not per host."""
        other = Path(self.tmp.name) / "other.lock"
        a = SingleInstanceGuard(self.lock).acquire()
        try:
            SingleInstanceGuard(other).acquire().release()
        finally:
            a.release()


class GuardedRunTest(SchedulerHarness):
    def test_run_refuses_while_another_instance_holds_the_lock(self):
        holder = SingleInstanceGuard(self.lock_path).acquire()
        try:
            sched = self.scheduler(QueueAdapter([([msg("C1", "1.0", "hi")], "1.0")]),
                                   lock_path=self.lock_path)
            with self.assertRaises(AlreadyRunning):
                sched.run(max_cycles=1)
            self.assertEqual(self.journal.row_count(), 0,
                             "the refused instance ran a cycle anyway — two loops were "
                             "live on one state directory")
        finally:
            holder.release()

    def test_run_acquires_and_releases_so_the_next_run_can_start(self):
        adapter = QueueAdapter()
        self.scheduler(adapter, lock_path=self.lock_path).run(max_cycles=1)
        self.scheduler(adapter, lock_path=self.lock_path).run(max_cycles=1)


class CursorOrderingTest(SchedulerHarness):
    """A crash between the journal write and the cursor commit must duplicate, never
    lose. The incumbent's variant of this dual-write (send + cursor) was auto-reconciled
    24 times; the poll-side variant has no reconciler, so the ordering IS the fix."""

    def test_a_crash_between_journal_and_cursor_never_loses_a_message(self):
        journal = CrashingJournal(Path(self.tmp.name) / "crash-journal.db", crash_on=2)
        adapter = CursorKeyedAdapter(
            {None: ([msg("C1", "1.0", "first"), msg("C1", "2.0", "second")], "2.0")})
        sched = self.scheduler(adapter, journal=journal)

        with self.assertRaises(RuntimeError):
            sched.cycle()              # journals "first", dies before "second"
        self.assertIsNone(self.store.cursor_get("inst", "C1"),
                          "the cursor advanced past a message that never reached the "
                          "journal — a crash here silently loses it forever")

        journal.armed = False          # the supervisor restarts the loop
        sched.cycle()
        try:
            self.assertEqual(journal.row_count(), 2,
                             "the re-poll did not recover the unjournaled message")
            self.assertEqual(journal.distinct_count(), 2)
            self.assertEqual(journal.get("C1", "1.0")["seen_count"], 2,
                             "the replayed first message should be a re-sighting — "
                             "duplicate-and-absorb is the contract, not lose")
            self.assertEqual(self.store.cursor_get("inst", "C1"), "2.0")
        finally:
            journal.close()

    def test_a_clean_cycle_persists_the_cursor(self):
        adapter = CursorKeyedAdapter({None: ([msg("C1", "1.0", "hello")], "1.0")})
        self.scheduler(adapter).cycle()
        self.assertEqual(self.store.cursor_get("inst", "C1"), "1.0")


class OwedEdgeTest(SchedulerHarness):
    """R4 at the loop level: the whole point is that nothing here sends a message."""

    def test_unattended_owed_work_escalates_with_no_inbound_message(self):
        self.owed.owe("g12", "promised work, driver never started")
        self.scheduler(QueueAdapter()).cycle()
        self.assertEqual(len(self.pages), 1)
        self.assertIn("DEGRADED", self.pages[0])
        self.assertIn(Scheduler.CONDITION, self.pages[0])

    def test_the_escalation_is_an_edge_not_a_level(self):
        """R20: one stuck condition polled every cycle must page ONCE, not per cycle."""
        self.owed.owe("g12", "still stalled")
        sched = self.scheduler(QueueAdapter())
        sched.cycle()
        sched.cycle()
        sched.cycle()
        self.assertEqual(len(self.pages), 1,
                         "the loop re-paged an unchanged condition — this is the "
                         "1,440-alerts/day trap the escalator exists to prevent")

    def test_recovery_is_announced(self):
        self.owed.owe("g12", "stalled, then picked up")
        sched = self.scheduler(QueueAdapter())
        sched.cycle()
        self.owed.attach_driver("g12", "d1")
        self.alive.add("d1")
        sched.cycle()
        self.assertEqual(len(self.pages), 2)
        self.assertIn("RECOVERED", self.pages[1])

    def test_a_healthy_loop_stays_quiet(self):
        sched = self.scheduler(QueueAdapter())
        sched.cycle()
        sched.cycle()
        self.assertEqual(self.pages, [])


class BackoffTest(SchedulerHarness):
    def cycle_and_wait(self, sched):
        sched.cycle()
        return sched.seconds_until_fire(self.now)

    def test_a_scheduler_that_has_never_fired_fires_immediately(self):
        self.assertEqual(self.scheduler(QueueAdapter()).seconds_until_fire(self.now), 0)

    def test_idle_cycles_widen_the_interval_up_to_the_cap(self):
        sched = self.scheduler(QueueAdapter())
        waits = [self.cycle_and_wait(sched) for _ in range(5)]
        self.assertEqual(waits, [120.0, 240.0, 480.0, 960.0, 960.0],
                         "idle backoff must double per quiet cycle and cap — correct "
                         "for a quiet channel, which is why R3 is an override not a "
                         "removal")

    def test_activity_resets_the_backoff_to_base(self):
        sched = self.scheduler(QueueAdapter([
            ([], None), ([], None), ([], None),
            ([msg("C1", "1.0", "hello")], "1.0"),
        ]))
        for _ in range(3):
            sched.cycle()
        self.assertGreater(sched.seconds_until_fire(self.now), self.BASE)
        sched.cycle()                  # the batch with a fresh message
        self.assertEqual(sched.seconds_until_fire(self.now), self.BASE)

    def test_owed_work_restores_base_cadence_under_deep_backoff(self):
        """R3 wired into the loop: the incumbent looked MORE idle the longer it was
        failing, because the stalled work itself kept the channel quiet."""
        sched = self.scheduler(QueueAdapter())
        for _ in range(5):
            sched.cycle()              # deep backoff: interval is at the 960s cap
        self.owed.owe("g12", "promised work, no driver")
        self.assertEqual(sched.seconds_until_fire(self.now), self.BASE,
                         "backoff suppressed owed work — this is the 8h17m bug")

    def test_owed_work_is_a_cadence_not_a_spin(self):
        """The override restores BASE cadence; it must not turn the loop into a hot
        spin that hammers every adapter while the owed work waits on a human."""
        sched = self.scheduler(QueueAdapter())
        sched.cycle()
        self.owed.owe("g12", "promised work, no driver")
        self.assertEqual(sched.seconds_until_fire(self.now), self.BASE)
        self.assertGreater(sched.seconds_until_fire(self.now), 0)

    def test_attended_owed_work_leaves_backoff_alone(self):
        self.alive.add("d1")
        sched = self.scheduler(QueueAdapter())
        for _ in range(3):
            sched.cycle()
        self.owed.owe("g12", "being worked right now", driver="d1")
        self.assertGreater(sched.seconds_until_fire(self.now), self.BASE,
                           "a live driver is already making progress; no forced "
                           "cadence needed")


class RouteTest(SchedulerHarness):
    """Route is the classify->consequence step: every actionable kind becomes OWED work
    (visible until attended, per R4), and the journal row records the destination so
    the audit trail shows not just what arrived but where the loop pointed it."""

    def route_one(self, text, ts="1.0", **extra):
        self.scheduler(QueueAdapter([([msg("C1", ts, text, **extra)], ts)])).cycle()
        return self.journal.get("C1", ts)

    def open_ids(self):
        return {r["id"] for r in self.owed.open_items()}

    def test_an_exec_request_is_routed_to_owed_work(self):
        row = self.route_one("please deploy the fix")
        self.assertEqual(row["kind"], "EXEC-REQUEST")
        self.assertEqual(row["routed"], "owed:exec")
        self.assertEqual(self.open_ids(), {"msg@C1@1.0"})

    def test_a_commitment_ask_is_routed_to_a_human(self):
        row = self.route_one("can you approve the rollout")
        self.assertEqual(row["kind"], "COMMITMENT-ASK")
        self.assertEqual(row["routed"], "owed:operator")
        self.assertEqual(self.open_ids(), {"msg@C1@1.0"})

    def test_a_question_is_routed_to_owed_answer(self):
        row = self.route_one("where is the dashboard?")
        self.assertEqual(row["kind"], "QUESTION")
        self.assertEqual(row["routed"], "owed:answer")
        self.assertEqual(self.open_ids(), {"msg@C1@1.0"})

    def test_an_attachment_only_message_is_routed_to_eyes(self):
        row = self.route_one("", attachments=[{"kind": "image", "name": "s.png"}])
        self.assertEqual(row["kind"], "ATTACHMENT-ONLY")
        self.assertEqual(row["routed"], "owed:eyes")
        self.assertEqual(self.open_ids(), {"msg@C1@1.0"})

    def test_a_statement_is_logged_and_owes_nothing(self):
        row = self.route_one("the deploy finished without errors")
        self.assertEqual(row["kind"], "STATEMENT")
        self.assertEqual(row["routed"], "logged")
        self.assertEqual(self.open_ids(), set(),
                         "a STATEMENT created owed work — every remark would page the "
                         "operator forever")

    def test_an_unknown_kind_fails_toward_a_human(self):
        """A future classifier kind this map has never heard of must not be silently
        inert (the F-2 class) — route it to the operator, never to 'logged'."""
        self.assertEqual(route("SOME-FUTURE-KIND"), "owed:operator")

    def test_a_reseen_message_does_not_reopen_closed_work(self):
        """Re-polls duplicate (R9); a duplicate sighting of a handled ask must not
        resurrect it."""
        adapter = CursorKeyedAdapter({None: ([msg("C1", "1.0", "where is it?")], "1.0")})
        sched = self.scheduler(adapter)
        sched.cycle()
        self.owed.close("msg@C1@1.0")
        self.store.conn.execute("DELETE FROM cursors")   # force an overlapping re-poll
        self.store.conn.commit()
        sched.cycle()
        self.assertEqual(self.open_ids(), set())

    def test_an_answered_ask_closes_its_owed_work(self):
        adapter = QueueAdapter([([msg("C1", "1.0", "where is the dashboard?")], "1.0")])
        sched = self.scheduler(adapter)
        sched.cycle()
        self.assertEqual(self.open_ids(), {"msg@C1@1.0"})
        self.journal.mark_responded("C1", "1.0", "outbox-key-1")
        sched.cycle()
        self.assertEqual(self.open_ids(), set(),
                         "the ask was answered but its owed work stayed open — the "
                         "loop would page the operator about finished work forever")
        self.assertIn("RECOVERED", self.pages[-1])

    def test_foreign_owed_work_is_never_auto_closed(self):
        """Adopters record their own promises in the same registry ('NEXT STEP: live
        leg 1'); only message-routed items may be closed by the journal's say-so."""
        self.owed.owe("g12", "manually promised work")
        sched = self.scheduler(QueueAdapter())
        sched.cycle()
        self.assertEqual(self.open_ids(), {"g12"})

    def test_an_edit_after_response_reopens_the_ask(self):
        """R23's harm, closed end to end: we answered version 1, the sender edited the
        message into a different ask — the owed work must come back and STAY back."""
        adapter = QueueAdapter([
            ([msg("C1", "1.0", "where is the dashboard?")], "1.0"),
            ([], "1.0"),
            ([msg("C1", "1.0", "actually, can you please deploy it?")], "1.0"),
            ([], "1.0"),
        ])
        sched = self.scheduler(adapter)
        sched.cycle()
        self.journal.mark_responded("C1", "1.0", "outbox-key-1")
        sched.cycle()
        self.assertEqual(self.open_ids(), set())
        sched.cycle()                  # the edit arrives
        self.assertEqual(self.open_ids(), {"msg@C1@1.0"},
                         "an edit that changes an answered ask never re-surfaced — the "
                         "earlier answer now answers the wrong question")
        sched.cycle()                  # and _close_answered must not swallow it again
        self.assertEqual(self.open_ids(), {"msg@C1@1.0"})


class AmbiguityRoutingTest(SchedulerHarness):
    """ENH-9: the classifier's hedge (exec verb, no directive -> STATEMENT) used to be
    routed as 'logged' with nothing downstream able to tell — safe, but lossy and
    invisible. Now the signal travels: the journal row records it (so the loss is
    countable whether or not anyone acts), and a source that OPTS IN routes each hedge
    to a human (owed:operator) instead of only logging it. Opt-in, not default: every
    hedge paged to the operator is noise for teams whose channels narrate constantly,
    and the safe direction is unchanged behaviour plus a visible count."""

    HEDGE = "The build and test cycle takes an hour."

    def route_one(self, text, escalate_ambiguous=False, ts="1.0"):
        self.scheduler(QueueAdapter([([msg("C1", ts, text)], ts)]),
                       escalate_ambiguous=escalate_ambiguous).cycle()
        return self.journal.get("C1", ts)

    def open_ids(self):
        return {r["id"] for r in self.owed.open_items()}

    def test_route_sends_an_ambiguous_decision_to_a_human_when_opted_in(self):
        self.assertEqual(route("STATEMENT", ambiguous=True, escalate_ambiguous=True),
                         "owed:operator")

    def test_escalation_is_opt_in_the_default_stays_logged(self):
        self.assertEqual(route("STATEMENT", ambiguous=True), "logged")

    def test_a_confident_decision_is_never_escalated_by_the_flag(self):
        self.assertEqual(route("STATEMENT", escalate_ambiguous=True), "logged")

    def test_an_opted_in_loop_owes_the_hedge_to_the_operator(self):
        row = self.route_one(self.HEDGE, escalate_ambiguous=True)
        self.assertEqual(row["kind"], "STATEMENT")
        self.assertEqual(row["routed"], "owed:operator")
        self.assertEqual(row["ambiguous"], 1)
        self.assertEqual(self.open_ids(), {"msg@C1@1.0"},
                         "the opted-in hedge created no owed work — it stays exactly "
                         "as invisible as before the flag existed")

    def test_the_default_loop_still_counts_what_it_declines_to_escalate(self):
        """The count is NOT gated by the routing flag: visibility is the point of the
        signal, and a count that only accrues for opted-in sources hides the loss from
        precisely the teams that have not decided yet."""
        row = self.route_one(self.HEDGE)
        self.assertEqual(row["routed"], "logged")
        self.assertEqual(self.open_ids(), set())
        self.assertEqual(row["ambiguous"], 1,
                         "the hedge was not recorded — with the flag off, ambiguity "
                         "is silently a STATEMENT again")
        self.assertEqual(self.journal.ambiguity_stats()["ambiguous"], 1)

    def test_a_confident_statement_is_untouched_by_the_opt_in(self):
        # No exec verb anywhere — "the deploy finished" would itself be a hedge
        # (noun-shaped "deploy" still matches the verb list; that IS the ambiguity).
        row = self.route_one("notes from the meeting are in the shared doc",
                             escalate_ambiguous=True)
        self.assertEqual(row["routed"], "logged")
        self.assertEqual(row["ambiguous"], 0)
        self.assertEqual(self.open_ids(), set(),
                         "a confident STATEMENT paged the operator — the flag must "
                         "escalate hedges, not every remark")


class RunLoopTest(SchedulerHarness):
    def test_run_fires_the_requested_number_of_cycles(self):
        sched = self.scheduler(QueueAdapter([([msg("C1", "1.0", "hi")], "1.0")]))
        self.assertEqual(sched.run(max_cycles=3), 3)
        self.assertEqual(self.journal.row_count(), 1)

    def test_run_sleeps_out_the_backoff_between_fires(self):
        sched = self.scheduler(QueueAdapter())
        sched.run(max_cycles=2)
        self.assertGreaterEqual(sum(self.slept), 2 * self.BASE,
                                "two idle cycles fired back to back — the loop is a "
                                "hot spin, not a cadence")

    def test_stop_ends_the_loop(self):
        sched = self.scheduler(QueueAdapter())
        self.assertEqual(sched.run(stop=lambda: True), 0)

    def test_run_reports_each_cycle(self):
        """The on_cycle hook is how the runnable script narrates — a loop that runs
        silently for hours is unobservable, which is its own incident class."""
        seen = []
        sched = self.scheduler(QueueAdapter([([msg("C1", "1.0", "hi")], "1.0")]))
        sched.on_cycle = seen.append
        sched.run(max_cycles=2)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0]["fresh"], 1)
        self.assertEqual(seen[1]["fresh"], 0)


class ReferenceSchedulerScriptTest(unittest.TestCase):
    """The runnable wiring (scripts/scheduler.py), driven exactly as an adopter would:
    a temp base with the shipped fake adapter and a settings.json — the tree quickstart
    steps 2-3 leave behind."""

    ROOT = Path(__file__).resolve().parent.parent
    SCRIPT = ROOT / "scripts" / "scheduler.py"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        shutil.copytree(self.ROOT / "channels" / "fake",
                        self.base / "channels" / "fake",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (self.base / "settings.json").write_text(
            '{"engine": {"state_dir": "state"},\n'
            ' "instances": [{"name": "loop-dry-run", "adapter": "fake",\n'
            '   "channels": [{"id": "C_DEMO", "label": "demo"}]}]}\n')

    def tearDown(self):
        self.tmp.cleanup()

    def run_scheduler(self, *flags):
        return subprocess.run(
            [sys.executable, str(self.SCRIPT),
             "--config", str(self.base / "settings.json"), *flags],
            capture_output=True, text=True, env={"PATH": os.environ["PATH"]},
            timeout=60)

    def test_one_guarded_cycle_wires_the_whole_pipeline(self):
        r = self.run_scheduler("--once", "--seed-demo")
        self.assertEqual(r.returncode, 0, f"scheduler failed:\n{r.stderr[-600:]}")
        self.assertIn("SCHEDULER DONE", r.stdout)
        j = Journal(self.base / "state" / "journal.db")
        try:
            row = j.get("C_DEMO", "1.0")
            self.assertIsNotNone(row, "the demo message never reached the journal")
            self.assertTrue(row["routed"],
                            "the journal row records no destination — route is the "
                            "step this script exists to demonstrate")
        finally:
            j.close()
        owed = OwedRegistry(self.base / "state" / "owed.db")
        try:
            self.assertEqual(len(owed.open_items()), 1,
                             "the routed demo ask created no owed work — the "
                             "goal-triggered edge has nothing to fire on")
        finally:
            owed.close_db()
        self.assertIn("OPERATOR:", r.stdout,
                      "unattended owed work raised no operator escalation")
        self.assertIn("DEGRADED", r.stdout)

    def test_escalate_ambiguous_reaches_the_loop_from_settings(self):
        """The config key must survive the scripts/scheduler.py wiring (the ENH-7
        adapter-wiring lesson: a flag parsed but not passed to Source is silently
        inert). The taxonomy strips every directive/ask cue so the shipped demo text
        ('Please review ...') becomes the hedge shape: an exec verb with nothing that
        makes it an instruction."""
        (self.base / "settings.json").write_text(
            '{"engine": {"state_dir": "state"},\n'
            ' "instances": [{"name": "loop-dry-run", "adapter": "fake",\n'
            '   "escalate_ambiguous": true,\n'
            '   "taxonomy": {"exec_verbs": ["review"], "ask_phrases": [],\n'
            '                "directive_markers": [], "commitment_phrases": []},\n'
            '   "channels": [{"id": "C_DEMO", "label": "demo"}]}]}\n')
        r = self.run_scheduler("--once", "--seed-demo")
        self.assertEqual(r.returncode, 0, f"scheduler failed:\n{r.stderr[-600:]}")
        j = Journal(self.base / "state" / "journal.db")
        try:
            row = j.get("C_DEMO", "1.0")
            self.assertIsNotNone(row, "the demo message never reached the journal")
            self.assertEqual(row["ambiguous"], 1,
                             "the stripped-taxonomy demo message did not classify as "
                             "a hedge — this test is measuring nothing")
            self.assertEqual(row["routed"], "owed:operator",
                             "escalate_ambiguous was set in settings.json but the "
                             "hedge was only logged — the flag died in the wiring")
        finally:
            j.close()

    def test_a_second_instance_is_refused_with_a_distinct_exit_code(self):
        (self.base / "state").mkdir()
        holder = SingleInstanceGuard(self.base / "state" / "scheduler.lock").acquire()
        try:
            r = self.run_scheduler("--once", "--seed-demo")
        finally:
            holder.release()
        self.assertEqual(r.returncode, 3,
                         f"a second scheduler did not refuse (stdout: {r.stdout[-200:]})")
        self.assertIn("REFUSED", r.stderr)
        self.assertFalse((self.base / "state" / "journal.db").exists() and
                         Journal(self.base / "state" / "journal.db").row_count(),
                         "the refused instance journaled anyway — it ran a cycle")

    def test_a_broken_config_is_refused_as_documented(self):
        r = subprocess.run(
            [sys.executable, str(self.SCRIPT), "--config",
             str(self.base / "no-such-settings.json")],
            capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
        self.assertEqual(r.returncode, 2)
        self.assertIn("REFUSED", r.stderr)

    def test_the_scheduler_has_no_send_path(self):
        """The loop is long-lived and unattended — exactly where an accidental send
        would live longest. Same construction as first-poll.py: the send layer is
        never even imported."""
        import ast
        tree = ast.parse(self.SCRIPT.read_text())
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        self.assertFalse([m for m in imported if "outbox" in m],
                         "scheduler.py imports the send layer — the reference loop "
                         "must be read-only by construction")



class HistoryOnlyFake:
    """The shipped fake adapter with Slack's shape imposed on poll(): history serves
    TOP-LEVEL messages only, so a thread reply can reach the engine through replies()
    alone — exactly the gap ENH-30 closes."""

    def __init__(self):
        from core.config import load_adapter_class
        root = Path(__file__).resolve().parent.parent
        self.inner = load_adapter_class(root / "channels", "fake")()
        self.explode_on = None          # a platform error for this thread
        self.dead = set()               # roots the platform refuses (deleted asks)
        self.rate_limit_after = None    # RateLimited after N replies() calls
        self.calls = 0
        self.roots_swept = set()
        self.sweep_log = []             # (channel, root) in call order

    def seed(self, messages):
        self.inner.seed(messages)

    def poll(self, cursor):
        msgs, new_cursor = self.inner.poll(cursor)
        return [m for m in msgs if not m.get("thread_id")], new_cursor

    def replies(self, channel_id, thread_ts, oldest=None):
        self.calls += 1
        self.roots_swept.add(thread_ts)
        self.sweep_log.append((channel_id, thread_ts))
        if self.rate_limit_after is not None and self.calls > self.rate_limit_after:
            from core.ratelimit import RateLimited
            raise RateLimited("5", method="conversations.replies")
        if thread_ts in self.dead or (
                self.explode_on is not None and thread_ts == self.explode_on):
            raise RuntimeError("conversations.replies failed: thread_not_found")
        return self.inner.replies(channel_id, thread_ts, oldest=oldest)


class ThreadSweepCycleTest(SchedulerHarness):
    """Each cycle sweeps the open asks' threads (ENH-30): an in-thread reply from
    someone else marks the ask answered, closes its owed work, and is itself
    journaled like any other message."""

    def test_an_in_thread_reply_from_someone_else_closes_the_ask(self):
        adapter = HistoryOnlyFake()
        adapter.seed([msg("C1", "1.0", "where is the dashboard?")])
        sched = self.scheduler(adapter)
        first = sched.cycle()
        self.assertEqual({r["id"] for r in self.owed.open_items()}, {"msg@C1@1.0"})
        self.assertEqual(first["answered"], 0)
        adapter.seed([msg("C1", "2.0", "http://127.0.0.1:8502", sender_id="B_AGENT",
                          thread_id="1.0")])
        second = sched.cycle()
        self.assertEqual(second["answered"], 1, "the cycle summary must count answers")
        row = self.journal.get("C1", "1.0")
        self.assertIsNotNone(row["responded_at"],
                             "an in-thread answer left the ask UNANSWERED")
        self.assertEqual(row["response_key"], "thread-reply:2.0")
        self.assertEqual({r["id"] for r in self.owed.open_items()}, set(),
                         "the owed work stayed open after its ask was answered")
        self.assertIsNotNone(self.journal.get("C1", "2.0"),
                             "the reply itself was not journaled — a reply is a "
                             "message, and an unlisted message is the bug the operator reported")

    def test_the_askers_own_bump_leaves_the_ask_open(self):
        adapter = HistoryOnlyFake()
        adapter.seed([msg("C1", "1.0", "where is the dashboard?")])
        sched = self.scheduler(adapter)
        sched.cycle()
        # A STATEMENT bump: a question-shaped bump ("anyone?") would rightly become an
        # owed ask of its own — that is the first test's domain, not this one's.
        adapter.seed([msg("C1", "2.0", "still waiting on this one", thread_id="1.0")])
        self.assertEqual(sched.cycle()["answered"], 0)
        self.assertIsNone(self.journal.get("C1", "1.0")["responded_at"])
        self.assertEqual({r["id"] for r in self.owed.open_items()}, {"msg@C1@1.0"})

    def test_a_dead_thread_is_reported_and_never_stops_the_cycle(self):
        """One deleted ask (conversations.replies -> thread_not_found, forever) must not
        become a crash loop that also leaves every later channel unpolled."""
        adapter = HistoryOnlyFake()
        adapter.seed([msg("C1", "1.0", "where is the dashboard?"),
                      msg("C2", "5.0", "status: all green")])
        adapter.explode_on = "1.0"
        sched = self.scheduler(adapter, channels=("C1", "C2"))
        summary = sched.cycle()
        self.assertEqual(summary["polled"], 2, "C2 was never polled after C1's dead thread")
        self.assertEqual([(c, ts) for c, ts, _ in summary["sweep_errors"]], [("C1", "1.0")],
                         "the dead thread must be named in the cycle summary")
        self.assertIn("thread_not_found", summary["sweep_errors"][0][2])
        self.assertIsNone(self.journal.get("C1", "1.0")["responded_at"])

    def test_sweep_rate_limits_and_truncation_reach_the_cycle_summary(self):
        adapter = HistoryOnlyFake()
        adapter.seed([msg("C1", f"{i}.0", f"question {i}?") for i in range(1, 4)])
        adapter.rate_limit_after = 1
        summary = self.scheduler(adapter).cycle()
        self.assertEqual(summary["sweep_rate_limited"], [("C1", "conversations.replies")],
                         "a replies 429 printed the same cycle line as a healthy sweep")
        adapter2 = HistoryOnlyFake()
        adapter2.seed([msg("C1", f"{i}.0", f"question {i}?") for i in range(1, 30)])
        summary2 = self.scheduler(adapter2).cycle()
        self.assertEqual(summary2["sweep_truncated"], ["C1"],
                         "a sweep that left open asks unswept said nothing")

    def test_the_sweep_budget_is_per_source_per_cycle(self):
        """25 calls per CHANNEL per cycle exceeds Slack's tier at two channels; the budget
        is one adapter's, spent across its channels (review F8)."""
        from core.threads import SWEEP_MAX
        adapter = HistoryOnlyFake()
        adapter.seed([msg("C1", f"{i}.0", f"question {i}?") for i in range(1, 21)]
                     + [msg("C2", f"{i}.0", f"question {i}?") for i in range(1, 21)])
        self.scheduler(adapter, channels=("C1", "C2")).cycle()
        self.assertEqual(adapter.calls, SWEEP_MAX,
                         f"one cycle issued {adapter.calls} replies() calls for one adapter")

    def test_a_429_stops_the_rest_of_that_sources_sweeps_this_cycle(self):
        adapter = HistoryOnlyFake()
        adapter.seed([msg("C1", "1.0", "q1?"), msg("C1", "2.0", "q2?"),
                      msg("C2", "3.0", "q3?")])
        adapter.rate_limit_after = 1
        sched = self.scheduler(adapter, channels=("C1", "C2"))
        summary = sched.cycle()
        self.assertEqual(adapter.calls, 2, "after a 429 the same method was called again "
                                           "in the same cycle")
        self.assertEqual(summary["polled"], 3, "polling must go on after a sweep 429")
        self.assertEqual(summary["sweep_skipped"], [("C2", "rate-limited")],
                         "the channel the 429 left unswept was not named, with its cause")
        adapter.rate_limit_after = None
        sched.cycle()
        self.assertEqual(adapter.sweep_log[2][0], "C2",
                         "the channel skipped last cycle must be swept FIRST this cycle")

    def test_older_asks_are_swept_on_later_cycles(self):
        from core.threads import SWEEP_MAX
        adapter = HistoryOnlyFake()
        adapter.seed([msg("C1", f"{i}.0", f"question {i}?") for i in range(1, 2 * SWEEP_MAX + 1)])
        sched = self.scheduler(adapter)
        for _ in range(3):
            sched.cycle()
        self.assertEqual(adapter.roots_swept, {f"{i}.0" for i in range(1, 2 * SWEEP_MAX + 1)},
                         "asks beyond the newest window were never swept — starvation")

    def test_a_follow_up_question_in_the_thread_does_not_close_the_ask(self):
        adapter = HistoryOnlyFake()
        adapter.seed([msg("C1", "1.0", "where is the dashboard?")])
        sched = self.scheduler(adapter)
        sched.cycle()
        adapter.seed([msg("C1", "2.0", "and where are the logs?", sender_id="U_B",
                          thread_id="1.0")])
        self.assertEqual(sched.cycle()["answered"], 0,
                         "a follow-up QUESTION closed the original ask")
        self.assertEqual({r["id"] for r in self.owed.open_items()},
                         {"msg@C1@1.0", "msg@C1@2.0"},
                         "both questions are owed; the follow-up is an ask of its own")

    def _answered_thread(self, adapter):
        """Cycle 1 polls the ask; cycle 2 sweeps the in-thread answer that closes it."""
        adapter.seed([msg("C1", "1.0", "where is the dashboard?")])
        sched = self.scheduler(adapter)
        sched.cycle()
        adapter.seed([msg("C1", "2.0", "http://127.0.0.1:8502", sender_id="B_AGENT",
                          thread_id="1.0")])
        self.assertEqual(sched.cycle()["answered"], 1)
        self.assertEqual({r["id"] for r in self.owed.open_items()}, set())
        return sched

    def test_a_follow_up_question_under_an_answered_ask_is_listed_and_owed(self):
        """Issue #8: once its ask was answered a thread was never swept again, so the
        asker's follow-up under the answer (often a new question) was never ingested,
        listed or owed. Within FOLLOWUP_WINDOW_S of the answer it is, and the answered
        ask keeps the reply that answered it."""
        adapter = HistoryOnlyFake()
        sched = self._answered_thread(adapter)
        adapter.seed([msg("C1", "3.0", "and where are the logs?", thread_id="1.0")])
        sched.cycle()
        self.assertIsNotNone(self.journal.get("C1", "3.0"),
                             "a follow-up under an answered ask was never listed")
        self.assertEqual({r["id"] for r in self.owed.open_items()}, {"msg@C1@3.0"},
                         "the follow-up question is an ask of its own and must be owed")
        self.assertEqual(self.journal.get("C1", "1.0")["response_key"], "thread-reply:2.0",
                         "the answered ask was re-tied")

    def test_a_follow_up_statement_under_an_answered_ask_is_listed_and_owes_nothing(self):
        adapter = HistoryOnlyFake()
        sched = self._answered_thread(adapter)
        adapter.seed([msg("C1", "3.0", "still waiting on this one", thread_id="1.0")])
        sched.cycle()
        self.assertIsNotNone(self.journal.get("C1", "3.0"), "the follow-up was not listed")
        self.assertEqual({r["id"] for r in self.owed.open_items()}, set(),
                         "a follow-up statement owes nobody an answer")
        self.assertEqual(self.journal.get("C1", "1.0")["response_key"], "thread-reply:2.0")

    def test_open_asks_are_swept_before_any_follow_up(self):
        """Lowest priority: a follow-up sweep never takes a call the open asks needed."""
        from core.threads import SWEEP_MAX
        adapter = HistoryOnlyFake()
        sched = self._answered_thread(adapter)
        adapter.seed([msg("C1", f"{i}.0", f"question {i}?")
                      for i in range(10, 10 + SWEEP_MAX)])
        adapter.sweep_log.clear()
        sched.cycle()
        self.assertEqual(len(adapter.sweep_log), SWEEP_MAX)
        self.assertNotIn(("C1", "1.0"), adapter.sweep_log,
                         "a follow-up sweep spent a call while open asks waited")

    def test_follow_up_sweeps_share_the_one_budget(self):
        """The follow-up sweep spends the same per-adapter budget, charged by attempt: a
        cycle with more recently answered threads than SWEEP_MAX issues SWEEP_MAX calls."""
        from core.threads import SWEEP_MAX
        adapter = HistoryOnlyFake()
        n = SWEEP_MAX + 3
        adapter.seed([msg(ch, f"{i}.0", f"question {i}?")
                      for ch in ("C1", "C2") for i in range(1, n + 1)])
        sched = self.scheduler(adapter, channels=("C1", "C2"))
        sched.cycle()
        for ch in ("C1", "C2"):
            for i in range(1, n + 1):
                self.journal.mark_responded(ch, f"{i}.0", f"outbox-{ch}-{i}")
        adapter.calls = 0
        summary = sched.cycle()
        self.assertEqual(adapter.calls, SWEEP_MAX,
                         f"one cycle issued {adapter.calls} replies() calls for one adapter")
        self.assertIn("C1", summary["followup_truncated"],
                      "a bounded follow-up sweep must say it left work")

    def test_an_adapter_without_replies_leaves_the_cycle_whole(self):
        adapter = QueueAdapter([([msg("C1", "1.0", "where is it?")], "1.0")])
        summary = self.scheduler(adapter).cycle()
        self.assertEqual(summary["answered"], 0)
        self.assertEqual(summary["fresh"], 1)

    def test_a_backlog_in_the_first_channel_does_not_starve_the_second(self):
        """Review D2: the source's one budget was spent in CONFIG order, so a first
        channel with 25 open roots (a normal support backlog) left every later channel
        unswept, every cycle, with nothing in the summary — the ENH-30 symptom itself
        for channel 2 onward. One call is now reserved for every channel still
        waiting: the backlog channel takes the bulk, never the whole budget."""
        from core.threads import SWEEP_MAX
        adapter = HistoryOnlyFake()
        adapter.seed([msg("C1", f"{i}.0", f"can you check item {i}?")
                      for i in range(1, SWEEP_MAX + 2)]
                     + [msg("C2", "100.0", "where is the dashboard?")])
        sched = self.scheduler(adapter, channels=("C1", "C2"))
        first = sched.cycle()
        self.assertEqual(first["sweep_skipped"], [],
                         "a channel went unswept although a call could be reserved for it")
        adapter.seed([msg("C2", "101.0", "here: http://127.0.0.1:8502",
                          sender_id="B_AGENT", thread_id="100.0")])
        summary = sched.cycle()
        self.assertEqual(summary["sweep_skipped"], [])
        self.assertIsNotNone(self.journal.get("C2", "100.0")["responded_at"],
                             "C2's in-thread answer never closed its ask: the first "
                             "channel's backlog ate the whole budget")
        self.assertEqual(summary["sweep_truncated"], ["C1"],
                         "the backlog channel still takes the bulk, and still says it is "
                         "not finished")

    def test_two_backlog_channels_take_turns_and_a_small_third_is_swept_every_cycle(self):
        """The reservation alone would hand ONE backlog channel the bulk forever (23 of
        25 calls, the other getting 1): the channel swept first moves to the back of
        the order, so the bulk alternates — and a small channel behind both backlogs
        is still swept every cycle."""
        adapter = HistoryOnlyFake()
        adapter.seed([msg("C1", f"{i}.0", f"item {i}?") for i in range(1, 31)]
                     + [msg("C2", f"{i}.0", f"ticket {i}?") for i in range(101, 131)]
                     + [msg("C3", "500.0", "where is the dashboard?")])
        sched = self.scheduler(adapter, channels=("C1", "C2", "C3"))
        sched.cycle()
        adapter.seed([msg("C3", "501.0", "here: http://127.0.0.1:8502",
                          sender_id="B_AGENT", thread_id="500.0")])
        second = sched.cycle()
        self.assertIsNotNone(self.journal.get("C3", "500.0")["responded_at"],
                             "the small channel behind two backlogs was never swept")
        self.assertEqual(second["sweep_skipped"], [])
        sched.cycle()
        c1 = {r for r in adapter.roots_swept if float(r) < 100}
        c2 = {r for r in adapter.roots_swept if 100 < float(r) < 500}
        self.assertGreaterEqual(len(c1), 20, f"C1 never had the bulk: {len(c1)} roots")
        self.assertGreaterEqual(len(c2), 20, f"C2 never had the bulk: {len(c2)} roots")

    def test_the_bulk_alternates_even_with_a_small_channel_in_the_rotation(self):
        """Rotating position 0 let a small channel's turn hand the bulk straight back to
        the same backlog channel (A 94 calls vs B 50 over 6 cycles, review F3). The
        channel that SPENT the most goes to the back, so two backlogs share the bulk."""
        from collections import Counter
        adapter = HistoryOnlyFake()
        adapter.seed([msg("A", f"{i}.0", f"item {i}?") for i in range(1, 41)]
                     + [msg("B", f"{i}.0", f"ticket {i}?") for i in range(101, 141)]
                     + [msg("S", "501.0", "where is the dashboard?")])
        sched = self.scheduler(adapter, channels=("A", "B", "S"))
        for _ in range(6):
            sched.cycle()
        calls = Counter(c for c, _ in adapter.sweep_log)
        self.assertLessEqual(abs(calls["A"] - calls["B"]), 2,
                             f"the bulk is not shared: A={calls['A']} B={calls['B']}")
        self.assertEqual(calls["S"], 6, "the small channel was not swept every cycle")

    def test_the_sweep_window_and_order_survive_a_restart_the_cron_deployment(self):
        """`scheduler.py --once` (the documented cron mode) builds a NEW Scheduler per
        run over the same state. An in-memory window restarted at the newest asks on
        every run: a backlog channel took the bulk, the one call reserved for the next
        channel always went to its NEWEST ask, and the asks behind it were never swept
        (independent review F1). The window and the order live in the store now."""
        adapter = HistoryOnlyFake()
        adapter.seed([msg("A", f"{i}.0", f"item {i}?") for i in range(1, 31)]
                     + [msg("B", f"{i}.0", f"ticket {i}?") for i in range(101, 106)])
        self.scheduler(adapter, channels=("A", "B")).cycle()            # run 1
        adapter.seed([msg("B", "200.0", "done, see the runbook", sender_id="B_AGENT",
                          thread_id="101.0")])                           # B's OLDEST ask
        for _ in range(6):                                               # six more runs
            self.scheduler(adapter, channels=("A", "B")).cycle()
        self.assertIsNotNone(self.journal.get("B", "101.0")["responded_at"],
                             "B's oldest ask was never swept across runs: the window "
                             "restarted at B's newest ask every run")

    def test_more_channels_than_calls_the_tail_is_swept_next_run_even_across_a_restart(self):
        """27 channels, one open ask each, 25 calls: the last two cannot be reached in one
        cycle. They are named SKIPPED (budget), swept FIRST next cycle — also when that
        cycle is a new process (durable order) — and every ask is swept within two
        cycles, so the floor of one call per channel is load-bearing (review G3/G4/G7)."""
        from core.threads import SWEEP_MAX
        chans = tuple(f"K{i:02d}" for i in range(SWEEP_MAX + 2))
        adapter = HistoryOnlyFake()
        adapter.seed([msg(c, f"{1000 * (i + 1)}.0", f"item {i}?")
                      for i, c in enumerate(chans)])
        first = self.scheduler(adapter, channels=chans).cycle()
        self.assertEqual(first["sweep_skipped"],
                         [(chans[-2], "budget"), (chans[-1], "budget")])
        adapter.seed([msg(c, f"{1000 * (i + 1) + 1}.0", "answered", sender_id="B_AGENT",
                          thread_id=f"{1000 * (i + 1)}.0") for i, c in enumerate(chans)])
        before = len(adapter.sweep_log)
        self.scheduler(adapter, channels=chans).cycle()                  # a new process
        self.assertEqual([c for c, _ in adapter.sweep_log[before:before + 2]],
                         [chans[-2], chans[-1]],
                         "the channels skipped last run were not swept first this run")
        self.scheduler(adapter, channels=chans).cycle()
        still_open = [c for i, c in enumerate(chans)
                      if self.journal.get(c, f"{1000 * (i + 1)}.0")["responded_at"] is None]
        self.assertEqual(still_open, [], "asks left unswept after two full cycles")

    def test_errored_thread_fetches_are_charged_against_the_budget(self):
        """A refused fetch (a deleted ask: thread_not_found, forever) costs the platform
        a call like any other. Charged nothing, an all-errors channel let the next one
        spend a second full budget: 49 calls on one adapter in one cycle (review F2)."""
        from core.threads import SWEEP_MAX
        adapter = HistoryOnlyFake()
        adapter.seed([msg("A", f"{i}.0", f"item {i}?") for i in range(1, 31)]
                     + [msg("B", f"{i}.0", f"ticket {i}?") for i in range(101, 131)])
        adapter.dead = {f"{i}.0" for i in range(1, 31)}
        summary = self.scheduler(adapter, channels=("A", "B")).cycle()
        self.assertLessEqual(adapter.calls, SWEEP_MAX,
                             f"{adapter.calls} replies() calls in one cycle on one adapter")
        self.assertTrue(summary["sweep_errors"], "the refused fetches were not reported")

    def test_sweep_reports_every_attempted_call(self):
        from core import threads
        adapter = HistoryOnlyFake()
        adapter.seed([msg("C1", "1.0", "q1?"), msg("C1", "2.0", "q2?"),
                      msg("C1", "3.0", "q3?")])
        self.scheduler(adapter).cycle()                 # journals the asks
        adapter.dead = {"2.0"}
        res = threads.sweep(adapter, self.store, self.journal, "C1")
        self.assertEqual((res["swept"], res["calls"]), (2, 3),
                         "a refused fetch was not counted as the call it was")

    def test_a_429_mid_batch_does_not_advance_the_window_past_the_unfetched_asks(self):
        """Review R2-1: next_start was computed before the fetch loop, so a 429 after the
        first fetch left the window past every ask the batch never reached — under a
        tier of one call per cycle the same ask was fetched forever and its siblings'
        in-thread answers never closed them. The window advances as far as the loop
        got, so every ask is reached in turn."""
        from core import threads
        adapter = HistoryOnlyFake()
        adapter.seed([msg("C1", f"{i}.0", f"q{i}?") for i in range(1, 6)])
        self.scheduler(adapter).cycle()                     # journals the five asks
        adapter.sweep_log = []
        for _ in range(5):
            adapter.calls, adapter.rate_limit_after = 0, 1  # one call per sweep, then 429
            res = threads.sweep(adapter, self.store, self.journal, "C1",
                                start=self.store.sweep_start_get("inst", "C1"))
            self.store.sweep_start_set("inst", "C1", res["next_start"])
        fetched = {root for _, root in adapter.sweep_log}
        self.assertEqual(fetched, {f"{i}.0" for i in range(1, 6)},
                         f"with one call per cycle only {sorted(fetched)} were ever fetched")

    def test_the_window_commits_only_after_the_fetched_replies_are_journaled(self):
        """The poll path's rule, on the sweep: the cursor commits strictly AFTER every
        fetched message has a journal row. Durable since round 2, the window used to be
        written first, so a crash in journaling left the fetched replies in the store,
        unjournaled and owed to nobody, until the window wrapped around (review R2-2)."""
        from core.threads import SWEEP_MAX
        adapter = HistoryOnlyFake()
        adapter.seed([msg("C1", f"{i}.0", f"q{i}?") for i in range(1, 31)])
        sched = self.scheduler(adapter)
        sched.cycle()                                         # window now at SWEEP_MAX
        before = self.store.sweep_start_get("inst", "C1")
        self.assertEqual(before, SWEEP_MAX)
        adapter.seed([msg("C1", "100.0", "and where are the logs?", sender_id="U_B",
                          thread_id="1.0")])                  # under a root of the next batch
        real = sched._journal_and_route

        def crashing(channel, messages, src):
            if any(m.get("thread_id") for m in messages):
                raise RuntimeError("database is locked")
            return real(channel, messages, src)
        sched._journal_and_route = crashing
        with self.assertRaises(RuntimeError):
            sched.cycle()
        self.assertEqual(self.store.sweep_start_get("inst", "C1"), before,
                         "the window moved past a reply that never reached the journal")

    def test_a_config_edit_between_runs_adds_the_new_channel_and_drops_the_removed_one(self):
        """The order is durable now, so editing settings.json between `--once` runs is a
        routine path (ENH-29 makes it a click). A channel added to the config must join
        the saved order — or its in-thread answers never close its asks, silently — and
        a channel removed from it must leave — or the engine keeps calling the platform
        for a channel it was told to stop watching (review R2-8)."""
        adapter = HistoryOnlyFake()
        adapter.seed([msg("A", f"{i}.0", f"item {i}?") for i in range(1, 31)]
                     + [msg("B", f"{i}.0", f"ticket {i}?") for i in range(101, 131)]
                     + [msg("N", "500.0", "where is the dashboard?")])
        self.scheduler(adapter, channels=("A", "B")).cycle()        # run 1 watched A, B
        self.assertEqual(self.store.sweep_order_get("inst"), ["B", "A"])
        before = len(adapter.sweep_log)
        for _ in range(3):                                           # settings.json edited
            self.scheduler(adapter, channels=("B", "N")).cycle()
        swept = {c for c, _ in adapter.sweep_log[before:]}
        self.assertIn("N", swept, "a channel added to the config was never swept")
        self.assertNotIn("A", swept, "a channel removed from the config was still swept")
        self.assertEqual(set(self.store.sweep_order_get("inst")), {"B", "N"})

    def test_a_channel_with_nothing_to_sweep_is_not_named_skipped(self):
        """SKIPPED names a channel left behind. One with no open ask owes no sweep, so a
        429 that stops the sweep before it is not its story (review F4)."""
        adapter = HistoryOnlyFake()
        adapter.seed([msg("C1", "1.0", "q1?"), msg("C1", "2.0", "q2?"),
                      msg("C2", "3.0", "status: all green")])
        adapter.rate_limit_after = 1
        summary = self.scheduler(adapter, channels=("C1", "C2")).cycle()
        self.assertEqual(summary["sweep_rate_limited"], [("C1", "conversations.replies")])
        self.assertEqual(summary["sweep_skipped"], [],
                         "a channel with no open ask was named SKIPPED")


class ReportLinesTest(unittest.TestCase):
    """scripts/scheduler.py's cycle report: every sweep refusal has its own line, with a
    TRUE cause — a channel skipped after a 429 was not 'out of budget' (review F4)."""

    def lines(self, **over):
        import importlib.util
        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "scheduler_script", root / "scripts" / "scheduler.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        summary = {"polled": 3, "fresh": 1, "unattended": 0, "next_interval": 60.0,
                   "answered": 0, "sweep_rate_limited": [], "sweep_truncated": [],
                   "sweep_errors": [], "sweep_skipped": []}
        summary.update(over)
        return mod.report_lines(summary)

    def test_a_skipped_channel_gets_a_line_with_its_cause(self):
        lines = self.lines(sweep_rate_limited=[("C1", "conversations.replies")],
                           sweep_skipped=[("C2", "rate-limited"), ("C3", "budget")])
        skipped = [ln for ln in lines if ln.startswith("sweep: SKIPPED")]
        self.assertEqual(len(skipped), 2, lines)
        self.assertIn("channel=C2", skipped[0])
        self.assertIn("rate-limited", skipped[0])
        self.assertNotIn("budget", skipped[0], "a 429 skip was blamed on the budget")
        self.assertIn("channel=C3", skipped[1])
        self.assertIn("budget was spent", skipped[1])

    def test_follow_up_refusals_get_their_own_lines(self):
        lines = self.lines(followup_rate_limited=[("C1", "conversations.replies")],
                           followup_truncated=["C2"])
        self.assertTrue(any(ln.startswith("followup-sweep: RATE-LIMITED channel=C1")
                            for ln in lines), lines)
        self.assertTrue(any(ln.startswith("followup-sweep: TRUNCATED channel=C2")
                            for ln in lines), lines)

    def test_a_healthy_cycle_prints_the_cycle_line_only(self):
        lines = self.lines()
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith("cycle: polled=3 fresh=1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
