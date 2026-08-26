"""Operator dashboard tests (ENH-8).

The dashboard UI lived in spoke scratch reading THAT host's monitor catalog, so an
adopter cloning this repo got no operator surface at all. The port inverts the data
source: the surface is built from the files the engine actually maintains for an
adopter — THEIR journal.db and THEIR per-instance outboxes — resolved from THEIR
settings.json, never from anything host-specific.

Two properties carry the whole feature:

* **It is a viewer.** An operator surface that can alter the audit trail it displays
  is one bug away from editing it, so every connection is sqlite `mode=ro` and the
  send layer is never imported. These tests write through the REAL Journal/Outbox
  modules and require the dashboard to read the result without changing a byte.
* **Attention first, severity ordered.** The spoke UI's one validated UX lesson: what
  needs action is stated before anything scrollable. A send that may have died
  mid-flight outranks everything (only recover() can tell "sent, unrecorded" from
  "never sent"); then answers invalidated by a later edit; then drafts waiting at the
  operator gate; then the unanswered backlog.

Layering mirrors that split: core/dashboard.py is the stdlib-only read layer any UI
can render (tested here with no third-party imports); scripts/dashboard.py is the
Streamlit shell, tested through streamlit's own AppTest harness where streamlit is
installed and skipped honestly where it is not — the data layer's properties hold
either way.
"""
import ast
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.dashboard import DashboardError, open_ro, snapshot  # noqa: E402
from core.journal import Journal  # noqa: E402
from core.outbox import Outbox, _Crash  # noqa: E402

SCRIPT = ROOT / "scripts" / "dashboard.py"
SERVE = ROOT / "scripts" / "dashboard-serve.sh"

try:
    from streamlit.testing.v1 import AppTest
    HAVE_STREAMLIT = True
except ImportError:                                    # pragma: no cover
    HAVE_STREAMLIT = False

ASK = "Please review the rollout plan before Thursday."
DRAFT = "draft reply: rollout numbers attached for your approval"
EDIT_V1 = "Notes from the meeting are in the doc."
EDIT_V2 = "Please deploy the patched image now."


class FakeAdapter:
    """Contract-shaped adapter (delivers + proves by read-back), like test_portability's."""

    def __init__(self):
        self.delivered = []

    def send(self, target, text, key=None, thread_id=None):
        self.delivered.append((target, text, key))
        return {"ts": "r1", "key": key}

    def read_back(self, target, key):
        return any(t == target and k == key for t, _, k in self.delivered)


def seed_journal(path):
    """One unanswered ask, one answered ask, one answer invalidated by a later edit."""
    j = Journal(path)
    j.record("C_TEAM", "1.0", sender_id="U1", text=ASK, kind="QUESTION")
    j.record("C_TEAM", "2.0", sender_id="U1", text="Ship it.", kind="EXEC-REQUEST")
    j.mark_responded("C_TEAM", "2.0", "outbox-key-answered")
    j.record("C_TEAM", "3.0", sender_id="U1", text=EDIT_V1, kind="STATEMENT")
    j.mark_responded("C_TEAM", "3.0", "outbox-key-stale")
    j.record("C_TEAM", "3.0", sender_id="U1", text=EDIT_V2, kind="EXEC-REQUEST")
    j.close()


