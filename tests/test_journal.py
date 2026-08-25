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


class RevisionTest(unittest.TestCase):
    """R23 — edits. Found by probing, not by reading: the first version of this journal
    silently discarded an edit, keeping the ORIGINAL text and classification."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.j = Journal(Path(self.tmp.name) / "j.db")

    def tearDown(self):
        self.j.close()
        self.tmp.cleanup()

    def test_an_edit_updates_the_recorded_text(self):
        self.j.record(CH, "1.1", text="Notes from the meeting are in the doc.",
                      kind="STATEMENT")
        self.j.record(CH, "1.1", text="Please deploy the patched image now.",
                      kind="EXEC-REQUEST")
        row = self.j.get(CH, "1.1")
        self.assertIn("deploy", row["text"],
                      "the audit still quotes the ORIGINAL text — it misquotes the channel")
        self.assertEqual(row["kind"], "EXEC-REQUEST",
                         "an edit that turns a remark into an instruction kept the old class")

    def test_an_edit_is_reported_as_a_revision_not_a_duplicate(self):
        self.j.record(CH, "1.1", text="first")
        res = self.j.record(CH, "1.1", text="second")
        self.assertTrue(res.is_revision)
        self.assertFalse(res.is_new)
        self.assertEqual(res.revision, 2)

    def test_an_edit_does_not_create_a_second_row(self):
        self.j.record(CH, "1.1", text="first")
        self.j.record(CH, "1.1", text="second")
        self.j.record(CH, "1.1", text="third")
        self.assertEqual(self.j.row_count(), 1)
        self.assertEqual(self.j.row_count(), self.j.distinct_count())

    def test_full_revision_history_is_retained_for_audit(self):
        self.j.record(CH, "1.1", text="v1", kind="STATEMENT")
        self.j.record(CH, "1.1", text="v2", kind="QUESTION")
        self.j.record(CH, "1.1", text="v3", kind="EXEC-REQUEST")
        revs = self.j.revisions(CH, "1.1")
        self.assertEqual([r["text"] for r in revs], ["v1", "v2", "v3"])
        self.assertEqual([r["kind"] for r in revs],
                         ["STATEMENT", "QUESTION", "EXEC-REQUEST"])

    def test_identical_text_is_a_resighting_not_a_revision(self):
        self.j.record(CH, "1.1", text="same")
        res = self.j.record(CH, "1.1", text="same")
        self.assertEqual(res.status, "reseen")
        self.assertEqual(self.j.get(CH, "1.1")["revision"], 1)
        self.assertEqual(len(self.j.revisions(CH, "1.1")), 1)

    def test_a_bare_resighting_with_no_text_is_not_an_edit_to_empty(self):
        """A poller that re-sights without re-sending the body must not wipe the record."""
        self.j.record(CH, "1.1", text="real content", kind="QUESTION")
        res = self.j.record(CH, "1.1")
        self.assertEqual(res.status, "reseen")
        self.assertEqual(self.j.get(CH, "1.1")["text"], "real content")

    def test_an_edit_after_we_answered_is_surfaced(self):
        """We answered version 1; the channel now shows version 2. Somebody must look."""
        self.j.record(CH, "1.1", text="original ask")
        self.j.mark_responded(CH, "1.1", response_key="k1")
        self.assertEqual(self.j.edited_after_response(), [])
        self.j.record(CH, "1.1", text="actually, do something else entirely")
        flagged = self.j.edited_after_response()
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["ts"], "1.1")

    def test_seen_count_still_counts_every_sighting_including_edits(self):
        self.j.record(CH, "1.1", text="a")
        self.j.record(CH, "1.1", text="a")
        self.j.record(CH, "1.1", text="b")
        self.assertEqual(self.j.get(CH, "1.1")["seen_count"], 3)


class AuditLinkTest(unittest.TestCase):
    """R22: the journal row is where a classification gets DISPUTED — possibly months
    later, possibly after the taxonomy changed, so re-running the classifier over the
    text is not evidence of what the engine decided at the time. The row itself must
    link back to the cues the decision actually matched."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "j.db"
        self.j = Journal(self.path)

    def tearDown(self):
        self.j.close()
        self.tmp.cleanup()

    def test_a_journal_row_links_back_to_the_decision_cues(self):
        self.j.record(CH, "1.1", text="Please deploy the image.", kind="EXEC-REQUEST",
                      reason="imperative or directed request to perform work",
                      matched=["deploy", "please"])
        a = self.j.audit(CH, "1.1")
        self.assertEqual(a["kind"], "EXEC-REQUEST")
        self.assertTrue(a["reason"])
        self.assertEqual(a["matched"], ["deploy", "please"],
                         "the cues never reached the journal — the decision cannot be "
                         "disputed from the audit trail")

    def test_the_audit_link_survives_a_restart(self):
        self.j.record(CH, "1.1", text="Where is the doc?", kind="QUESTION",
                      reason="asks for information", matched=["where is"])
        self.j.close()
        self.j = Journal(self.path)
        self.assertEqual(self.j.audit(CH, "1.1")["matched"], ["where is"])

    def test_a_bare_resighting_does_not_erase_the_cues(self):
        self.j.record(CH, "1.1", text="Please deploy it.", kind="EXEC-REQUEST",
                      reason="imperative", matched=["deploy", "please"])
        self.j.record(CH, "1.1")
        self.assertEqual(self.j.audit(CH, "1.1")["matched"], ["deploy", "please"])

    def test_each_revision_keeps_its_own_decisions_cues(self):
        """An edit re-classifies; the dispute 'why did you answer version 1 that way?'
        needs version 1's cues, not the live row's."""
        import json
        self.j.record(CH, "1.1", text="Where is the doc?", kind="QUESTION",
                      reason="asks for information", matched=["where is"])
        self.j.record(CH, "1.1", text="Please deploy the doc fix.", kind="EXEC-REQUEST",
                      reason="imperative", matched=["deploy", "please"])
        revs = self.j.revisions(CH, "1.1")
        self.assertEqual([json.loads(r["matched"]) for r in revs],
                         [["where is"], ["deploy", "please"]])
        self.assertEqual(self.j.audit(CH, "1.1")["matched"], ["deploy", "please"])

    def test_export_carries_the_audit_link(self):
        """The export is what any rendering surface (dashboard included) reads; cues
        that die before the export are not auditable, only stored."""
        import json
        self.j.record(CH, "1.1", text="Please deploy it.", kind="EXEC-REQUEST",
                      reason="imperative", matched=["deploy", "please"])
        line = json.loads(self.j.export_jsonl())
        self.assertEqual(json.loads(line["matched"]), ["deploy", "please"])

    def test_recorded_empty_cues_stay_distinct_from_never_recorded(self):
        """[] means 'the classifier matched nothing'; None means 'no decision was ever
        recorded'. Collapsing them would let an absent record masquerade as evidence."""
        self.j.record(CH, "1.1", text="plain remark", kind="STATEMENT",
                      reason="no directive, question or commitment cue", matched=[])
        self.j.record(CH, "2.2", text="unclassified sighting")
        self.assertEqual(self.j.audit(CH, "1.1")["matched"], [])
        self.assertIsNone(self.j.audit(CH, "2.2")["matched"])

    def test_a_pre_audit_database_is_migrated_on_open(self):
        """Adopters already hold journal.db files created before the cues column
        existed. Refusing to open one would destroy an audit trail in order to improve
        it — the column must be added in place, with legacy rows reading as None."""
        import sqlite3
        old = Path(self.tmp.name) / "old.db"
        conn = sqlite3.connect(old)
        conn.executescript("""
            CREATE TABLE journal (
                channel TEXT NOT NULL, ts TEXT NOT NULL, sender_id TEXT, text TEXT,
                text_hash TEXT, kind TEXT, reason TEXT, routed TEXT,
                first_seen_at REAL NOT NULL, last_seen_at REAL NOT NULL,
                seen_count INTEGER NOT NULL DEFAULT 1,
                revision INTEGER NOT NULL DEFAULT 1,
                responded_at REAL, response_key TEXT,
                PRIMARY KEY (channel, ts));
            CREATE TABLE revisions (
                channel TEXT NOT NULL, ts TEXT NOT NULL, seq INTEGER NOT NULL,
                text TEXT, kind TEXT, reason TEXT, recorded_at REAL NOT NULL,
                PRIMARY KEY (channel, ts, seq));
            INSERT INTO journal VALUES ('C_LEGACY', '0.9', NULL, 'old row', 'h',
                'STATEMENT', 'legacy reason', NULL, 1.0, 1.0, 1, 1, NULL, NULL);
        """)
        conn.commit()
        conn.close()
        j2 = Journal(old)
        try:
            self.assertIsNone(j2.audit("C_LEGACY", "0.9")["matched"])
            j2.record("C_LEGACY", "1.0", text="Please deploy it.", kind="EXEC-REQUEST",
                      reason="imperative", matched=["deploy", "please"])
            self.assertEqual(j2.audit("C_LEGACY", "1.0")["matched"],
                             ["deploy", "please"])
        finally:
            j2.close()


class ConcurrencyTest(unittest.TestCase):
    """The outbox held under 6 concurrent senders when probed. A property that holds by
    accident is one refactor from breaking, so it is pinned here."""

    def test_concurrent_senders_deliver_exactly_once(self):
        import threading
        from core.outbox import Outbox

        class A:
            def __init__(self):
                self.delivered = []
                self.lock = threading.Lock()

            def send(self, target, text, key=None):
                with self.lock:
                    self.delivered.append((target, text, key))
                return {"ts": "r", "key": key}

            def read_back(self, target, key):
                with self.lock:
                    return any(k == key for _, _, k in self.delivered)

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "o.db"
            ad = A()
            errs = []

            def worker():
                try:
                    Outbox(db, ad, {"C": "direct"}).send("C", "1.1", "same text")
                except BaseException as e:      # noqa: BLE001
                    errs.append(type(e).__name__)

            threads = [threading.Thread(target=worker) for _ in range(6)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            self.assertEqual(len(ad.delivered), 1,
                             f"{len(ad.delivered)} deliveries from 6 concurrent senders")
            self.assertEqual(errs, [], f"concurrent senders raised: {errs}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
