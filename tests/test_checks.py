"""Tests for core/checks.py — the meta-property is the point (gate G4; R5, R6).

The framework's job is to make "a check that can neither pass nor fail" impossible. So the
central test is not that a check works — it is that a BROKEN check still produces a FAIL.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.checks import (CheckContractError, Registry, Verdict,  # noqa: E402
                         freshness_check, schema_check, watcher_source_check)


class VerdictContractTest(unittest.TestCase):
    def test_pass_on_zero_inspected_is_refused(self):
        """The vacuous pass, banned at construction time."""
        with self.assertRaises(CheckContractError):
            Verdict.passed("empty-scan", inspected=0)

    def test_pass_with_evidence_is_allowed(self):
        v = Verdict.passed("scan", inspected=13)
        self.assertTrue(v.ok)
        self.assertEqual(v.inspected, 13)

    def test_failed_needs_no_evidence(self):
        self.assertFalse(Verdict.failed("x", "because").ok)


class RegistryTest(unittest.TestCase):
    """Every registered check MUST yield exactly one verdict — no skips, no silence."""

    def test_every_check_produces_a_verdict(self):
        r = Registry()
        r.add("good", lambda: Verdict.passed("good", 1))
        r.add("bad", lambda: Verdict.failed("bad", "nope"))
        results = r.run_all()
        self.assertEqual(len(results), 2)

    def test_a_check_returning_none_becomes_a_failure(self):
        """This is the silent no-op, converted into a loud FAIL."""
        r = Registry()
        r.add("silent", lambda: None)
        results = r.run_all()
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertIn("no verdict", results[0].detail)

    def test_a_raising_check_becomes_a_failure_not_a_skip(self):
        r = Registry()

        def boom():
            raise OSError("probe exploded")
        r.add("explodes", boom)
        results = r.run_all()
        self.assertFalse(results[0].ok)
        self.assertIn("OSError", results[0].detail)

    def test_a_check_returning_junk_becomes_a_failure(self):
        r = Registry()
        r.add("junk", lambda: "looks fine to me")
        results = r.run_all()
        self.assertFalse(results[0].ok)
        self.assertIn("not a Verdict", results[0].detail)

    def test_summary_says_FAIL_when_any_check_fails(self):
        r = Registry()
        r.add("ok", lambda: Verdict.passed("ok", 1))
        r.add("silent", lambda: None)
        s = Registry.summary(r.run_all())
        self.assertIn("FAIL", s)
        self.assertIn("silent(FAIL)", s)

    def test_summary_never_says_OK_while_a_check_emitted_nothing(self):
        """The exact incumbent bug: 'OK — 7 checks passed' while check 7 was inert."""
        r = Registry()
        for i in range(6):
            r.add(f"c{i}", lambda: Verdict.passed("c", 1))
        r.add("inert", lambda: None)
        self.assertNotIn("OK —", Registry.summary(r.run_all()))


class SchemaCheckTest(unittest.TestCase):
    REQ = ("ts", "channel_id", "user_id")

    def test_records_with_all_fields_pass(self):
        recs = [{"ts": "1", "channel_id": "C", "user_id": "U"}]
        self.assertTrue(schema_check("inbox", recs, self.REQ).ok)

    def test_renamed_field_fails_and_names_the_field(self):
        """.timestamp instead of .ts — the field that broke the live watchdog."""
        recs = [{"timestamp": "1", "channel_id": "C", "user_id": "U"}]
        v = schema_check("inbox", recs, self.REQ)
        self.assertFalse(v.ok)
        self.assertIn("ts", v.detail)

    def test_empty_record_set_fails_rather_than_passing(self):
        v = schema_check("inbox", [], self.REQ)
        self.assertFalse(v.ok)
        self.assertIn("no records", v.detail)


class WatcherSourceCheckTest(unittest.TestCase):
    def test_present_source_passes(self):
        v = watcher_source_check("reply-watcher", "pane agent:0.0", lambda: True)
        self.assertTrue(v.ok)

    def test_missing_source_fails_and_calls_it_a_zombie(self):
        """F-1: the watcher scraping a tmux pane that does not exist."""
        v = watcher_source_check("reply-watcher", "pane jarvis:0.0", lambda: False)
        self.assertFalse(v.ok)
        self.assertIn("zombie", v.detail.lower())
        self.assertIn("jarvis", v.detail)

    def test_raising_probe_fails(self):
        def boom():
            raise RuntimeError("tmux missing")
        self.assertFalse(watcher_source_check("w", "pane", boom).ok)


class FreshnessCheckTest(unittest.TestCase):
    def test_fresh_within_budget_passes(self):
        self.assertTrue(freshness_check("poller", age_s=10, budget_s=60).ok)

    def test_stale_beyond_budget_fails(self):
        v = freshness_check("poller", age_s=600, budget_s=60)
        self.assertFalse(v.ok)
        self.assertIn("stale", v.detail)

    def test_action_only_log_is_healthy_when_silent(self):
        """The false-DEGRADED bug: a log written only on action is fine when quiet."""
        v = freshness_check("session-watchdog", age_s=115000, budget_s=900,
                            action_only_log=True)
        self.assertTrue(v.ok)
        self.assertIn("silence is health", v.detail)

    def test_missing_age_fails_rather_than_passing(self):
        self.assertFalse(freshness_check("poller", age_s=None, budget_s=60).ok)

    def test_missing_budget_fails_rather_than_guessing(self):
        v = freshness_check("poller", age_s=10, budget_s=None)
        self.assertFalse(v.ok)
        self.assertIn("no cadence budget", v.detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
