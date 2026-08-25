"""Tests for core/journal.py (gate G10; requirement R16).

The invariant: **one row per distinct message, however many times it is seen.** The incumbent
log holds 323 entries for 177 distinct messages (77 duplicated, one 9 times), so this suite
reproduces that scenario and asserts the count stays honest.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.journal import Journal  # noqa: E402

CH = "C_EXAMPLE"


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.j = Journal(Path(self.tmp.name) / "j.db")

    def tearDown(self):
        self.j.close()
        self.tmp.cleanup()

    # ---- R16: idempotence -------------------------------------------------
    def test_first_sighting_reports_true_and_creates_one_row(self):
        self.assertTrue(self.j.record(CH, "1.1", text="hello"))
        self.assertEqual(self.j.row_count(), 1)

    def test_re_recording_the_same_message_does_not_append(self):
        self.j.record(CH, "1.1", text="hello")
        self.assertFalse(self.j.record(CH, "1.1", text="hello"))
        self.assertEqual(self.j.row_count(), 1)

    def test_the_incumbent_scenario_one_message_seen_nine_times(self):
        """A real message in the live log was re-appended 9 times."""
        for _ in range(9):
            self.j.record(CH, "1787623032.775359", text="the same ask")
        self.assertEqual(self.j.row_count(), 1,
                         "a message seen 9 times produced more than one audit row")
        self.assertEqual(self.j.get(CH, "1787623032.775359")["seen_count"], 9,
                         "re-sightings must be observable, just not duplicated")

    def test_row_count_equals_distinct_count_always(self):
        """The invariant the incumbent violates by 45%."""
        for i in range(20):
            self.j.record(CH, f"{i}.0", text=f"m{i}")
        for i in range(20):                      # replay the whole window twice
            self.j.record(CH, f"{i}.0", text=f"m{i}")
            self.j.record(CH, f"{i}.0", text=f"m{i}")
        self.assertEqual(self.j.row_count(), self.j.distinct_count())
        self.assertEqual(self.j.row_count(), 20)

    def test_same_ts_in_different_channels_are_distinct(self):
        self.j.record("C_ONE", "1.1", text="a")
        self.j.record("C_TWO", "1.1", text="b")
        self.assertEqual(self.j.row_count(), 2)

    def test_seen_count_starts_at_one(self):
        self.j.record(CH, "1.1")
        self.assertEqual(self.j.get(CH, "1.1")["seen_count"], 1)

    def test_last_seen_advances_while_first_seen_is_stable(self):
        self.j.record(CH, "1.1")
        first = self.j.get(CH, "1.1")["first_seen_at"]
        self.j.record(CH, "1.1")
        row = self.j.get(CH, "1.1")
        self.assertEqual(row["first_seen_at"], first)
        self.assertGreaterEqual(row["last_seen_at"], first)

    # ---- classification/routing are part of the audit ---------------------
    def test_classification_is_recorded_and_can_be_refined(self):
        self.j.record(CH, "1.1", text="x", kind="STATEMENT", reason="initial")
        self.j.record(CH, "1.1", text="x", kind="EXEC-REQUEST", reason="refined")
        row = self.j.get(CH, "1.1")
        self.assertEqual(row["kind"], "EXEC-REQUEST")
        self.assertEqual(self.j.row_count(), 1)

    def test_a_later_null_does_not_erase_a_recorded_classification(self):
        self.j.record(CH, "1.1", kind="QUESTION", reason="asked")
        self.j.record(CH, "1.1")                 # a bare re-sighting
        self.assertEqual(self.j.get(CH, "1.1")["kind"], "QUESTION")

    def test_by_kind_counts_distinct_messages_not_sightings(self):
        for _ in range(5):
            self.j.record(CH, "1.1", kind="QUESTION")
        self.j.record(CH, "2.2", kind="STATEMENT")
        self.assertEqual(self.j.by_kind(), {"QUESTION": 1, "STATEMENT": 1})

    # ---- answering the audit's real question ------------------------------
    def test_unanswered_lists_asks_with_no_response(self):
        self.j.record(CH, "1.1", text="ask one")
        self.j.record(CH, "2.2", text="ask two")
        self.j.mark_responded(CH, "1.1", response_key="abc123")
        self.assertEqual([r["ts"] for r in self.j.unanswered()], ["2.2"])

    def test_response_is_linked_to_the_outbox_key(self):
        self.j.record(CH, "1.1")
        self.j.mark_responded(CH, "1.1", response_key="deadbeef")
        row = self.j.get(CH, "1.1")
        self.assertEqual(row["response_key"], "deadbeef")
        self.assertIsNotNone(row["responded_at"])

    def test_unanswered_can_be_scoped_to_a_channel(self):
        self.j.record("C_ONE", "1.1")
        self.j.record("C_TWO", "2.2")
        self.assertEqual(len(self.j.unanswered("C_ONE")), 1)

    def test_export_emits_one_line_per_distinct_message(self):
        for _ in range(4):
            self.j.record(CH, "1.1", text="dup")
        self.j.record(CH, "2.2", text="other")
        self.assertEqual(len(self.j.export_jsonl().splitlines()), 2)

    def test_journal_survives_a_restart(self):
        self.j.record(CH, "1.1", text="durable")
        path = self.j.path if hasattr(self.j, "path") else None
        del path
        j2 = Journal(Path(self.tmp.name) / "j.db")
        self.assertEqual(j2.row_count(), 1)
        self.assertFalse(j2.record(CH, "1.1", text="durable"),
                         "a restarted journal re-appended an existing message")


if __name__ == "__main__":
    unittest.main(verbosity=2)
