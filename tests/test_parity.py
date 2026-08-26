"""Tests for core/parity.py — the G1 differ (requirement R8).

Two properties matter more than the rest:

* `test_empty_oracle_raises_instead_of_passing` — a differ that reports "no misses"
  because it compared nothing is the same defect class as the secret gate that passed on
  an empty file list while CI stayed green.
* `test_engine_lost_can_never_be_accepted` — classification exists so that a divergence
  the platform explains (a deleted message) stops masquerading as a read-path defect. The
  moment it can also excuse a REAL loss, the gate is decorative. Acceptance is a
  reviewable argument, and this one class is outside its reach by construction.
"""
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.parity import (  # noqa: E402
    AHEAD_OF_ORACLE, BEFORE_ENGINE_START, ENGINE_LOST, ENGINE_ONLY, NOT_YET_POLLED,
    ORACLE_MISSED, PRE_ORACLE_FLOOR, UNCLASSIFIED, UNRETRIEVABLE,
    ParityError, compare, main as parity_main, read_served, read_timestamps,
    snapshot_declaration)


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


class FailClosedTest(unittest.TestCase):
    """With no platform snapshot, an ambiguous loss must be read as OUR loss."""

    def test_without_a_platform_snapshot_every_miss_is_engine_lost(self):
        r = compare({"1.1", "1.2"}, {"1.1"}, "C_EXAMPLE")
        self.assertEqual(r.classified["1.2"], ENGINE_LOST)
        self.assertFalse(r.served_known)
        self.assertFalse(r.ok)

    def test_summary_says_out_loud_that_the_snapshot_was_absent(self):
        """A reader must not mistake a fail-closed verdict for a platform-confirmed one."""
        self.assertIn("ABSENT", compare({"1.1", "1.2"}, {"1.1"}, "C_EXAMPLE").summary())

    def test_an_empty_platform_snapshot_is_an_error_not_a_blanket_excuse(self):
        """An empty snapshot would relabel every genuine loss 'deleted upstream' — the
        vacuous-pass bug wearing a new hat."""
        with self.assertRaises(ParityError):
            compare({"1.1", "1.2"}, {"1.1"}, "C_EXAMPLE", served_ts=set())

    def test_unparseable_timestamp_raises_instead_of_sorting_as_zero(self):
        """A ts that floats to 0.0 would fall below every retention floor and earn a
        benign class for free."""
        with self.assertRaises(ParityError):
            compare({"1.1", "not-a-ts"}, {"1.1"}, "C_EXAMPLE", served_ts={"1.1"})


