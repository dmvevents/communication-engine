"""Tests for core/classify.py (gate G9; requirement R15).

The corpus below is SYNTHETIC. It paraphrases the linguistic shapes observed in the live
system's own log — real colleague/customer text is never copied into this repo. Each
"regression" case reproduces a shape the incumbent substring classifier got wrong.

The headline metric the gate defends: the false-EXEC rate on reporting-style statements.
Measured on the incumbent: 47 of 138 EXEC-REQUESTs (34%) matched a substring only.
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.classify import Taxonomy, classify, classify_batch  # noqa: E402

# (text, expected_kind). Shapes drawn from the live log, reworded.
CORPUS = [
    # --- REGRESSIONS: these were classified EXEC-REQUEST by substring matching ---
    ("I have finished testing all the aggregate options on the new nodes.", "STATEMENT"),
    ("I am running the disaggregated deployment and it is working fine.", "STATEMENT"),
    ("For the expert-parallel case, I exposed the worker via a service.", "STATEMENT"),
    ("A couple of notes: the instances come back Wednesday night.", "STATEMENT"),
    ("I already rebuilt the image with the latest patch.", "STATEMENT"),
    ("We finished the smoke run and everything passed.", "STATEMENT"),
    ("The frontend is working now after I merged the fix.", "STATEMENT"),
    ("I verified the traffic path end to end.", "STATEMENT"),
    ("Testing continues on the second cluster.", "STATEMENT"),
    ("The latest build is greatest so far.", "STATEMENT"),

    # --- genuine EXEC-REQUESTs ---
    ("Can you run the smoke test on the new cluster?", "EXEC-REQUEST"),
    ("Please deploy the patched image and report back.", "EXEC-REQUEST"),
    ("Run the full matrix and open a pull request when it drains.", "EXEC-REQUEST"),
    ("Could you rebuild the container with the newer driver?", "EXEC-REQUEST"),
    ("Use the cluster as needed to test, I will check back tomorrow.", "EXEC-REQUEST"),
    ("Go ahead and restart the workers.", "EXEC-REQUEST"),
    ("I need you to reproduce the failure on two nodes.", "EXEC-REQUEST"),

    # --- word-boundary isolators: an exec verb appears ONLY as a substring inside another
    # word ("latest" contains "test", "running" contains "run") ALONGSIDE a directive
    # marker. Under substring matching these become EXEC-REQUEST, which is exactly the
    # measured incumbent bug; these cases are what make that mutation catchable.
    ("Can you send me the latest numbers?", "QUESTION"),
    ("Could you confirm the latest status?", "QUESTION"),
    ("Can you confirm the job is still running?", "QUESTION"),

    # --- QUESTIONs ---
    ("Where is the manifest for the disaggregated setup?", "QUESTION"),
    ("How do I point this at a different workspace?", "QUESTION"),
    ("What is the current latency number?", "QUESTION"),
    ("Can you send me the link to the results file?", "QUESTION"),
    ("Any idea why the frontend returns an empty list?", "QUESTION"),

    # --- COMMITMENT-ASKs (need a human gate) ---
    ("Can you commit to having this done by Friday?", "COMMITMENT-ASK"),
    ("What is the ETA for the report?", "COMMITMENT-ASK"),
    ("Please approve the change so we can proceed.", "COMMITMENT-ASK"),
    ("By when will the numbers be ready?", "COMMITMENT-ASK"),
    ("I need you to sign off on the results.", "COMMITMENT-ASK"),

    # --- plain STATEMENTs ---
    ("Notes from today's meeting are in the shared doc.", "STATEMENT"),
    ("The code is pushed to the repository.", "STATEMENT"),
    ("Here is the project structure I have in mind.", "STATEMENT"),
    ("Thanks, that explains the discrepancy.", "STATEMENT"),
]


class CorpusTest(unittest.TestCase):
    def test_every_corpus_message_classifies_as_labelled(self):
        wrong = []
        for text, expected in CORPUS:
            got = classify(text)
            if got.kind != expected:
                wrong.append((text[:58], expected, got.kind, got.reason[:40]))
        self.assertEqual(wrong, [], f"{len(wrong)}/{len(CORPUS)} misclassified: {wrong}")

    def test_no_reporting_statement_is_ever_read_as_an_order(self):
        """The consequential direction: a false EXEC can start a cluster run."""
        reporting = [t for t, exp in CORPUS if exp == "STATEMENT"]
        false_exec = [t for t in reporting if classify(t).kind == "EXEC-REQUEST"]
        self.assertEqual(false_exec, [],
                         f"false EXEC-REQUEST on narration: {false_exec}")

    def test_false_exec_rate_is_zero_on_the_corpus(self):
        """Incumbent baseline was 34% substring-only EXEC matches; the gate demands 0 here."""
        non_exec = [(t, e) for t, e in CORPUS if e != "EXEC-REQUEST"]
        bad = [t for t, _ in non_exec if classify(t).kind == "EXEC-REQUEST"]
        rate = len(bad) / len(non_exec)
        self.assertEqual(rate, 0.0, f"false-EXEC rate {rate:.0%} on non-exec corpus: {bad}")

    def test_recall_on_genuine_exec_requests_is_total(self):
        execs = [t for t, e in CORPUS if e == "EXEC-REQUEST"]
        missed = [t for t in execs if classify(t).kind != "EXEC-REQUEST"]
        self.assertEqual(missed, [], f"missed genuine EXEC-REQUESTs: {missed}")


class WordBoundaryTest(unittest.TestCase):
    """The single change that removes the substring class of error."""

    def test_testing_does_not_match_test_as_a_substring_alone(self):
        c = classify("Testing continues on the second cluster.")
        self.assertNotEqual(c.kind, "EXEC-REQUEST")

    def test_latest_does_not_match_test(self):
        self.assertNotEqual(classify("The latest numbers look fine.").kind, "EXEC-REQUEST")

    def test_currently_does_not_match_run(self):
        self.assertNotEqual(classify("Currently the queue is empty.").kind, "EXEC-REQUEST")

    def test_a_bare_imperative_verb_still_counts(self):
        self.assertEqual(classify("Deploy the new image, please.").kind, "EXEC-REQUEST")


class PrecedenceTest(unittest.TestCase):
    def test_commitment_outranks_an_exec_verb(self):
        c = classify("Can you run it and commit to a date?")
        self.assertEqual(c.kind, "COMMITMENT-ASK",
                         "a commitment ask must reach a human even when it also asks for work")

    def test_reporting_plus_directive_is_still_an_exec_request(self):
        """'I ran it, please run it again' — narration does not cancel a real instruction."""
        c = classify("I already ran the smoke test, but can you run it again on node two?")
        self.assertEqual(c.kind, "EXEC-REQUEST")

    def test_ambiguous_verb_without_directive_classifies_down(self):
        """Deliberate asymmetry: a missed order costs a follow-up; a false order costs a run."""
        c = classify("The build and test cycle takes an hour.")
        self.assertEqual(c.kind, "STATEMENT")
        self.assertIn("ambiguous", c.reason)


class ConfigurabilityTest(unittest.TestCase):
    """Gate G8: an adopting team overrides vocabulary by CONFIG, never by forking."""

    def test_custom_exec_verbs_are_honoured(self):
        tax = Taxonomy.from_config({"exec_verbs": ["provision", "tear down"]})
        self.assertEqual(classify("Please provision the environment.", tax).kind,
                         "EXEC-REQUEST")

    def test_default_verbs_are_inert_when_overridden(self):
        tax = Taxonomy.from_config({"exec_verbs": ["provision"]})
        self.assertNotEqual(classify("Please deploy the image.", tax).kind, "EXEC-REQUEST")

    def test_from_config_with_none_uses_defaults(self):
        self.assertEqual(classify("Please deploy it.", Taxonomy.from_config(None)).kind,
                         "EXEC-REQUEST")


class RobustnessTest(unittest.TestCase):
    def test_empty_and_whitespace_are_statements_not_errors(self):
        for t in ("", "   ", "\n"):
            self.assertEqual(classify(t).kind, "STATEMENT")

    def test_none_does_not_raise(self):
        self.assertEqual(classify(None).kind, "STATEMENT")

    def test_every_classification_carries_a_reason(self):
        """An unexplained classification cannot be audited or disputed."""
        for text, _ in CORPUS:
            self.assertTrue(classify(text).reason,
                            f"no reason given for {text[:40]!r}")

    def test_batch_matches_individual(self):
        texts = [t for t, _ in CORPUS[:8]]
        self.assertEqual([c.kind for c in classify_batch(texts)],
                         [classify(t).kind for t in texts])


class AuditabilityTest(unittest.TestCase):
    """R22: a decision is disputable only if it NAMES the cues that produced it. The
    dispute may come months later, after the taxonomy changed — so the evidence must
    travel with the decision, not be reconstructed by re-running the classifier."""

    def test_every_decision_names_the_cues_that_matched(self):
        """Corpus-wide: any classification above the no-cue fall-through must carry at
        least one matched cue. A bare kind cannot be argued with."""
        for text, expected in CORPUS:
            c = classify(text)
            self.assertTrue(c.reason, f"no reason given for {text[:40]!r}")
            if expected != "STATEMENT":
                self.assertTrue(c.matched,
                                f"{c.kind} decision names no cue for {text[:40]!r} — "
                                "it cannot be disputed")

    def test_recorded_cues_are_verifiable_against_the_text(self):
        """Each recorded cue must be checkable in the message itself; that check is what
        turns 'the engine says so' into evidence."""
        for text, _ in CORPUS:
            for cue in classify(text).matched:
                if cue == "ends with '?'":
                    self.assertTrue(text.rstrip().endswith("?"),
                                    f"pseudo-cue recorded but {text[:40]!r} has no '?'")
                    continue
                pattern = r"\b" + r"\s+".join(re.escape(w) for w in cue.split()) + r"\b"
                self.assertTrue(re.search(pattern, text, flags=re.IGNORECASE),
                                f"recorded cue {cue!r} does not occur in {text[:40]!r} — "
                                "the audit trail would assert evidence that is not there")

    def test_the_ambiguous_downgrade_names_the_verb_it_refused_to_act_on(self):
        """The downgrade is the decision most worth disputing ('why was my order
        ignored?'); its row must show the verb seen and, by the reason, the directive
        that was missing."""
        c = classify("The build and test cycle takes an hour.")
        self.assertEqual(c.kind, "STATEMENT")
        self.assertTrue(set(c.matched) & {"build", "test"},
                        "the downgraded decision does not name the verb it saw")


if __name__ == "__main__":
    unittest.main(verbosity=2)