def seed_outbox(path):
    """One row per ladder state that matters to an operator: STAGED (gate), INTENT and
    SENT (both crash seams, via the fault harness's own _crash_at), COMMITTED (done)."""
    ob = Outbox(path, FakeAdapter(),
                {"C_CUST": "staged", "C_TEAM": "direct"}, send_interval=0.0)
    ob.send("C_CUST", "10.0", DRAFT)
    ob.send("C_TEAM", "11.0", "answered in channel")
    try:
        ob.send("C_TEAM", "12.0", "died before the adapter was called",
                _crash_at="after_intent")
    except _Crash:
        pass
    try:
        ob.send("C_TEAM", "13.0", "died between send and read-back",
                _crash_at="before_readback")
    except _Crash:
        pass
    ob.close()


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.journal = self.base / "journal.db"
        self.outbox = self.base / "outbox-team.db"
        seed_journal(self.journal)
        seed_outbox(self.outbox)

    def snap(self):
        return snapshot(self.journal, {"team": self.outbox})

    def by_severity(self, snap, severity):
        return [i for i in snap["attention"] if i["severity"] == severity]

    def test_the_attention_queue_is_severity_ordered(self):
        """The explicit order, NOT derived from the module's own constant — a reordered
        constant must fail here, not re-derive the expectation."""
        sevs = [i["severity"] for i in self.snap()["attention"]]
        self.assertEqual(sevs, sorted(sevs, key=["in_flight", "edited_after_response",
                                                 "staged", "unanswered"].index))
        self.assertEqual(sevs[0], "in_flight",
                         "a send that may have died mid-flight is the one item that "
                         "can double-message someone — nothing outranks it")
        self.assertEqual(sevs[-1], "unanswered")

    def test_in_flight_is_exactly_the_intent_and_sent_rows(self):
        """COMMITTED is done and STAGED is a different queue; surfacing either as a
        crashed send would train the operator to ignore the banner."""
        states = sorted(i["state"] for i in self.by_severity(self.snap(), "in_flight"))
        self.assertEqual(states, ["INTENT", "SENT"])
        for item in self.by_severity(self.snap(), "in_flight"):
            self.assertIn("recover", item["why"],
                          "the item must name the one safe next action — only "
                          "recovery's read-back can tell 'sent, unrecorded' from "
                          "'never sent'")

    def test_a_staged_draft_surfaces_with_its_text_and_target(self):
        staged = self.by_severity(self.snap(), "staged")
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0]["where"], "C_CUST")
        self.assertEqual(staged[0]["instance"], "team")
        self.assertEqual(staged[0]["text"], DRAFT,
                         "the gate is an approval decision — the operator approves "
                         "the exact text or nothing")

    def test_an_edit_after_our_answer_is_surfaced_with_the_new_text(self):
        edited = self.by_severity(self.snap(), "edited_after_response")
        self.assertEqual([(i["where"], i["ts"]) for i in edited], [("C_TEAM", "3.0")])
        self.assertEqual(edited[0]["text"], EDIT_V2,
                         "the operator needs the text that made the old answer stale, "
                         "not the text we already answered")

    def test_unanswered_shows_the_open_ask_and_not_the_answered_one(self):
        open_asks = self.by_severity(self.snap(), "unanswered")
        self.assertIn(("C_TEAM", "1.0"), [(i["where"], i["ts"]) for i in open_asks])
        self.assertNotIn(("C_TEAM", "2.0"), [(i["where"], i["ts"]) for i in open_asks])

    def test_counts_come_from_the_files_not_from_the_attention_queue(self):
        s = self.snap()
        self.assertEqual(s["journal"]["distinct"], 3)
        self.assertEqual(s["journal"]["unanswered"], 1)
        self.assertEqual(s["journal"]["answered"], 2)
        self.assertEqual(s["outbox"]["team"],
                         {"STAGED": 1, "COMMITTED": 1, "INTENT": 1, "SENT": 1})

    def test_missing_state_is_reported_never_created(self):
        """A fresh clone has no journal and no outbox. The dashboard must SAY so —
        an absent file rendered as healthy zeros is the F-2 false-confidence shape —
        and must not mint empty databases while looking."""
        empty = self.base / "elsewhere"
        empty.mkdir()
        s = snapshot(empty / "journal.db", {"team": empty / "outbox-team.db"})
        self.assertEqual(s["attention"], [])
        self.assertEqual(len(s["missing"]), 2)
        self.assertIsNone(s["journal"],
                          "no journal must read as UNKNOWN, never as zero messages")
        self.assertIsNone(s["outbox"]["team"])
        self.assertEqual(list(empty.iterdir()), [],
                         "the viewer created state while reporting it missing")