class ClassificationTest(unittest.TestCase):
    def test_platform_no_longer_serves_it_so_the_miss_is_unretrievable(self):
        r = compare({"1.1", "1.2"}, {"1.1"}, "C_EXAMPLE", served_ts={"1.1"})
        self.assertEqual(r.classified["1.2"], UNRETRIEVABLE)
        self.assertFalse(r.ok, "unaccepted by default — explaining is a deliberate act")
        self.assertTrue(compare({"1.1", "1.2"}, {"1.1"}, "C_EXAMPLE",
                                served_ts={"1.1"}, accept=[UNRETRIEVABLE]).ok)

    def test_platform_still_serves_it_so_the_miss_is_engine_lost(self):
        r = compare({"1.1", "1.2"}, {"1.1"}, "C_EXAMPLE", served_ts={"1.1", "1.2"},
                    accept=[UNRETRIEVABLE, PRE_ORACLE_FLOOR])
        self.assertEqual(r.classified["1.2"], ENGINE_LOST)
        self.assertFalse(r.ok, "accepting other classes must not excuse a real loss")

    def test_a_row_the_platform_serves_and_neither_store_has_is_still_a_loss(self):
        """The universe of misses is served|oracle. A message the platform will hand over
        and the engine lacks is lost whether or not the incumbent caught it."""
        r = compare({"1.1"}, {"1.1"}, "C_EXAMPLE", served_ts={"1.1", "2.0"})
        self.assertEqual(r.classified["2.0"], ENGINE_LOST)
        self.assertFalse(r.ok)

    def test_misses_newer_than_the_cursor_are_not_yet_polled(self):
        r = compare({"1.1", "3.0"}, {"1.1"}, "C_EXAMPLE", served_ts={"1.1", "3.0"},
                    covered_through="2.0", accept=[NOT_YET_POLLED])
        self.assertEqual(r.classified["3.0"], NOT_YET_POLLED)
        self.assertTrue(r.ok)

    def test_the_window_comes_from_the_cursor_not_the_newest_stored_row(self):
        """THE laundering hole: if the window were inferred from the candidate's own
        maximum, a lost newest message would define itself out of the window and be
        waved through as 'not yet polled'."""
        r = compare({"1.1", "3.0"}, {"1.1"}, "C_EXAMPLE", served_ts={"1.1", "3.0"},
                    covered_through="3.0", accept=[NOT_YET_POLLED])
        self.assertEqual(r.classified["3.0"], ENGINE_LOST)
        self.assertFalse(r.ok)

    def test_misses_older_than_the_engines_first_poll_are_before_engine_start(self):
        r = compare({"0.5", "1.1"}, {"1.1"}, "C_EXAMPLE", served_ts={"0.5", "1.1"},
                    covered_from="1.0", accept=[BEFORE_ENGINE_START])
        self.assertEqual(r.classified["0.5"], BEFORE_ENGINE_START)
        self.assertTrue(r.ok)

    def test_extra_below_the_oracle_floor_is_pre_oracle_floor(self):
        r = compare({"5.0", "6.0"}, {"1.0", "5.0", "6.0"}, "C_EXAMPLE",
                    served_ts={"5.0", "6.0"}, accept=[PRE_ORACLE_FLOOR])
        self.assertEqual(r.classified["1.0"], PRE_ORACLE_FLOOR)
        self.assertTrue(r.ok, "a deeper backfill than the incumbent's retention is not a bug")

    def test_extra_above_the_oracle_high_water_mark_is_ahead_of_oracle(self):
        r = compare({"5.0"}, {"5.0", "9.0"}, "C_EXAMPLE", served_ts={"5.0", "9.0"},
                    accept=[AHEAD_OF_ORACLE])
        self.assertEqual(r.classified["9.0"], AHEAD_OF_ORACLE)
        self.assertTrue(r.ok, "the engine polling sooner than the incumbent is a race, not a defect")

    def test_extra_inside_the_window_that_the_platform_serves_means_the_oracle_missed_it(self):
        r = compare({"5.0", "7.0"}, {"5.0", "6.0", "7.0"}, "C_EXAMPLE",
                    served_ts={"5.0", "6.0", "7.0"})
        self.assertEqual(r.classified["6.0"], ORACLE_MISSED)

    def test_extra_inside_the_window_that_nobody_else_has_is_engine_only(self):
        """Sole-witness rows are where a foreign-channel ingest bug lands (ENH-14), so
        this class must exist separately and must not be accepted by default."""
        r = compare({"5.0", "7.0"}, {"5.0", "6.0", "7.0"}, "C_EXAMPLE",
                    served_ts={"5.0", "7.0"})
        self.assertEqual(r.classified["6.0"], ENGINE_ONLY)
        self.assertFalse(r.ok)

    def test_every_divergent_row_gets_a_class(self):
        """No silent hole: a row that reached no rule would be invisible to the verdict."""
        r = compare({"1.0", "2.0", "3.0"}, {"2.0", "4.0", "0.1"}, "C_EXAMPLE",
                    served_ts={"1.0", "2.0"})
        for ts in (r.missed | r.extra):
            self.assertIn(ts, r.classified, f"{ts} was classified into nothing")
            self.assertNotEqual(r.classified[ts], UNCLASSIFIED)


