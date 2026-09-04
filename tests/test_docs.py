"""Adopter-documentation accuracy tests (gate G12, requirement R21).

Documentation is shipped interface: an adopter who cannot reach first poll forks or
abandons. But prose drifts from code silently — drift between a doc and a check is the
F-2 defect class this project exists to kill — so the docs are tested like code:

* the quickstart's HONEST LIMITS section must exist, AND its central claim (only the
  `fake` adapter ships) is checked against the filesystem, so landing a real adapter
  forces the limits section to be rewritten in the same change;
* every repo path and core API name the docs cite must resolve, so a rename goes red
  until the doc is updated;
* the first-poll step (scripts/first-poll.py) is EXECUTED against a temp-dir adopter
  config — "first successful poll" is a tested behaviour, not a promise.
"""
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.checks import Verdict, freshness_check  # noqa: E402
# _word_hits is the classifier's own definition of "occurs in the text" (word-boundary,
# case-insensitive). The step-6 overlap test uses it instead of a local regex because a
# second matcher here could drift from the real one and pass on text the classifier misses.
from core.classify import Taxonomy, _word_hits, classify  # noqa: E402
from core.config import discover_adapters  # noqa: E402
from core.journal import Journal  # noqa: E402
from core.outbox import Outbox  # noqa: E402
from core.owed import OwedRegistry  # noqa: E402
from core.store import Store  # noqa: E402

QUICKSTART = ROOT / "docs" / "QUICKSTART.md"
RUNBOOK = ROOT / "docs" / "RUNBOOK.md"
README = ROOT / "README.md"
CORE_README = ROOT / "core" / "README.md"
FIRST_POLL = ROOT / "scripts" / "first-poll.py"

# `obj.method(` citations in the docs resolve against these classes. The docs teach by
# runnable snippet, so a cited method that no longer exists teaches an adopter a lie.
CITED_OBJECTS = {"outbox": Outbox, "journal": Journal, "owed": OwedRegistry,
                 "store": Store}


def _section(text: str, heading: str) -> str:
    """The body of a `## heading`, up to the next `## ` or EOF. Empty if absent."""
    m = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", text,
                  re.MULTILINE | re.DOTALL | re.IGNORECASE)
    return m.group(1) if m else ""


class HonestLimitsTest(unittest.TestCase):
    """R21 acceptance: 'the limits section names what the engine does not do'."""

    def setUp(self):
        self.assertTrue(QUICKSTART.is_file(), "docs/QUICKSTART.md is missing")
        self.text = QUICKSTART.read_text()
        self.limits = _section(self.text, "Honest limits")

    def test_quickstart_has_an_honest_limits_section(self):
        self.assertTrue(self.limits.strip(),
                        "the quickstart has no '## Honest limits' section — an adopter "
                        "must be told what the engine does NOT do before they rely on it")

    def test_limits_name_concrete_non_capabilities(self):
        bullets = [l for l in self.limits.splitlines() if l.lstrip().startswith("- ")]
        self.assertGreaterEqual(
            len(bullets), 3,
            "the limits section must NAME the gaps, not gesture at them")
        for gap in ("scheduler", "parity"):
            self.assertIn(gap, self.limits.lower(),
                          f"a known, load-bearing limit ({gap}) vanished from the doc "
                          "while still being true")

    def test_the_shipped_adapter_claim_matches_the_filesystem(self):
        """The limits section claims exactly `fake` + `slack` + `slack_socket` +
        `telegram` + `email` ship. Check the claim against reality: when another
        adapter lands, THIS test fails, forcing the limits section (and this
        assertion) to be updated in the same change — the doc cannot drift quietly.
        (It fired exactly as designed when `slack` landed, again when `slack_socket`
        landed, again when `telegram` landed, and again when `email` landed.)"""
        self.assertIn("fake", self.limits,
                      "the limits section no longer names the fake adapter")
        self.assertIn("slack", self.limits,
                      "the limits section no longer names the slack adapter")
        self.assertIn("slack_socket", self.limits,
                      "the limits section no longer names the socket-mode adapter")
        self.assertIn("telegram", self.limits,
                      "the limits section no longer names the telegram adapter")
        self.assertIn("email", self.limits,
                      "the limits section no longer names the email adapter")
        self.assertIn("read-only", self.limits,
                      "the adapters' defining limit — read-only, no send path — "
                      "vanished from the doc while still being true")
        self.assertIn("push-poll-parity", self.limits,
                      "the socket adapter's defining limit — push can MISS events, so it "
                      "is only trustworthy under the continuous parity watch — vanished "
                      "from the doc while still being true")
        # \s+ joins because the doc is hard-wrapped (the ThreadPolicyDocTest lesson).
        self.assertRegex(
            self.limits, r"(?is)no\s+history\s+API",
            "telegram's defining limit vanished: bots cannot re-read acknowledged "
            "updates, and an adopter who does not know that will read the fail-closed "
            "parity verdict as a read-path defect — the exact R8 misreading")
        self.assertRegex(
            self.limits, r"(?is)permanently\s+fail-closed",
            "the doc no longer says parity against a Telegram store can never go "
            "green on deleted history — the one expectation that must be set before "
            "the first parity run, not after")
        self.assertRegex(
            self.limits, r"(?is)identity\s+is\s+the\s+Message-ID",
            "email's defining limit vanished: identity is a Message-ID string that "
            "no float() parses — an adopter who assumes an orderable ts will misread "
            "both the store's keys and parity's non-orderable verdicts")
        self.assertRegex(
            self.limits, r"(?is)non-orderable\s+identities",
            "the doc no longer says parity classifies email ids WITHOUT ordering "
            "them — the one expectation that keeps its unavailable window classes "
            "from being read as a defect")
        shipped = set(discover_adapters(ROOT / "channels"))
        self.assertEqual(
            shipped, {"fake", "slack", "slack_socket", "telegram", "email"},
            f"channels/ now ships {sorted(shipped)} but docs/QUICKSTART.md's honest-limits "
            "section still claims fake + slack (read-only) + slack_socket + telegram + "
            "email — rewrite the limits section, then update this assertion")

    def test_the_distribution_model_limit_states_the_exemption_and_the_risk(self):
        """ENH-15: since 2025-05-29 the platform throttles conversations.history and
        conversations.replies — the two calls every poll cycle depends on — for
        newly-created, commercially-distributed apps without Marketplace approval.
        Internal customer-built apps are exempt (the quickstart's target case), so the
        adopter must learn this BEFORE choosing a distribution model: discovered after,
        a throttled poller looks exactly like an engine defect and gets blamed as one
        (state/RESEARCH-INGESTION.md finding 2)."""
        for method in ("conversations.history", "conversations.replies"):
            self.assertIn(method, self.limits,
                          f"the limits section no longer names {method} — the exact "
                          "method the distribution-model limit throttles")
        self.assertIn("2025-05-29", self.limits,
                      "the platform change lost its date — 'recently' rots, the date "
                      "lets an adopter check the rule that applies to THEIR app")
        self.assertIn("Marketplace", self.limits,
                      "the limits section no longer says WHICH distribution model "
                      "triggers the reduced limits")
        # \s+ joins because the doc is hard-wrapped (the ThreadPolicyDocTest lesson):
        # any phrase may split across a line break without changing what it says.
        self.assertRegex(
            self.limits, r"(?is)internal\s+customer-built\s+apps[^.]{0,60}exempt",
            "the exemption vanished: an internal app — the quickstart's target case — "
            "is exempt, and the doc must say so or every internal adopter reads the "
            "limit as applying to them")
        self.assertRegex(
            self.limits, r"(?is)throttled\s+by\s+the\s+platform,\s+not\s+by\s+the\s+engine",
            "the risk lost its point: an adopter who goes commercial without "
            "Marketplace approval gets throttled on the poll path and, unwarned, "
            "blames the engine for the platform's limit")

    def test_the_parity_advice_names_the_shipped_differ(self):
        """ENH-22: the parity limit advises 'run both and diff' — and core/parity.py is
        a working CLI that does exactly that, yet no doc named it, so every adopter
        was sent off to write a differ the repo already ships."""
        self.assertIn("python3 -m core.parity", self.limits,
                      "the limits section tells the adopter to run both systems and "
                      "diff but never names the shipped differ — advice pointing at a "
                      "tool it hides is homework, not advice")


