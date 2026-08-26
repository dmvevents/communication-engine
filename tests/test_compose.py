"""Reply-composition citation discipline (ENH-10).

The origin system's strongest reply rule is human, not structural: "never improvise a
number — cite banked artifacts". Every figure in an outbound reply (a latency, a count,
a commit, a pass rate) must come from evidence that was actually recorded, because a
plausible-sounding invented number sent in the operator's name to a customer channel is
unrecoverable in exactly the way a duplicated message is not. Meanwhile `Outbox.send()`
is deliberately content-blind — it delivers whatever string it is handed.

`core/compose.py` is the layer between "I want to reply" and "here is the string":

* prose may carry NO digit-bearing tokens — a number can only enter a reply as a claim;
* a claim REQUIRES a citation, and the citation must resolve to a banked artifact;
* a citation is not a laundering stamp: every number in the claim must actually occur
  in the cited artifact (its ref or its content), so citing real evidence for an
  invented figure is refused too;
* the citation is rendered INTO the reply, so the recipient can check it — discipline
  that is invisible on the wire protects nobody.

"Factual claim" here means the machine-checkable subset: digit-bearing tokens. A false
claim written in words alone is the composer's judgment, honestly out of scope.
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.compose import CitationError, Composer, factual_claims  # noqa: E402
from core.config import load_adapter_class  # noqa: E402
from core.outbox import Outbox  # noqa: E402

# A bank is (ref -> artifact content). Refs are how the reply will cite; content is the
# banked evidence itself. "bench.log"'s numbers appear nowhere else, so any test that
# passes with it proves coverage came from the artifact, not from another entry.
BANK = {
    "bench.log": "fresh redeploy: p50=691.5us over 96 iterations, ratio 0.92",
    "progress.log": "campaign status: 88 of 96 items complete",
    "ci.log": "suite green, zero failures, no skips",
}


class FactualClaimDetectorTest(unittest.TestCase):
    """The detector is the single definition of 'factual claim' — prose() and claim()
    must share it, or a number one path refuses could slip through the other."""

    def test_number_free_text_has_no_claims(self):
        self.assertEqual(factual_claims("suite green, no blockers, ready to merge"), [])

    def test_numbers_decimals_versions_and_times_are_claims(self):
        self.assertEqual(factual_claims("p50 was 691.5us at ratio 0.92"),
                         ["50", "691.5", "0.92"])
        self.assertEqual(factual_claims("pinned 1.35.0 at 03:35"), ["1.35.0", "03:35"])

    def test_a_comma_grouped_number_is_detected_in_parts(self):
        # "1,440" vs "1440" is a formatting choice, and "options 1,2" is a list — the
        # comma is ambiguous, so it SPLITS. Consistent on both the claim and the
        # artifact side, so coverage matching still works for either formatting.
        self.assertEqual(factual_claims("1,440 alerts"), ["1", "440"])


class ProseDisciplineTest(unittest.TestCase):
    def test_prose_with_no_numbers_composes(self):
        text = "no blockers, all lanes green"
        self.assertEqual(Composer(BANK).prose(text).render(), text)

    def test_prose_carrying_a_number_is_refused(self):
        """The rule that makes the layer worth having: a number cannot enter a reply
        as prose. The refusal names the number so the composer knows what to cite."""
        with self.assertRaises(CitationError) as ctx:
            Composer(BANK).prose("p50 was 691.5us on the fresh pod")
        self.assertIn("691.5", str(ctx.exception))


class ClaimCitationTest(unittest.TestCase):
    def test_a_claim_with_a_banked_citation_composes(self):
        # "bench.log" carries no digits itself, so this passing proves the numbers
        # were covered by the artifact CONTENT — the pair of the ref-only test below.
        out = Composer(BANK).claim("p50 was 691.5us", cite="bench.log").render()
        self.assertIn("691.5", out)

    def test_a_claim_requires_a_citation(self):
        # Deliberately number-free text: with a number in it, the improvised-number
        # check would ALSO raise and this test could not tell the two properties
        # apart — the citation-required check needs teeth of its own.
        for empty in ("", (), None):
            with self.assertRaises(CitationError, msg=f"cite={empty!r} was accepted"):
                Composer(BANK).claim("the suite is fully green", cite=empty)

    def test_a_citation_must_resolve_to_a_banked_artifact(self):
        with self.assertRaises(CitationError) as ctx:
            Composer(BANK).claim("96 iterations passed", cite="made-up.log")
        self.assertIn("made-up.log", str(ctx.exception))

    def test_an_improvised_number_is_refused_even_with_a_citation(self):
        """A real citation on an invented figure is laundering, not discipline: the
        artifact says 691.5, the claim says 700 — refused, naming both sides."""
        with self.assertRaises(CitationError) as ctx:
            Composer(BANK).claim("p50 was 700us", cite="bench.log")
        self.assertIn("700", str(ctx.exception))
        self.assertIn("bench.log", str(ctx.exception))

    def test_a_rounded_or_rescaled_number_is_improvised(self):
        # The artifact says 0.92; "92%" is a human's transformation of it. "Never
        # improvise" includes derivation — bank the derived figure or quote exactly.
        with self.assertRaises(CitationError) as ctx:
            Composer(BANK).claim("ratio was 92%", cite="bench.log")
        self.assertIn("92", str(ctx.exception))

    def test_numbers_in_the_citation_ref_itself_are_banked(self):
        # A ref like "commit 4b41428" IS banked evidence: requiring its digits to be
        # duplicated into the content would be ceremony, not discipline.
        bank = {"commit 4b41428": "the isolation fix"}
        out = Composer(bank).claim("landed in 4b41428", cite="commit 4b41428").render()
        self.assertIn("4b41428", out)

    def test_coverage_may_span_multiple_cited_artifacts(self):
        out = (Composer(BANK)
               .claim("88 of 96 done, p50 691.5us", cite=("progress.log", "bench.log"))
               .render())
        self.assertIn("88", out)

    def test_every_cited_ref_must_be_banked_even_in_a_list(self):
        with self.assertRaises(CitationError) as ctx:
            Composer(BANK).claim("88 of 96 done", cite=("progress.log", "wish.log"))
        self.assertIn("wish.log", str(ctx.exception))

    def test_a_wordy_claim_with_a_citation_composes(self):
        # Citing MORE than the detector can force is allowed and encouraged: the
        # detector is the floor of the discipline, not its ceiling.
        out = Composer(BANK).claim("the suite is green", cite="ci.log").render()
        self.assertIn("ci.log", out)


class RenderTest(unittest.TestCase):
    def test_render_carries_the_citation_next_to_the_claim(self):
        """The citation must survive into the string the outbox will send — a receipt
        the recipient can check. Internal-only bookkeeping would satisfy every other
        test here and still send a bare unverifiable number."""
        out = Composer(BANK).claim("p50 was 691.5us", cite="bench.log").render()
        self.assertEqual(out, "p50 was 691.5us (per bench.log)")

    def test_render_names_every_cited_artifact(self):
        out = (Composer(BANK)
               .claim("88 of 96 done, p50 691.5us", cite=("progress.log", "bench.log"))
               .render())
        self.assertEqual(out, "88 of 96 done, p50 691.5us (per progress.log, bench.log)")

    def test_render_preserves_composition_order(self):
        out = (Composer(BANK)
               .prose("smoke finished.")
               .claim("88 of 96 done", cite="progress.log")
               .prose("no blockers.")
               .render())
        self.assertEqual(out, "smoke finished. 88 of 96 done (per progress.log) "
                              "no blockers.")

    def test_prose_renders_without_a_citation_marker(self):
        self.assertNotIn("(per", Composer(BANK).prose("all lanes green").render())


class OutboxSeamTest(unittest.TestCase):
    """ENH-10's why, verbatim: 'the outbox sends whatever string it is handed'. The
    composed string — citation included — must be EXACTLY what crosses the adapter,
    or the discipline evaporates at the last seam. Fake adapter only: this test must
    never be able to post anywhere real."""

    def test_a_composed_reply_travels_the_outbox_intact(self):
        rendered = (Composer(BANK)
                    .claim("p50 was 691.5us", cite="bench.log")
                    .render())
        adapter = load_adapter_class(ROOT / "channels", "fake")(auth={})
        with tempfile.TemporaryDirectory() as tmp:
            box = Outbox(Path(tmp) / "outbox.db", adapter, {"C_OPS": "direct"})
            try:
                receipt = box.send("C_OPS", "100.0", rendered)
                self.assertEqual(receipt["state"], "COMMITTED")
            finally:
                box.close()
        (channel, text, _key, _thread) = adapter.delivered[0]
        self.assertEqual(channel, "C_OPS")
        self.assertEqual(text, rendered)
        self.assertIn("(per bench.log)", text,
                      "the citation was stripped between composition and the wire")


if __name__ == "__main__":
    unittest.main(verbosity=2)