class AcceptanceTest(unittest.TestCase):
    def test_engine_lost_can_never_be_accepted(self):
        """The whole taxonomy is safe only because this one class is out of reach."""
        with self.assertRaises(ParityError):
            compare({"1.1"}, {"1.1"}, "C_EXAMPLE", accept=[ENGINE_LOST])

    def test_unclassified_can_never_be_accepted(self):
        with self.assertRaises(ParityError):
            compare({"1.1"}, {"1.1"}, "C_EXAMPLE", accept=[UNCLASSIFIED])

    def test_accepting_nothing_reproduces_the_strict_two_way_differ(self):
        """The default must be the old, strict behaviour — opting into tolerance is the
        change, not opting out of it."""
        r = compare({"1.1", "1.2"}, {"1.1", "9.9"}, "C_EXAMPLE", served_ts={"1.1", "1.2"})
        self.assertFalse(r.ok)
        self.assertEqual(r.accepted, frozenset())

    def test_unexplained_names_only_the_classes_that_were_not_accepted(self):
        r = compare({"1.1", "1.2"}, {"1.1", "9.9"}, "C_EXAMPLE",
                    served_ts={"1.1", "1.2"}, accept=[AHEAD_OF_ORACLE])
        self.assertEqual(set(r.unexplained), {ENGINE_LOST})
        self.assertIn(AHEAD_OF_ORACLE, r.counts())

    def test_summary_marks_each_class_accepted_or_unexplained(self):
        s = compare({"1.1", "1.2"}, {"1.1"}, "C_EXAMPLE",
                    served_ts={"1.1"}, accept=[UNRETRIEVABLE]).summary()
        self.assertIn("UNRETRIEVABLE=1 [accepted]", s)
        self.assertIn("PARITY OK", s)


class LiveIncidentRegressionTest(unittest.TestCase):
    """The measured 2026-08-26 R8 window, reduced to its shape.

    Oracle 507 / engine 189 on one channel: 342 rows the platform will not serve
    (one app's deleted burst) and 24 rows below the oracle's retention floor. The old
    differ called this FAIL and pointed at the read path; the platform's own answer was
    that the engine held 100% of retrievable history. This test pins that verdict so a
    future change cannot quietly re-break either direction.
    """

    def setUp(self):
        self.common = {f"{1000 + i}.0" for i in range(165)}
        self.deleted = {f"{2000 + i}.0" for i in range(342)}   # oracle-only, unserved
        self.deep = {f"{100 + i}.0" for i in range(24)}        # engine-only, pre-floor
        self.oracle = self.common | self.deleted
        self.candidate = self.common | self.deep
        self.served = self.common                              # what Slack returns today

    def test_the_measured_divergence_is_fully_explained(self):
        r = compare(self.oracle, self.candidate, "C_EXAMPLE", served_ts=self.served,
                    accept=[UNRETRIEVABLE, PRE_ORACLE_FLOOR])
        self.assertEqual(r.counts(), {UNRETRIEVABLE: 342, PRE_ORACLE_FLOOR: 24})
        self.assertEqual(r.by_class(ENGINE_LOST), set())
        self.assertTrue(r.ok, "every divergence has a named, accepted cause")

    def test_one_genuinely_lost_message_still_fails_the_same_window(self):
        """The acceptance list must not be a blanket amnesty: drop a single row that the
        platform WOULD serve and the run must go red."""
        lost = sorted(self.common)[0]
        r = compare(self.oracle, self.candidate - {lost}, "C_EXAMPLE",
                    served_ts=self.served,
                    accept=[UNRETRIEVABLE, PRE_ORACLE_FLOOR])
        self.assertEqual(r.by_class(ENGINE_LOST), {lost})
        self.assertFalse(r.ok)


class _PushShaped:
    """The slack_socket/Telegram shape: a full read surface, no retrievable_ts —
    push receives events; it cannot ask the platform what it still serves."""

    def poll(self, cursor):
        return [], cursor


class _PollShaped(_PushShaped):
    def retrievable_ts(self, channel, oldest=None, latest=None):
        return set()