class DocCitationsTest(unittest.TestCase):
    """Paths and API names cited by the docs must exist. A doc that points at a file or
    method that is gone reads as authoritative and is worse than no doc at all."""

    def setUp(self):
        # README.md and core/README.md joined this set for ENH-19. The non-author
        # adoption run (2026-08-25) measured the root cause exactly: the two docs with
        # NO citation coverage were precisely the two that had drifted — core/README.md
        # listed six modules that never existed while the tested docs stayed accurate.
        self.docs = {p: p.read_text() for p in (QUICKSTART, RUNBOOK, README, CORE_README)}

    def test_every_cited_repo_path_exists(self):
        path_like = re.compile(r"(scripts|tests|docs|channels|core)/[\w.\-/]+")
        offenders = []
        for doc, text in self.docs.items():
            for token in re.findall(r"`([^`]+)`", text):
                if path_like.fullmatch(token) and not (ROOT / token).exists():
                    # relative_to(ROOT), not .name: both READMEs are named README.md.
                    offenders.append(f"{doc.relative_to(ROOT)}: `{token}`")
        self.assertEqual(offenders, [],
                         "docs cite repo paths that do not exist:\n" + "\n".join(offenders))

    def test_core_readme_cites_no_phantom_modules(self):
        """The adopter called core/README.md 'the single most misleading file in the
        repo': its module table cited engine/triggers/watchdog/supervisor/probe/
        dashboard — six files that never existed. Bare `name.py` citations there are
        module names by convention, so they resolve against core/ itself (the
        ROOT-relative check above cannot see them)."""
        offenders = [t for t in re.findall(r"`([\w\-]+\.py)`", self.docs[CORE_README])
                     if not (ROOT / "core" / t).is_file()]
        self.assertEqual(offenders, [],
                         "core/README.md cites modules that do not exist:\n"
                         + "\n".join(offenders))

    def test_every_core_module_appears_in_the_core_readme(self):
        """The reverse direction: at the same adoption run the table also OMITTED the
        implemented modules, so understating drift needs its own check. If the
        citation extractor above ever goes inert, this fails too (missing == all)."""
        cited = set(re.findall(r"`([\w\-]+\.py)`", self.docs[CORE_README]))
        # __init__.py is packaging, not a module with a responsibility to document.
        actual = {p.name for p in (ROOT / "core").glob("*.py")} - {"__init__.py"}
        missing = sorted(actual - cited)
        self.assertEqual(missing, [],
                         "core/ ships modules its own README never mentions:\n"
                         + "\n".join(missing))

    def test_every_cited_method_exists_on_its_class(self):
        cited, offenders = [], []
        for doc, text in self.docs.items():
            for obj, meth in re.findall(r"\b(outbox|journal|owed|store)\.([a-z_]+)\(", text):
                cited.append((doc.name, obj, meth))
                if not hasattr(CITED_OBJECTS[obj], meth):
                    offenders.append(f"{doc.name}: {obj}.{meth}() — no such method")
        # A regex that quietly stops matching would pass on zero citations — the inert-check
        # defect (Verdict refuses inspected=0) applied to this test itself.
        self.assertGreaterEqual(len(cited), 5,
                                "the citation extractor found almost nothing — it is broken, "
                                "not the docs")
        self.assertEqual(offenders, [],
                         "docs cite core APIs that do not exist:\n" + "\n".join(offenders))

    def test_taxonomy_guidance_names_its_placement(self):
        """The first non-author adoption run (2026-08-25) put "taxonomy" at the TOP level
        of settings.json, where the loader silently ignores it — the adopter believed the
        classifier was retuned when nothing had changed. The doc must state the placement,
        and the per-instance field it points at must still exist."""
        import dataclasses
        from core.config import InstanceConfig
        self.assertIn("per-instance", self.docs[QUICKSTART],
                      "the quickstart no longer says WHERE the taxonomy lives — the one "
                      "placement mistake a real adopter actually made")
        self.assertIn("taxonomy", {f.name for f in dataclasses.fields(InstanceConfig)},
                      "taxonomy is no longer a per-instance config field — rewrite the "
                      "quickstart's step 6, then this test")

    def test_runbook_helpers_exist_as_documented(self):
        runbook = self.docs[RUNBOOK]
        for name in ("freshness_check", "Verdict.passed", "Taxonomy.from_config",
                     "classify("):
            self.assertIn(name, runbook, f"runbook no longer documents {name} — if it was "
                                         "renamed, update the doc; if dropped, drop this")
        self.assertTrue(callable(classify) and callable(Taxonomy.from_config))
        self.assertTrue(callable(Verdict.passed))
        # The runbook tells operators to pass action_only_log for action-only sources.
        self.assertIn("action_only_log",
                      inspect.signature(freshness_check).parameters,
                      "runbook instructs passing action_only_log but freshness_check "
                      "no longer takes it")


