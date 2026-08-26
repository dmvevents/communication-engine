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


if __name__ == "__main__":
    unittest.main(verbosity=2)