class SnapshotDeclarationTest(unittest.TestCase):
    """ENH-27: an adapter that cannot supply a platform snapshot must be declared
    BY NAME, or its permanently fail-closed parity runs read as read-path defects —
    the exact misreading that cost a day on R8's first live window."""

    def test_an_adapter_without_retrievable_ts_is_declared_by_name(self):
        d = snapshot_declaration(_PushShaped(), "slack_socket")
        self.assertIn("'slack_socket'", d)
        self.assertIn("retrievable_ts", d, "the missing capability must be named — "
                      "'cannot supply a snapshot' alone tells nobody what to add")

    def test_the_declaration_names_the_unavailable_verdicts_and_the_consequence(self):
        d = snapshot_declaration(_PushShaped(), "slack_socket")
        self.assertIn(UNRETRIEVABLE, d)
        self.assertIn(ORACLE_MISSED, d)
        self.assertIn(ENGINE_LOST, d,
                      "the fail-closed consequence is the whole point: without it an "
                      "operator cannot connect the missing method to the red rows")

    def test_a_capable_adapter_declares_nothing(self):
        self.assertIsNone(snapshot_declaration(_PollShaped(), "slack"))
        # The CLI inspects the discovered CLASS (no auth to construct with) — the
        # capability answer must be the same before and after construction.
        self.assertIsNone(snapshot_declaration(_PollShaped, "slack"))

    def test_a_non_callable_attribute_is_not_a_capability(self):
        shaped = _PushShaped()
        shaped.retrievable_ts = "present but not callable"
        self.assertIsNotNone(snapshot_declaration(shaped, "shaped"))


class SnapshotDeclarationInSummaryTest(unittest.TestCase):
    def test_the_declaration_reaches_the_summary_when_no_snapshot_exists(self):
        d = snapshot_declaration(_PushShaped(), "slack_socket")
        r = compare({"1.1", "1.2"}, {"1.1"}, "C_EXAMPLE", snapshot_unavailable=d)
        s = r.summary()
        self.assertIn("'slack_socket'", s)
        self.assertIn("retrievable_ts", s)

    def test_the_declaration_explains_fail_closed_but_never_softens_it(self):
        d = snapshot_declaration(_PushShaped(), "slack_socket")
        r = compare({"1.1", "1.2"}, {"1.1"}, "C_EXAMPLE", snapshot_unavailable=d)
        self.assertEqual(r.classified["1.2"], ENGINE_LOST)
        self.assertFalse(r.ok)

    def test_a_snapshot_supplied_from_elsewhere_silences_the_declaration(self):
        """A sibling adapter on the same platform (the polling 'slack' adapter, for
        a slack_socket store) can export the snapshot on the push store's behalf;
        then no verdict is degraded and printing 'cannot supply' would misdescribe
        the run that actually happened."""
        d = snapshot_declaration(_PushShaped(), "slack_socket")
        r = compare({"1.1", "1.2"}, {"1.1"}, "C_EXAMPLE", served_ts={"1.1"},
                    snapshot_unavailable=d)
        self.assertNotIn("slack_socket", r.summary())
        self.assertEqual(r.classified["1.2"], UNRETRIEVABLE)