class DoctorDocTest(unittest.TestCase):
    """ENH-5: the doctor exists because config failures surfaced as stack traces, but a
    preflight nobody is told about diagnoses nothing. It must be documented at both
    moments an adopter needs it: before the first real poll (quickstart) and at 2am
    when something is misconfigured (runbook)."""

    COMMAND = "python3 -m core.doctor --config settings.json"

    def test_the_preflight_command_is_documented_where_adopters_look(self):
        for doc in (QUICKSTART, RUNBOOK):
            self.assertIn(self.COMMAND, doc.read_text(),
                          f"{doc.name} no longer shows the doctor preflight command — "
                          "config failures go back to surfacing as stack traces")


class ReferenceSchedulerDocTest(unittest.TestCase):
    """ENH-6: the loop is where the incumbent's hardest bugs lived, and the reference
    scheduler only transfers those lessons if adopters are TOLD it exists and how to
    run it. The docs must show the command, and the 'no scheduler' framing that was
    true for months must not quietly return (the made-vague-again class)."""

    def test_the_quickstart_documents_the_run_command(self):
        text = QUICKSTART.read_text()
        self.assertIn("python3 scripts/scheduler.py --config settings.json", text,
                      "the quickstart never shows how to run the reference scheduler — "
                      "adopters go back to writing their own loop, bugs included")
        self.assertIn("--once", text,
                      "the cron-friendly single-cycle mode vanished from the doc — "
                      "overlapping cron fires racing one state dir is the exact "
                      "incident the guard exists for")

    def test_the_stale_no_scheduler_claim_cannot_return(self):
        self.assertNotIn("no scheduler in this repo", QUICKSTART.read_text(),
                         "the quickstart reverted to claiming no scheduler ships "
                         "while scripts/scheduler.py is implemented and tested")
        self.assertNotIn("no scheduler daemon", README.read_text(),
                         "README.md reverted to claiming no scheduler ships "
                         "while scripts/scheduler.py is implemented and tested")

    def test_the_runbook_covers_the_second_instance_refusal(self):
        """The one scheduler failure an operator WILL meet at 2am: exit 3, refused.
        The runbook must say what it means and must not teach the reflex fix —
        deleting the lock — that puts two loops on one state directory."""
        runbook = RUNBOOK.read_text()
        self.assertIn("scheduler.lock", runbook,
                      "the runbook never mentions the scheduler lock — the refusal "
                      "message points at a file the docs do not explain")
        self.assertRegex(runbook, r"(?is)never\s+delete\s+the\s+lock",
                         "the runbook no longer warns against deleting the lock — "
                         "that reflex is how two loops end up racing one cursor")


class FrontDoorTest(unittest.TestCase):
    """ENH-19: the front door must not UNDERSTATE what ships. The adoption run found a
    newcomer reading README.md concluded there was nothing to adopt — phases marked
    HELD, the quickstart gated on 'once phase 1 lands' — and never opened
    docs/QUICKSTART.md. Understating reality is the inverse of the drift this project
    guards against everywhere else, and it costs the same: the adopter leaves."""

    def setUp(self):
        self.readme = README.read_text()

    def test_front_door_links_the_quickstart(self):
        self.assertIn("docs/QUICKSTART.md", self.readme,
                      "README.md never points at docs/QUICKSTART.md — the measured "
                      "78-second first-poll path is unreachable from the front door")

    def test_front_door_names_every_shipped_adapter(self):
        # Reality-coupled like the honest-limits test: when a new adapter lands, this
        # goes red until the front door mentions it.
        shipped = set(discover_adapters(ROOT / "channels"))
        missing = sorted(a for a in shipped if a not in self.readme)
        self.assertEqual(missing, [],
                         "channels/ ships adapters README.md never names: "
                         + ", ".join(missing))

    def test_front_door_does_not_call_shipped_work_held(self):
        """The exact framing the adopter tripped over, pinned verbatim so it cannot
        quietly return (same pattern as the 'made vague again' doc mutations)."""
        for phrase in ("once phase 1 lands", "HELD"):
            self.assertNotIn(phrase, self.readme,
                             f"README.md reverted to phase-freeze framing ({phrase!r}) "
                             "while the code it gates is implemented and tested")
        core_readme = CORE_README.read_text()
        for phrase in ("HELD", "before any code exists"):
            self.assertNotIn(phrase, core_readme,
                             f"core/README.md claims its own contents are pending "
                             f"({phrase!r}) while core/ ships implemented, tested "
                             "modules")