class ReadOnlyTest(unittest.TestCase):
    """The property that makes this a viewer rather than an editor."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.journal = self.base / "journal.db"
        seed_journal(self.journal)

    def test_the_connection_the_dashboard_hands_out_refuses_writes(self):
        conn = open_ro(self.journal)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("DELETE FROM journal")

    def test_a_missing_file_is_refused_by_name_and_not_created(self):
        ghost = self.base / "nope.db"
        with self.assertRaises(DashboardError) as ctx:
            open_ro(ghost)
        self.assertIn(str(ghost), str(ctx.exception))
        self.assertFalse(ghost.exists(),
                         "opening a missing database created it — sqlite's default "
                         "connect does exactly this, which is why mode=ro is load-"
                         "bearing")

    def test_a_snapshot_leaves_the_state_byte_identical(self):
        outbox = self.base / "outbox-t.db"
        seed_outbox(outbox)
        before = (self.journal.read_bytes(), outbox.read_bytes())
        snapshot(self.journal, {"t": outbox})
        self.assertEqual((self.journal.read_bytes(), outbox.read_bytes()), before)


class DashboardScriptTest(unittest.TestCase):
    """Static properties of the Streamlit shell — checkable without streamlit."""

    def test_the_script_never_imports_the_send_layer(self):
        """Same discipline as scripts/scheduler.py: a surface that cannot even import
        core.outbox cannot post as anyone, whatever bug it grows."""
        tree = ast.parse(SCRIPT.read_text())
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        self.assertFalse([m for m in imported if "outbox" in m],
                         "scripts/dashboard.py imports the send layer — the operator "
                         "surface must stay a viewer")

    def test_the_serve_wrapper_binds_loopback_only(self):
        """HARD security bound: services bind 127.0.0.1 only; remote operators come in
        over an SSH tunnel. One flag away is a UI with journal text on the open port."""
        text = SERVE.read_text()
        self.assertIn("--server.address 127.0.0.1", text)
        self.assertNotIn("0.0.0.0", text)


def seeded_adopter_base(tmp):
    """The tree quickstart steps 2-5 leave behind: shipped fake adapter, a
    settings.json, and state written by the engine's own modules."""
    base = Path(tmp)
    shutil.copytree(ROOT / "channels" / "fake", base / "channels" / "fake",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (base / "settings.json").write_text(
        '{"engine": {"state_dir": "state"},\n'
        ' "instances": [{"name": "team", "adapter": "fake",\n'
        '   "channels": [{"id": "C_TEAM", "reply_policy": "direct"},\n'
        '                {"id": "C_CUST", "reply_policy": "staged"}]}]}\n')
    return base


@unittest.skipUnless(HAVE_STREAMLIT, "streamlit not installed — the data layer's "
                     "tests above still guard every property the UI renders")
class DashboardAppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = seeded_adopter_base(self.tmp.name)
        self._env = os.environ.get("COMMS_SETTINGS")
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._env is None:
            os.environ.pop("COMMS_SETTINGS", None)
        else:
            os.environ["COMMS_SETTINGS"] = self._env

    def run_app(self, settings):
        os.environ["COMMS_SETTINGS"] = str(settings)
        return AppTest.from_file(str(SCRIPT), default_timeout=60).run()

    def rendered(self, at):
        return "\n".join(str(m.value) for m in at.markdown)

    def test_the_dashboard_renders_the_adopters_own_state(self):
        """The acceptance criterion end to end: state seeded through the engine's own
        modules at the paths THIS config resolves, rendered by the app THIS repo ships."""
        from core.config import load
        cfg = load(self.base / "settings.json")
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        seed_journal(cfg.journal_path)
        seed_outbox(cfg.outbox_path_for("team"))
        at = self.run_app(self.base / "settings.json")
        self.assertFalse(at.exception, f"the app crashed: {at.exception}")
        body = self.rendered(at)
        self.assertIn(DRAFT, body, "the staged draft never reached the operator gate "
                                   "surface")
        self.assertIn(ASK, body, "the unanswered ask is not on the surface")

    def test_a_fresh_clone_renders_guidance_not_a_stacktrace(self):
        at = self.run_app(self.base / "settings.json")
        self.assertFalse(at.exception)
        warnings = "\n".join(str(w.value) for w in at.warning)
        self.assertIn("first-poll", warnings,
                      "an adopter with no state yet must be pointed at quickstart "
                      "step 5, not shown a healthy-looking empty dashboard")
        state = self.base / "state"
        self.assertEqual([p.name for p in state.iterdir() if p.suffix == ".db"] if
                         state.is_dir() else [], [],
                         "the viewer minted databases on a fresh clone")

    def test_a_missing_config_is_an_error_panel_naming_the_fix(self):
        at = self.run_app(self.base / "no-such-settings.json")
        self.assertFalse(at.exception)
        errors = "\n".join(str(e.value) for e in at.error)
        self.assertIn("no-such-settings.json", errors)
        self.assertIn("COMMS_SETTINGS", errors,
                      "the panel must say how to point the app at a real config")


if __name__ == "__main__":
    unittest.main(verbosity=2)