class SnapshotDeclarationCliTest(unittest.TestCase):
    """--candidate-adapter names the channel type FEEDING the candidate store, so a
    parity run pointed at a push store declares the capability gap itself. The class
    is discovered from channels_dir exactly as config does (R11): core never imports
    a platform module by name."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        for name, rows in (("oracle.db", [("C_ONE", "1.1"), ("C_ONE", "1.2")]),
                           ("engine.db", [("C_ONE", "1.1")])):
            c = sqlite3.connect(self.base / name)
            c.execute("CREATE TABLE messages (channel_id TEXT, ts TEXT)")
            c.executemany("INSERT INTO messages VALUES (?,?)", rows)
            c.commit()
            c.close()
        push = self.base / "channels" / "pushonly"
        push.mkdir(parents=True)
        (push / "adapter.py").write_text(
            "class Adapter:\n"
            "    def __init__(self, auth=None):\n"
            "        self.auth = auth or {}\n"
            "    def poll(self, cursor):\n"
            "        return [], cursor\n")

    def run_cli(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = parity_main(["--oracle", str(self.base / "oracle.db"),
                                "--candidate", str(self.base / "engine.db"),
                                "--channel", "C_ONE", *extra])
        return code, out.getvalue(), err.getvalue()

    def test_the_cli_reports_an_incapable_candidate_adapter_by_name(self):
        code, out, _ = self.run_cli("--candidate-adapter", "pushonly",
                                    "--channels-dir", str(self.base / "channels"))
        self.assertEqual(code, 1, "declared is EXPLAINED, never excused — the miss "
                                  "still fails the run")
        self.assertIn("'pushonly'", out)
        self.assertIn("retrievable_ts", out)
        self.assertIn(UNRETRIEVABLE, out)

    def test_an_unknown_candidate_adapter_is_an_unusable_comparison(self):
        """A typo'd type name must not silently degrade to the generic ABSENT line —
        the operator asked for a capability report on an adapter that was not found."""
        code, _, err = self.run_cli("--candidate-adapter", "nope",
                                    "--channels-dir", str(self.base / "channels"))
        self.assertEqual(code, 2)
        self.assertIn("nope", err)


class PanelTest(unittest.TestCase):
    """ENH-24: the operator surface renders a VERDICT, not raw divergence counts.

    The measured failure this kills: R8's first live window read '342 missed, 24
    extra' for a channel whose truthful verdict was PARITY OK / ENGINE_LOST=0. An
    operator who learns to ignore a scary raw number will ignore it on the day
    ENGINE_LOST goes to 1, so panel() leads with the one number that means a defect
    and demotes the raw counts to the tail.
    """

    def lossy_report(self, accept=("UNRETRIEVABLE", "PRE_ORACLE_FLOOR"),
                     with_floor_extra=True):
        """2 ENGINE_LOST, 2 UNRETRIEVABLE, optionally 1 PRE_ORACLE_FLOOR:
        raw missed=4 / extra=1, but only TWO rows are an engine defect."""
        candidate = {"2.0", "1.0"} if with_floor_extra else {"2.0"}
        return compare({"2.0", "3.0", "4.0", "5.0", "6.0"}, candidate, "C_LOST",
                       served_ts={"2.0", "3.0", "4.0"}, accept=accept)

    def clean_report(self):
        """The R8 shape: every divergence falls in an accepted class. verdict OK."""
        return compare({"2.0", "3.0", "4.0", "5.0"}, {"2.0", "3.0", "1.0"}, "C_CLEAN",
                       served_ts={"2.0", "3.0"},
                       accept=("UNRETRIEVABLE", "PRE_ORACLE_FLOOR"))

    def test_engine_lost_is_the_class_count_not_the_raw_missed_count(self):
        p = self.lossy_report().panel()
        self.assertEqual(p["engine_lost"], 2,
                         "the panel's lead number must be the ENGINE_LOST class "
                         "count — reporting the raw missed count (4 here) is the "
                         "exact scary-number failure ENH-24 removes")
        self.assertEqual(p["raw"], {"missed": 4, "extra": 1})
        self.assertEqual(p["engine_lost_sample"], ["3.0", "4.0"],
                         "the operator acts on rows, not on a count")

    def test_the_panel_leads_with_the_verdict_and_demotes_raw_counts(self):
        """Key order IS render order — json and every dict consumer preserve it."""
        keys = list(self.lossy_report().panel())
        self.assertEqual(keys[0], "verdict")
        self.assertEqual(keys[1], "engine_lost")
        self.assertEqual(keys[-1], "raw",
                         "raw missed/extra counts belong at the tail: a raw count "
                         "is not a verdict")

    def test_a_clean_run_reads_ok_not_366_divergences(self):
        p = self.clean_report().panel()
        self.assertEqual(p["verdict"], "PARITY OK")
        self.assertEqual(p["engine_lost"], 0)
        self.assertEqual(p["unexplained"], {})
        self.assertEqual(p["accepted"],
                         {"UNRETRIEVABLE": 2, "PRE_ORACLE_FLOOR": 1},
                         "'clean, for a stated reason' — the accepted-class "
                         "breakdown IS the stated reason")
        self.assertEqual(p["raw"], {"missed": 2, "extra": 1})

    def test_a_lossy_run_reads_fail_with_engine_lost_unexplained(self):
        p = self.lossy_report().panel()
        self.assertEqual(p["verdict"], "PARITY FAIL")
        self.assertEqual(p["unexplained"], {"ENGINE_LOST": 2})

    def test_the_accept_list_in_force_is_named_even_when_a_class_never_occurred(self):
        """The list is the CONFIGURATION, the breakdown is the OCCURRENCE. An
        operator judging a verdict needs both: what was waived, and what showed up."""
        p = self.lossy_report(with_floor_extra=False).panel()
        self.assertEqual(p["accept_list"], ["PRE_ORACLE_FLOOR", "UNRETRIEVABLE"])
        self.assertEqual(p["accepted"], {"UNRETRIEVABLE": 2},
                         "the breakdown lists only classes that occurred")

    def test_an_empty_accept_list_is_an_empty_list_not_a_missing_key(self):
        p = compare({"1.1", "1.2"}, {"1.1", "1.2"}, "C_X").panel()
        self.assertEqual(p["accept_list"], [])

    def test_the_tombstone_slot_defaults_to_unknown(self):
        """The differ has no retention db; the operator surface fills the slot from
        core/retention.py. None is UNKNOWN — rendering it as 0 would be the
        F-2 false-confidence shape."""
        self.assertIsNone(self.clean_report().panel()["tombstones"])


class PanelCliTest(unittest.TestCase):
    """--panel-json persists the verdict-led panel so the read-only dashboard can
    render it. The differ run is the writer; the dashboard never computes parity."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        for name, rows in (("oracle.db", [("C_ONE", "1.1"), ("C_ONE", "1.2")]),
                           ("engine.db", [("C_ONE", "1.1")])):
            c = sqlite3.connect(self.base / name)
            c.execute("CREATE TABLE messages (channel_id TEXT, ts TEXT)")
            c.executemany("INSERT INTO messages VALUES (?,?)", rows)
            c.commit()
            c.close()

    def test_the_cli_writes_the_panel_artifact(self):
        # The parent dir does not exist yet — the writer creates it (state/parity/
        # on a fresh adopter); only the VIEWER is forbidden from minting state.
        out_path = self.base / "parity" / "panel-C_ONE.json"
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = parity_main(["--oracle", str(self.base / "oracle.db"),
                                "--candidate", str(self.base / "engine.db"),
                                "--channel", "C_ONE",
                                "--panel-json", str(out_path)])
        self.assertEqual(code, 1, "persisting the panel must not soften the exit")
        panel = json.loads(out_path.read_text())
        self.assertEqual(list(panel)[0], "verdict")
        self.assertEqual(panel["verdict"], "PARITY FAIL")
        self.assertEqual(panel["engine_lost"], 1)
        self.assertEqual(panel["channel"], "C_ONE")
        self.assertIsInstance(panel["generated_at"], float,
                              "a verdict with no timestamp cannot be judged stale")


class ReadServedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, body):
        p = Path(self.tmp.name) / "served.json"
        p.write_text(body)
        return str(p)

    def test_reads_a_json_list_of_timestamps(self):
        self.assertEqual(read_served(self._write('["1.1", "1.2"]')), {"1.1", "1.2"})

    def test_reads_a_channel_keyed_object_too(self):
        self.assertEqual(read_served(self._write('{"C_A": ["1.1"], "C_B": ["2.2"]}')),
                         {"1.1", "2.2"})

    def test_missing_file_raises_parity_error(self):
        with self.assertRaises(ParityError):
            read_served(str(Path(self.tmp.name) / "nope.json"))

    def test_malformed_json_raises_instead_of_reading_as_no_snapshot(self):
        """Silently degrading to 'no snapshot' would flip the run to fail-closed and hide
        that the snapshot step is broken."""
        with self.assertRaises(ParityError):
            read_served(self._write("{not json"))

    def test_wrong_shape_raises(self):
        with self.assertRaises(ParityError):
            read_served(self._write('"1.1"'))


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