class FirstPollTest(unittest.TestCase):
    """The quickstart's target is 'first successful poll'. Run that step for real:
    a temp adopter base, the shipped fake adapter, a settings.json with an env-ref
    credential — exactly the tree a fresh adopter has after quickstart steps 2-3."""

    INSTANCE, CHANNEL, ENV = "adoption-dry-run", "C_DEMO", "FIRST_POLL_TOKEN"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        shutil.copytree(ROOT / "channels" / "fake", self.base / "channels" / "fake",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (self.base / "settings.json").write_text(
            '{"engine": {"state_dir": "state"},\n'
            ' "instances": [{"name": "%s", "adapter": "fake",\n'
            '   "auth": {"token": "env:%s"},\n'
            '   "channels": [{"id": "%s", "label": "demo"}],\n'
            '   "principals": ["U_DEMO"]}]}\n' % (self.INSTANCE, self.ENV, self.CHANNEL))

    def tearDown(self):
        self.tmp.cleanup()

    def run_first_poll(self, *flags, with_token=True):
        env = {"PATH": os.environ["PATH"]}
        if with_token:
            env[self.ENV] = "dry-run-value-not-a-real-token"
        return subprocess.run(
            [sys.executable, str(FIRST_POLL),
             "--config", str(self.base / "settings.json"), *flags],
            capture_output=True, text=True, env=env)

    def journal_rows(self):
        j = Journal(self.base / "state" / "journal.db")
        try:
            return j.row_count()
        finally:
            j.close()

    def test_quickstart_documents_the_first_poll_step(self):
        # The FULL seeded command is the property: the real-poll step (R17) also invokes
        # first-poll.py, so a substring match would let the dry-run step vanish unnoticed.
        text = QUICKSTART.read_text()
        self.assertIn("python3 scripts/first-poll.py --config settings.json --seed-demo",
                      text,
                      "the quickstart no longer walks the adopter through the seeded "
                      "dry-run poll — 'first successful poll' is the doc's own stated "
                      "target, and it must work before any real credential exists")

    def test_first_poll_polls_classifies_journals_and_persists_the_cursor(self):
        r = self.run_first_poll("--seed-demo")
        self.assertEqual(r.returncode, 0, f"first poll failed:\n{r.stderr[-600:]}")
        self.assertIn("FIRST POLL OK", r.stdout)
        self.assertGreaterEqual(self.journal_rows(), 1,
                                "the demo message was polled but never journaled — the "
                                "journal row IS the proof the poll happened")
        s = Store(self.base / "state" / "messages.db")
        try:
            self.assertEqual(s.count(self.CHANNEL), 1)
            self.assertIsNotNone(s.cursor_get(self.INSTANCE, self.CHANNEL),
                                 "cursor not persisted — the next poll would re-read "
                                 "from the beginning of time")
        finally:
            s.close()

    def test_the_journaled_row_carries_the_audit_link(self):
        """R22: first-poll is the one caller that stands between the classifier and the
        journal; if it drops the cues there, every adopter's audit trail starts broken."""
        self.run_first_poll("--seed-demo")
        j = Journal(self.base / "state" / "journal.db")
        try:
            a = j.audit(self.CHANNEL, "1.0")
            self.assertIsNotNone(a, "the demo message was never journaled")
            self.assertTrue(a["reason"])
            self.assertTrue(a["matched"],
                            "the decision cues never reached the journal — the "
                            "classification cannot be disputed from the audit trail")
        finally:
            j.close()

    def test_the_journaled_row_records_the_classifiers_confidence(self):
        """ENH-9: first-poll is the other caller between the classifier and the journal
        (the ENH-4 attachments lesson); if it drops the signal there, every first-poll
        row reads as never-classified and the adopter's hedge count starts broken. The
        demo message is a confident QUESTION, so the row must say False — recorded
        confidence, never the unrecorded NULL."""
        self.run_first_poll("--seed-demo")
        j = Journal(self.base / "state" / "journal.db")
        try:
            a = j.audit(self.CHANNEL, "1.0")
            self.assertIsNotNone(a, "the demo message was never journaled")
            self.assertIs(a["ambiguous"], False,
                          "the confidence signal never reached the journal row — "
                          "ambiguity_stats() cannot count what first-poll drops")
        finally:
            j.close()

    def test_a_second_poll_is_idempotent(self):
        self.run_first_poll("--seed-demo")
        before = self.journal_rows()
        r = self.run_first_poll()
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertEqual(self.journal_rows(), before,
                         "re-running first-poll duplicated journal rows — replay must "
                         "be safe or an adopter's second command destroys trust")

    def test_missing_env_var_is_refused_loudly(self):
        r = self.run_first_poll("--seed-demo", with_token=False)
        self.assertNotEqual(r.returncode, 0,
                            "the engine started half-configured — the quickstart promises "
                            "it refuses")
        self.assertIn(self.ENV, r.stderr, "the refusal must NAME the missing variable")

    def test_first_poll_has_no_send_path(self):
        """Polling is first contact and must be provably read-only: the script may not
        even import the send layer, so no bug in it can post as anyone."""
        import ast
        tree = ast.parse(FIRST_POLL.read_text())
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        self.assertFalse([m for m in imported if "outbox" in m],
                         "first-poll.py imports the send layer — first contact must be "
                         "read-only by construction")


class AttachmentBoundaryTest(unittest.TestCase):
    """ENH-4 at the caller boundary — the same seam where the R22 cues once died:
    first-poll stands between the adapter's normalized message and the classifier, and
    a classify() call that passes only msg['text'] silently re-drops every attachment
    however well both ends handle them. The journal row is the observable: an
    image-only message must land as an explicit ATTACHMENT-ONLY decision, not as the
    empty STATEMENT the live system acknowledged and forgot."""

    def journal_message(self):
        spec = importlib.util.spec_from_file_location("first_poll_enh4_test", FIRST_POLL)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.journal_message

    def test_an_image_only_message_reaches_the_journal_as_attachment_only(self):
        image_only = {"channel_type": "fake", "channel_id": "C_DEMO",
                      "sender_id": "U_DEMO", "ts": "1.0", "text": "",
                      "attachments": [{"kind": "image", "name": "screenshot.png",
                                       "mimetype": "image/png",
                                       "url": "https://files.example/x.png"}]}
        with tempfile.TemporaryDirectory() as tmp:
            j = Journal(Path(tmp) / "journal.db")
            try:
                self.journal_message()(j, "C_DEMO", image_only, Taxonomy())
                a = j.audit("C_DEMO", "1.0")
            finally:
                j.close()
        self.assertEqual(a["kind"], "ATTACHMENT-ONLY",
                         "the attachments never reached the classifier — the polled "
                         "screenshot journals as an empty STATEMENT again")
        self.assertIn("image:screenshot.png", a["matched"],
                      "the decision does not name the attachment it saw — it cannot "
                      "be disputed from the audit trail (R22)")


class RealPollDocTest(unittest.TestCase):
    """R17: the quickstart must walk the adopter past the fake dry-run to THEIR real
    workspace — config alone, no code edits. The fake-adapter step proves the pipeline;
    only a documented real-poll step makes the repo adoptable, and its stated auth
    contract must match what the adapter actually refuses to start without."""

    def setUp(self):
        self.text = QUICKSTART.read_text()
        m = re.search(r"^## .*real poll.*$(.*?)(?=^## |\Z)", self.text,
                      re.MULTILINE | re.DOTALL | re.IGNORECASE)
        self.section = m.group(0) if m else ""

    def test_quickstart_walks_the_adopter_to_a_real_poll(self):
        self.assertTrue(self.section.strip(),
                        "the quickstart has no real-poll step — it leaves the adopter at "
                        "the fake adapter, so 'adopt by config alone' stops one step "
                        "short of their own workspace")
        self.assertIn("slack", self.section.lower())
        self.assertIn("read-only", self.section.lower(),
                      "the real-poll step must restate read-only where the adopter acts, "
                      "not only in the limits section")
        self.assertIn("scripts/first-poll.py", self.section,
                      "the real poll must reuse the same observable first-poll cycle, "
                      "not a second undocumented entry point")

    def test_documented_auth_contract_matches_the_adapter(self):
        """Drift-proofing: the section must name every auth key the slack adapter
        refuses to start without. Probe the adapter itself so a changed requirement
        goes red here until the doc names it too."""
        from core.config import load_adapter_class
        cls = load_adapter_class(ROOT / "channels", "slack")
        with self.assertRaises(ValueError) as ctx:
            cls(auth={})
        self.assertIn("token", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            cls(auth={"token": "tok"})
        self.assertIn("channels", str(ctx.exception))
        for key in ("token", "channels"):
            # The env-reference form is part of the assertion: showing the key with a
            # literal value would teach exactly what the loader refuses.
            self.assertIn(f'"{key}": "env:', self.section,
                          f"the slack adapter refuses to start without auth[{key!r}] "
                          "but the real-poll step's config example never shows it as "
                          "an env: reference")

    def test_the_two_place_channel_rule_is_stated(self):
        """The one silent failure on this path: the adapter polls the ids in the auth
        env list, the engine keeps only ids listed under channels[] — a mismatch is a
        successful-looking poll of nothing. The live bring-up (state evidence,
        2026-08-26) needed the id in both places; the doc must say so."""
        self.assertIn("two places", self.section,
                      "the channel id lives in TWO places (auth env list + channels[]) "
                      "and a mismatch polls nothing, silently — the doc no longer warns "
                      "about the one quiet failure on the real-poll path")


class TaxonomyTuningStepTest(unittest.TestCase):
    """ENH-23: step 6's verification instruction must be followable AS WRITTEN. The
    adopter re-run at 0447ca4 followed it literally and watched nothing move, for two
    independent doc-side reasons: the example vocabulary (provision/deploy/roll back)
    never occurred in the shipped demo text, so `kind` could not change; and a plain
    re-run re-seeds the SAME ts behind a persisted cursor, so there was no fresh demo
    message at all (`0 polled` — which the RUNBOOK's 0-messages entry correctly calls
    working-as-designed, directly contradicting the old step 6). Both are pinned here
    against the real demo text and the real classifier, not against copies."""

    def setUp(self):
        text = QUICKSTART.read_text()
        m = re.search(r"^## 6\..*?$(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(m, "the quickstart lost its step-6 classifier-tuning step")
        self.section = m.group(0)

    def step6_taxonomy(self):
        m = re.search(r"```json\s*(.*?)```", self.section, re.DOTALL)
        self.assertIsNotNone(m, "step 6 no longer shows a taxonomy config example")
        # The snippet is an instance fragment, exactly as it would sit in settings.json.
        cfg = json.loads("{" + m.group(1) + "}")
        return cfg["instances"][0]["taxonomy"]

    def demo_text(self):
        """The shipped demo message, taken from the code that plants it. A literal copy
        here would be a third place for the same string to drift."""
        spec = importlib.util.spec_from_file_location("first_poll_step6_test", FIRST_POLL)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        inst = types.SimpleNamespace(adapter="fake",
                                     channels=[types.SimpleNamespace(id="C_DEMO")])
        return mod.demo_messages(inst)[0]["text"]

    def test_step6_example_verbs_intersect_the_demo_text(self):
        """Failure reason (b) of the adopter re-run: with no overlap between the example
        exec_verbs and the demo text, 'confirm the kind moved' was unfollowable even
        with fresh state."""
        hits = _word_hits(self.demo_text(), self.step6_taxonomy()["exec_verbs"])
        self.assertTrue(hits,
                        "no verb in step 6's exec_verbs example occurs in the shipped "
                        "demo message — the step's own verification ('confirm the kind "
                        "moved') cannot be followed with the doc's example config")

    def test_step6_promised_kind_move_happens_on_the_shipped_demo_message(self):
        """The observation step 6 promises must actually happen, and the doc must name
        both kinds so a classifier-default change forces the prose to move with it."""
        demo = self.demo_text()
        before = classify(demo).kind
        after = classify(demo, Taxonomy.from_config(self.step6_taxonomy())).kind
        self.assertNotEqual(before, after,
                            f"the example taxonomy leaves the demo message at {before!r} "
                            "— retuning changes nothing observable, which is exactly the "
                            "adopter-measured defect")
        for kind in (before, after):
            self.assertIn(kind, self.section,
                          f"step 6 never states the expected observation ({before} -> "
                          f"{after}) — 'the way you expected' gives the reader nothing "
                          "to check against")

    def test_step6_says_how_to_get_a_fresh_demo_message(self):
        """Failure reason (a): --seed-demo re-plants the same ts and the cursor is
        already past it, so a plain re-run polls nothing. Deletion is the instruction
        because it is the one that works with the shipped example config: the example
        pins `store` under state/ explicitly, so moving state_dir alone leaves the
        cursor behind (measured live, scratch/enh23-step6)."""
        self.assertIn("Delete the state directory", self.section,
                      "step 6 no longer tells the reader to get fresh state before "
                      "re-polling — a plain re-run reports '0 polled' by construction, "
                      "and the RUNBOOK even documents that as correct")
        self.assertIn("--seed-demo", self.section,
                      "step 6 no longer says the verification re-run needs --seed-demo "
                      "— an unseeded fresh-state poll shows an empty channel, not a "
                      "reclassified message")


    def test_step6_documents_the_hedge_signal_and_its_escalation_flag(self):
        """ENH-9: the flag and the count live where the adopter is already tuning the
        classifier — an undocumented config key is inert in practice however loudly
        the loader would refuse its typo. Both doc claims are held against the code
        (the placement lesson from test_taxonomy_guidance_names_its_placement): the
        key must be a real per-instance field and the count a real Journal method."""
        import dataclasses
        from core.config import InstanceConfig
        self.assertIn('"escalate_ambiguous": true', self.section,
                      "step 6 no longer shows the exact key that routes hedged "
                      "decisions to a human — nobody can opt in to a flag the docs "
                      "never name")
        self.assertIn("escalate_ambiguous",
                      {f.name for f in dataclasses.fields(InstanceConfig)},
                      "the documented key is not a per-instance config field — the "
                      "doc names a phantom")
        self.assertIn("ambiguity_stats", self.section,
                      "step 6 no longer says where the hedge count lives — the signal "
                      "is invisible again for anyone who has not read core/journal.py")
        self.assertTrue(hasattr(Journal, "ambiguity_stats"),
                        "the documented count surface does not exist on Journal")


class RunbookConstructorTest(unittest.TestCase):
    """ENH-22: every runbook snippet calls journal./outbox./owed. methods, and until
    this section existed the only import the whole RUNBOOK showed was core.classify —
    a 2am operator had to source-dive for every constructor (measured: the adopter
    re-run at 0447ca4 could instantiate Journal only from remembered knowledge of a
    prior run). The doc block is EXECUTED here, as pasted, against a real adopter
    tree, and then the runbook's own snippets are run on the objects it built —
    prose about construction would drift; a block that must run cannot."""

    INSTANCE, CHANNEL, ENV = "runbook-2am", "C_DEMO", "RUNBOOK_TOKEN"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        shutil.copytree(ROOT / "channels" / "fake", self.base / "channels" / "fake",
                        ignore=shutil.ignore_patterns("__pycache__"))
        # A 2am operator inspects a running engine's EXISTING state directory.
        (self.base / "state").mkdir()
        (self.base / "settings.json").write_text(
            '{"engine": {"state_dir": "state"},\n'
            ' "instances": [{"name": "%s", "adapter": "fake",\n'
            '   "auth": {"token": "env:%s"},\n'
            '   "channels": [{"id": "%s", "label": "demo",'
            ' "reply_policy": "staged"}],\n'
            '   "principals": ["U_DEMO"]}]}\n' % (self.INSTANCE, self.ENV, self.CHANNEL))

    def tearDown(self):
        self.tmp.cleanup()

    def constructor_block(self):
        for block in re.findall(r"```python\s*(.*?)```", RUNBOOK.read_text(), re.DOTALL):
            if "cfg = load(" in block:
                return block
        self.fail("the RUNBOOK never shows how to construct the objects its snippets "
                  "call — every diagnostic on the page needs a source dive first")

    # The runbook's own snippets, verbatim method-for-method, appended to the doc's
    # constructor block. STAGED policy so the adapter is never sent through.
    SNIPPETS = (
        '\nout = []\n'
        'out.append(("policy", outbox.policy_for("C_DEMO"),'
        ' outbox.policy_for("C_DEMO", "thread")))\n'
        'outbox.send("C_DEMO", "9.9", "[AGENT] draft for the operator gate")\n'
        'print("DRAFTS:", outbox.staged())\n'
        'print("RECOVER:", outbox.recover())\n'
        'journal.record("C_DEMO", "9.9", text="Please deploy the fix.",'
        ' kind="EXEC-REQUEST", reason="imperative", matched=["deploy", "please"])\n'
        'print("AUDIT:", journal.audit("C_DEMO", "9.9"))\n'
        'print("REVISIONS:", journal.revisions("C_DEMO", "9.9"))\n'
        'print("UNANSWERED:", len(journal.unanswered("C_DEMO")))\n'
        'print("COUNTS:", journal.row_count(), journal.distinct_count())\n'
        'print("UNATTENDED:", len(owed.unattended()))\n'
        'print("STORE:", store.count("C_DEMO"))\n'
        'print("RUNBOOK-SNIPPETS-OK")\n'
    )

    def test_the_documented_construction_runs_as_pasted(self):
        r = subprocess.run(
            [sys.executable, "-c", self.constructor_block() + self.SNIPPETS],
            cwd=self.base, capture_output=True, text=True,
            env={"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT),
                 self.ENV: "dry-run-value-not-a-real-token"})
        self.assertEqual(r.returncode, 0,
                         "the RUNBOOK's constructor block does not run as pasted:\n"
                         + r.stderr[-800:])
        self.assertIn("RUNBOOK-SNIPPETS-OK", r.stdout)

    def test_what_the_snippets_print_is_readable(self):
        """The other half of the ergonomics defect: constructed or not, staged() and
        revisions() used to PRINT as '<sqlite3.Row object at 0x...>' — the draft text
        the operator must gate and the edit history under dispute were invisible."""
        r = subprocess.run(
            [sys.executable, "-c", self.constructor_block() + self.SNIPPETS],
            cwd=self.base, capture_output=True, text=True,
            env={"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT),
                 self.ENV: "dry-run-value-not-a-real-token"})
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        self.assertIn("draft for the operator gate", r.stdout,
                      "outbox.staged() printed no draft text — the operator gate "
                      "is unreadable exactly where the RUNBOOK points a human at it")
        self.assertIn("Please deploy the fix.", r.stdout,
                      "journal.revisions() printed no message text — the audit walk "
                      "shows object addresses instead of the disputed history")


class GatesCheckDocTest(unittest.TestCase):
    """ENH-22: the runbook's hook health check was `git config core.hooksPath`, which
    on a correctly-installed clone prints NOTHING and exits 1 — the healthy result
    read as a failure at exactly the moment someone was doubting the gates."""

    def section(self):
        s = _section(RUNBOOK.read_text(), '"Are the gates actually running?"')
        self.assertTrue(s.strip(), "the runbook lost its gates-health section")
        return s

    def test_the_hook_check_shows_output_on_a_healthy_clone(self):
        s = self.section()
        self.assertIn("ls .git/hooks/pre-commit .git/hooks/pre-push", s,
                      "the gates section lost the hook check that PRINTS on a "
                      "healthy clone")
        self.assertNotRegex(s, r"git config core\.hooksPath\s*(;|\n|$)",
                            "the silent-on-healthy form is back as the check: "
                            "`git config core.hooksPath` prints nothing and exits 1 "
                            "on a correctly-installed clone")
        self.assertRegex(s, r"(?i)healthy:",
                         "the checks no longer state what their healthy output looks "
                         "like — output with no stated expectation cannot be judged")

    def test_the_stated_expectation_holds_on_this_clone(self):
        if not (ROOT / ".git").is_dir():
            self.skipTest("not a git clone (mutation sandbox); the content "
                          "assertions above carry the property there")
        for hook in ("pre-commit", "pre-push"):
            self.assertTrue(
                (ROOT / ".git" / "hooks" / hook).is_file(),
                f"the runbook promises `ls .git/hooks/{hook}` prints on a healthy "
                "clone, but this clone has no such hook — run "
                "scripts/install-hooks.sh or fix the doc")


class ThreadPolicyDocTest(unittest.TestCase):
    """ENH-3: the placement policy is only expressible if an adopter can find it.

    The quickstart's example channel is LOADED through the real config loader and its
    resolved policy driven through the real per-scope resolution, so a renamed key or a
    changed default goes red here — a documented setting that silently does nothing is
    worse than an undocumented one, because the adopter believes the main channel is
    protected.
    """

    def setUp(self):
        self.text = QUICKSTART.read_text()
        # Numbered headings ("## 4. Understand the reply policy ...") do not match
        # _section's exact-heading form, and the steps get renumbered as steps land.
        self.section = self._step("reply policy")
        self.edit_step = self._step("Copy the example config")

    def _step(self, phrase):
        # [^\n]* for the heading line, not .* — under DOTALL a greedy .* swallows the
        # rest of the file, and a section that is secretly the whole document passes
        # every assertion about it (this exact bug made a doc mutation survive once).
        m = re.search(rf"^## [^\n]*{re.escape(phrase)}[^\n]*$(.*?)(?=^## |\Z)", self.text,
                      re.MULTILINE | re.DOTALL | re.IGNORECASE)
        return m.group(0) if m else ""

    def documented_channel(self):
        """The one-line JSON channel object the quickstart shows for this policy."""
        for block in re.findall(r"```json\s*(.*?)```", self.section, re.DOTALL):
            if "thread_reply_policy" in block:
                return json.loads(block)
        self.fail("the reply-policy step shows no json channel example carrying "
                  "thread_reply_policy — an adopter cannot express 'answer in thread, "
                  "never the main channel' from prose alone")

    def test_the_documented_example_loads_and_scopes_the_policy(self):
        from core.config import from_dict
        cfg = from_dict({"engine": {}, "instances": [
            {"name": "t", "adapter": "fake",
             "channels": [self.documented_channel()]}]}, base_dir=ROOT)
        ch = cfg.instance("t").channels[0]
        policies = cfg.instance("t").policies()
        with tempfile.TemporaryDirectory() as tmp:
            box = Outbox(Path(tmp) / "outbox.db", None, policies)
            self.assertEqual(box.policy_for(ch.id, "thread"), ch.thread_reply_policy)
            self.assertEqual(box.policy_for(ch.id, "channel"), ch.reply_policy)
            box.close()

    def test_the_documented_example_leaves_the_main_channel_refusing(self):
        """The whole point of the example: the channel scope must still be 'never'."""
        self.assertEqual(self.documented_channel().get("reply_policy"), "never",
                         "the 'answer in thread, never the main channel' example no "
                         "longer shows the main channel as never — copied as-is it "
                         "would post top-level in a channel the adopter thought was "
                         "read-only")

    def test_the_step_says_each_scope_is_deny_by_default(self):
        self.assertIn("deny-by-default", self.section.lower())
        # \s+ between words: the doc is hard-wrapped, so any of these phrases can be
        # split across a line break at any time without changing what it says.
        self.assertRegex(
            self.section,
            r"(?is)naming\s+one\s+placement\s+does\s+not\s+promote\s+the\s+other|"
            r"each\s+scope\s+is\s+deny-by-default\s+on\s+its\s+own",
            "the step no longer states that naming one placement leaves the other "
            "denied — an adopter who sets only thread_reply_policy would reasonably "
            "assume the main channel inherited it")

    def test_the_runbook_diagnostic_asks_about_the_refused_placement(self):
        """The "it is not sending anything" section is where an operator lands when a
        thread reply was refused. A one-argument policy_for() answers for the main
        channel, so on a thread-scoped channel it reports the opposite of the truth —
        and this doc is the incumbent's own debugging habit written down."""
        runbook = RUNBOOK.read_text()
        m = re.search(r'outbox\.policy_for\("[^"]+",\s*"thread"\)', runbook)
        self.assertTrue(m, "the runbook's policy diagnostic never asks about a thread, "
                           "so an operator debugging a refused thread reply reads the "
                           "channel's policy and concludes the wrong thing")
        with tempfile.TemporaryDirectory() as tmp:
            box = Outbox(Path(tmp) / "outbox.db", None,
                         {"C": {"channel": "never", "thread": "direct"}})
            # The documented call must genuinely distinguish the two placements.
            self.assertEqual(box.policy_for("C"), "never")
            self.assertEqual(box.policy_for("C", "thread"), "direct")
            box.close()

    def test_the_config_key_is_listed_where_the_adopter_edits_the_file(self):
        """Step 2's table is the checklist an adopter edits against; a policy that only
        appears in later prose is a policy most adopters never see."""
        self.assertIn("thread_reply_policy", self.edit_step,
                      "the placement policy is missing from the step-2 field table an "
                      "adopter edits against, so most adopters never learn it exists")


class ConfigSurfaceDocTest(unittest.TestCase):
    """ENH-29 — the quickstart's config-management paragraph must keep stating the
    three facts an operator acts on, and each must stay true of the code it
    describes. Each has a mutation in tests/mutation_check.sh."""

    def setUp(self):
        self.quickstart = QUICKSTART.read_text()

    def test_the_reload_truth_is_stated_and_matches_the_code(self):
        """'Applied' without the restart caveat reads as 'live everywhere' — false
        for a running scheduler/watcher, which loads settings once at startup. The
        doc and core/reconfig.RELOAD_TRUTH must keep saying the same thing."""
        self.assertIn("loads settings only at startup", self.quickstart)
        self.assertIn("must be restarted", self.quickstart)
        from core.reconfig import RELOAD_TRUTH
        for word in ("restart", "startup", "hot-reload"):
            self.assertIn(word, RELOAD_TRUTH,
                          "the code's own reload message dropped the truth the doc "
                          "promises it states")

    def test_the_never_default_and_the_widening_flag_are_stated(self):
        self.assertIn("`reply_policy: never` (deny) **by omission**", self.quickstart,
                      "the doc no longer says a new channel denies by default — the "
                      "one property that makes UI-created channels safe to create")
        self.assertIn("flagged in red", self.quickstart,
                      "the doc no longer promises the widening flag, so an operator "
                      "has no reason to look for it before clicking Apply")

    def test_the_secret_handling_claim_is_stated(self):
        self.assertIn("never its value", self.quickstart,
                      "the doc stopped stating that env values never render — the "
                      "claim tests/test_dashboard_config.py holds the surface to")


if __name__ == "__main__":
    unittest.main(verbosity=2)
