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
import inspect
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.checks import Verdict, freshness_check  # noqa: E402
from core.classify import Taxonomy, classify  # noqa: E402
from core.config import discover_adapters  # noqa: E402
from core.journal import Journal  # noqa: E402
from core.outbox import Outbox  # noqa: E402
from core.owed import OwedRegistry  # noqa: E402
from core.store import Store  # noqa: E402

QUICKSTART = ROOT / "docs" / "QUICKSTART.md"
RUNBOOK = ROOT / "docs" / "RUNBOOK.md"
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

    def test_the_only_shipped_adapter_claim_matches_the_filesystem(self):
        """The limits section claims only `fake` ships. Check the claim against reality:
        when a real adapter lands, THIS test fails, forcing the limits section (and this
        assertion) to be updated in the same change — the doc cannot drift quietly."""
        self.assertIn("fake", self.limits,
                      "the limits section no longer names the fake adapter")
        shipped = set(discover_adapters(ROOT / "channels"))
        self.assertEqual(
            shipped, {"fake"},
            f"channels/ now ships {sorted(shipped)} but docs/QUICKSTART.md's honest-limits "
            "section still claims only 'fake' is implemented — rewrite the limits section, "
            "then update this assertion")


class DocCitationsTest(unittest.TestCase):
    """Paths and API names cited by the docs must exist. A doc that points at a file or
    method that is gone reads as authoritative and is worse than no doc at all."""

    def setUp(self):
        self.docs = {p: p.read_text() for p in (QUICKSTART, RUNBOOK)}

    def test_every_cited_repo_path_exists(self):
        path_like = re.compile(r"(scripts|tests|docs|channels|core)/[\w.\-/]+")
        offenders = []
        for doc, text in self.docs.items():
            for token in re.findall(r"`([^`]+)`", text):
                if path_like.fullmatch(token) and not (ROOT / token).exists():
                    offenders.append(f"{doc.name}: `{token}`")
        self.assertEqual(offenders, [],
                         "docs cite repo paths that do not exist:\n" + "\n".join(offenders))

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
        text = QUICKSTART.read_text()
        self.assertIn("python3 scripts/first-poll.py", text,
                      "the quickstart no longer walks the adopter through a first poll — "
                      "'first successful poll' is the doc's own stated target")
        self.assertIn("--seed-demo", text)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
