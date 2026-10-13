"""The gated write surface (ENH-28): default read-only, click-to-send, no bypass.

Three properties carry the feature, and each has a mutation in
tests/mutation_check.sh that removes it and requires a test here to go red:

* **Default read-only.** With COMMS_UI_WRITE_ENABLED unset or 'false', the dashboard
  is byte-for-byte the viewer it always was: no write widgets render, and the send
  layer is not merely unused but UNREACHABLE — scripts/dashboard_write.py (the one
  module allowed to import core.outbox) is never imported. That placement is pinned
  statically (the import must sit inside the `if WRITE_ENABLED:` branch — checkable
  with no streamlit installed) and proven at runtime via sys.modules.
* **Composing stages; only the click sends.** The compose form's submit produces a
  STAGED row and nothing else; the fake adapter's ground truth stays empty until the
  operator clicks Send on that exact draft, which must land the row COMMITTED with a
  read-back-proven receipt.
* **A junk gate value is refused by name.** 'ture' silently resolving to read-only
  is the ENH-17 inert-key defect on an env var: the operator believes writes are on.

The data-layer half of the gate (stage/release/discard semantics, crash seams,
policy re-checks) lives in tests/test_outbox_write.py and needs no streamlit; these
tests hold the UI wiring to it.
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

from core.dashboard import snapshot  # noqa: E402
from core.journal import Journal  # noqa: E402

SCRIPT = ROOT / "scripts" / "dashboard.py"
WRITE_MODULE = ROOT / "scripts" / "dashboard_write.py"

try:
    from streamlit.testing.v1 import AppTest
    HAVE_STREAMLIT = True
except ImportError:                                    # pragma: no cover
    HAVE_STREAMLIT = False

DRAFT = "hello from the operator — numbers attached"


class GatePlacementTest(unittest.TestCase):
    """Static properties — checkable with no streamlit installed, so the mutation
    check keeps its teeth on a stdlib-only runner too."""

    def test_the_write_import_sits_inside_the_gate_branch(self):
        """`import dashboard_write` must appear, and ONLY inside an `if` whose test
        is exactly the WRITE_ENABLED gate. Hoisted to module level (or re-guarded by
        `if True:`), the send layer would load for every read-only operator."""
        tree = ast.parse(SCRIPT.read_text())
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                child._parent = node
        imports = [n for n in ast.walk(tree)
                   if (isinstance(n, ast.Import)
                       and any(a.name == "dashboard_write" for a in n.names))
                   or (isinstance(n, ast.ImportFrom)
                       and n.module == "dashboard_write")]
        self.assertTrue(imports, "the write surface is no longer wired in at all")
        for imp in imports:
            guard, cursor = None, imp
            while cursor is not None:
                cursor = getattr(cursor, "_parent", None)
                if isinstance(cursor, ast.If):
                    guard = cursor
                    break
            self.assertIsNotNone(
                guard, "import dashboard_write is not inside any `if` — the send "
                       "layer loads unconditionally")
            self.assertTrue(
                isinstance(guard.test, ast.Name)
                and guard.test.id == "WRITE_ENABLED",
                "the import's guard is not the WRITE_ENABLED gate — whatever this "
                f"condition is ({ast.dump(guard.test)}), it is not the operator's "
                "explicit opt-in")

    def test_the_write_module_is_the_only_script_importing_the_send_layer(self):
        """The sanctioned exception must stay the only one: every OTHER script keeps
        the scheduler's discipline. (dashboard.py's own check lives in
        tests/test_dashboard.py; this sweeps the rest, including new arrivals.)"""
        for script in sorted((ROOT / "scripts").glob("*.py")):
            if script.name == "dashboard_write.py":
                continue
            imported = []
            for node in ast.walk(ast.parse(script.read_text())):
                if isinstance(node, ast.Import):
                    imported += [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    imported.append(node.module or "")
            self.assertFalse(
                [m for m in imported if "outbox" in m],
                f"scripts/{script.name} imports the send layer — dashboard_write.py "
                "is the one sanctioned write surface")


def write_base(tmp, cust_policy='"reply_policy": "staged"'):
    """An adopter tree with one composable channel and one read-only one."""
    base = Path(tmp)
    shutil.copytree(ROOT / "channels" / "fake", base / "channels" / "fake",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (base / "settings.json").write_text(
        '{"engine": {"state_dir": "state"},\n'
        ' "instances": [{"name": "team", "adapter": "fake",\n'
        '   "channels": [{"id": "C_CUST", ' + cust_policy + '},\n'
        '                {"id": "C_RO"}]}]}\n')
    return base


class _WriteSurfaceBase(unittest.TestCase):
    """setUp + widget helpers shared by the write-surface AppTests below."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = write_base(self.tmp.name)
        self._saved = {k: os.environ.get(k)
                       for k in ("COMMS_SETTINGS", "COMMS_UI_WRITE_ENABLED")}
        self.addCleanup(self._restore_env)
        os.environ["COMMS_SETTINGS"] = str(self.base / "settings.json")

    def _restore_env(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def run_app(self):
        return AppTest.from_file(str(SCRIPT), default_timeout=60).run()

    def outbox_rows(self):
        db = self.base / "state" / "outbox-team.db"
        if not db.is_file():
            return None
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return conn.execute(
                "SELECT key, state, receipt, text FROM outbox "
                "ORDER BY created_at").fetchall()
        finally:
            conn.close()

    def compose(self, at, text):
        at.selectbox[0].select("team · C_CUST (policy: staged)")
        at.text_area[0].input(text)
        submit = [b for b in at.button
                  if b.key and b.key.startswith("FormSubmitter:compose")]
        self.assertEqual(len(submit), 1, "the compose form lost its submit")
        return submit[0].click().run()

    def buttons(self, at, prefix):
        return [b for b in at.button if b.key and b.key.startswith(prefix)]


@unittest.skipUnless(HAVE_STREAMLIT, "streamlit not installed — the static checks "
                     "above and tests/test_outbox_write.py still guard the gate")
class WriteSurfaceAppTest(_WriteSurfaceBase):
    # ---- default read-only ---------------------------------------------------
    def test_gate_off_renders_no_write_surface_and_never_loads_the_send_layer(self):
        for gate in (None, "false"):
            with self.subTest(gate=gate):
                os.environ.pop("COMMS_UI_WRITE_ENABLED", None)
                if gate is not None:
                    os.environ["COMMS_UI_WRITE_ENABLED"] = gate
                sys.modules.pop("dashboard_write", None)
                at = self.run_app()
                self.assertFalse(at.exception)
                self.assertEqual([str(h.value) for h in at.header], [],
                                 "a write-surface header rendered with the gate off")
                self.assertEqual(self.buttons(at, "FormSubmitter:compose"), [],
                                 "the compose form rendered with the gate off")
                self.assertNotIn(
                    "dashboard_write", sys.modules,
                    "the send-layer module was IMPORTED with the gate off — "
                    "read-only must mean unreachable, not unused")
                captions = "\n".join(str(c.value) for c in at.caption)
                self.assertIn("cannot send or edit", captions,
                              "the read-only promise left the sidebar")

    def test_a_junk_gate_value_is_refused_by_name(self):
        os.environ["COMMS_UI_WRITE_ENABLED"] = "ture"
        at = self.run_app()
        self.assertFalse(at.exception)
        errors = "\n".join(str(e.value) for e in at.error)
        self.assertIn("COMMS_UI_WRITE_ENABLED", errors,
                      "a misspelled gate must be refused by name, not silently "
                      "resolved to one of the two states")
        self.assertEqual(self.buttons(at, "FormSubmitter:compose"), [],
                         "the write surface rendered despite the refusal")

    # ---- the write cycle, through the real widgets ----------------------------
    def test_compose_stages_and_nothing_sends_without_the_click(self):
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        at = self.compose(self.run_app(), DRAFT)
        self.assertFalse(at.exception)
        rows = self.outbox_rows()
        self.assertEqual([(r[1], r[3]) for r in rows], [("STAGED", DRAFT)])
        self.assertIsNone(rows[0][2], "a receipt exists for a draft nobody sent")
        self.assertEqual(len(self.buttons(at, "send-")), 1,
                         "the staged draft is not at the gate with a Send button")

    def test_the_click_sends_and_the_sent_half_shows_the_receipt(self):
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        at = self.compose(self.run_app(), DRAFT)
        at = self.buttons(at, "send-")[0].click().run()
        self.assertFalse(at.exception)
        rows = self.outbox_rows()
        self.assertEqual(rows[0][1], "COMMITTED")
        self.assertIsNotNone(rows[0][2], "COMMITTED without a receipt")
        body = "\n".join(str(m.value) for m in at.markdown)
        self.assertIn("COMMITTED", body,
                      "the sent half of the staged-vs-sent distinction is not "
                      "on the surface")
        self.assertEqual(self.buttons(at, "send-"), [],
                         "a delivered draft still offers a Send button")

    def test_discard_is_terminal_and_visible(self):
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        at = self.compose(self.run_app(), DRAFT)
        at = self.buttons(at, "discard-")[0].click().run()
        self.assertFalse(at.exception)
        self.assertEqual(self.outbox_rows()[0][1], "DISCARDED",
                         "discard did not record the refusal")
        self.assertEqual(self.buttons(at, "send-"), [],
                         "a discarded draft still offers a Send button")

    def test_gate_on_with_every_channel_never_says_so_instead_of_composing(self):
        base2 = write_base(tempfile.mkdtemp(), cust_policy='"label": "ro"')
        os.environ["COMMS_SETTINGS"] = str(base2 / "settings.json")
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        at = self.run_app()
        self.assertFalse(at.exception)
        self.assertEqual(self.buttons(at, "FormSubmitter:compose"), [])
        info = "\n".join(str(i.value) for i in at.info)
        self.assertIn("never", info,
                      "with nothing composable the surface must explain the "
                      "deny-by-default, not render a dead form")

    def test_a_half_configured_connection_is_named_not_a_crashed_page(self):
        """A connection whose adapter __init__ refuses a half-configured instance —
        the state the add-connection form leaves for a multi-auth adapter (slack has
        its token but not yet its `channels` auth entry) — must NOT take the whole
        write surface down. The page renders, the incomplete connection is NAMED so
        the operator knows to finish it, and a healthy sibling stays composable.

        Falsifiable: remove the try/except guard in composable() and the slack
        adapter's construction error escapes render() — at.exception is set and this
        assertFalse fails. (The identical construction call in core/doctor is already
        guarded; this pins the same discipline on the write surface.)"""
        base = Path(tempfile.mkdtemp())
        for a in ("fake", "slack"):
            shutil.copytree(ROOT / "channels" / a, base / "channels" / a,
                            ignore=shutil.ignore_patterns("__pycache__"))
        # A healthy fake instance (composable) AND a half-configured slack instance
        # (token only — no `channels` auth entry yet). The loader accepts the slack
        # instance (from_dict resolves auth refs but never constructs the adapter);
        # only the write surface constructs it, which is where the refusal lands.
        (base / "settings.json").write_text(
            '{"engine": {"state_dir": "state"},\n'
            ' "instances": [\n'
            '   {"name": "team", "adapter": "fake",\n'
            '    "channels": [{"id": "C_CUST", "reply_policy": "staged"}]},\n'
            '   {"name": "half-slack", "adapter": "slack",\n'
            '    "auth": {"token": "env:HALF_SLACK_TOKEN_T"}}]}\n')
        self._saved.setdefault("HALF_SLACK_TOKEN_T", os.environ.get("HALF_SLACK_TOKEN_T"))
        os.environ["HALF_SLACK_TOKEN_T"] = "placeholder-token-value-not-a-secret"
        os.environ["COMMS_SETTINGS"] = str(base / "settings.json")
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        at = self.run_app()
        self.assertFalse(at.exception,
                         "a half-configured connection crashed the whole write "
                         "surface instead of being reported")
        warns = "\n".join(str(w.value) for w in at.warning)
        self.assertIn("half-slack", warns,
                      "the incomplete connection was not NAMED on the surface")
        self.assertIn("channels", warns,
                      "the surface did not surface the adapter's own reason")
        # the healthy sibling is still composable — the guard skips only the broken one
        self.assertTrue(
            any(any("team · C_CUST" in opt for opt in s.options)
                for s in at.selectbox if not s.key),
            "the healthy connection lost its compose target")

    def test_rendering_the_write_surface_mints_no_state(self):
        """The gate ON and fresh state: LOOKING at the surface writes nothing —
        only actions do. The viewer discipline survives the write half."""
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        at = self.run_app()
        self.assertFalse(at.exception)
        state = self.base / "state"
        minted = [p.name for p in state.rglob("*")] if state.is_dir() else []
        self.assertEqual(minted, [],
                         "rendering (not acting on) the write surface created "
                         f"state: {minted}")



ASK = "Please review the rollout plan before Thursday."


@unittest.skipUnless(HAVE_STREAMLIT, "streamlit not installed — the static checks "
                     "above and tests/test_outbox_write.py still guard the gate")
class ReplyFromTheSurfaceTest(_WriteSurfaceBase):
    """An answer sent from the surface must CLOSE the ask it answers (ENH-30).

    Measured on the aws-do-agent integration (2026-10-12): the operator composed and
    sent a reply to the demo question, and the question stayed UNANSWERED forever —
    nothing tied the released draft to the journal row (`responded_at` had no caller
    outside the tests). The surface now offers Reply on each open ask of a composable
    channel; a reply stages with the ask's ts as its trigger (in-thread when the thread
    policy allows), and the Send click — the moment the answer is PROVEN on the target
    — marks the ask responded with the outbox key. Staging alone never does (a draft
    is a promise, not an answer), and a free compose ties to nothing."""

    def seed_ask(self, channel="C_CUST", ts="1.0"):
        (self.base / "state").mkdir(exist_ok=True)
        j = Journal(self.base / "state" / "journal.db")
        j.record(channel, ts, sender_id="U_ASKER", text=ASK, kind="QUESTION")
        j.close()

    def journal_row(self, channel="C_CUST", ts="1.0"):
        j = Journal(self.base / "state" / "journal.db")
        try:
            row = j.get(channel, ts)
            return (row["responded_at"], row["response_key"])
        finally:
            j.close()

    def outbox_full_rows(self):
        db = self.base / "state" / "outbox-team.db"
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return [dict(zip(("key", "state", "trigger_ts", "thread_id", "text"), r))
                    for r in conn.execute(
                        "SELECT key, state, trigger_ts, thread_id, text FROM outbox "
                        "ORDER BY created_at")]
        finally:
            conn.close()

    def reply_then_stage(self, at, text):
        at = self.buttons(at, "reply-C_CUST-1.0")[0].click().run()
        at.text_area[0].input(text)
        submit = self.buttons(at, "FormSubmitter:compose")
        self.assertEqual(len(submit), 1)
        return submit[0].click().run()

    def test_an_open_ask_offers_reply_only_where_composing_is_allowed(self):
        self.seed_ask("C_CUST", "1.0")
        self.seed_ask("C_RO", "2.0")
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        at = self.run_app()
        self.assertFalse(at.exception)
        self.assertEqual(len(self.buttons(at, "reply-C_CUST-1.0")), 1,
                         "an open ask on a composable channel has no Reply")
        self.assertEqual(self.buttons(at, "reply-C_RO-"), [],
                         "a never-policy channel offered a Reply the gate would refuse")

    def test_a_reply_sent_from_the_surface_marks_the_ask_answered(self):
        self.seed_ask()
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        at = self.reply_then_stage(self.run_app(), "reviewed — go ahead")
        self.assertFalse(at.exception)
        rows = self.outbox_full_rows()
        self.assertEqual((rows[0]["state"], rows[0]["trigger_ts"], rows[0]["thread_id"]),
                         ("STAGED", "1.0", "1.0"),
                         "a reply stages with the ask as its trigger, in its thread")
        self.assertIsNone(self.journal_row()[0],
                          "STAGING marked the ask answered — a draft is a promise")
        at = self.buttons(at, "send-")[0].click().run()
        self.assertFalse(at.exception)
        self.assertEqual(self.outbox_full_rows()[0]["state"], "COMMITTED")
        at_, key = self.journal_row()
        self.assertIsNotNone(at_, "the ask stayed UNANSWERED after its answer was "
                                  "proven on the target")
        self.assertEqual(key, rows[0]["key"],
                         "the response key must be the outbox key that answered")
        open_asks = [i for i in snapshot(self.base / "state" / "journal.db",
                                         {})["attention"]
                     if i["severity"] == "unanswered"]
        self.assertEqual(open_asks, [], "the answered ask is still in the queue")

    def test_a_free_compose_ties_to_no_ask(self):
        self.seed_ask()
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        at = self.compose(self.run_app(), DRAFT)
        at = self.buttons(at, "send-")[0].click().run()
        self.assertFalse(at.exception)
        self.assertEqual(self.outbox_full_rows()[0]["state"], "COMMITTED")
        self.assertIsNone(self.journal_row()[0],
                          "a message composed to the channel, not in reply to the "
                          "ask, was recorded as its answer")

    def test_a_reply_where_the_thread_policy_denies_stages_top_level_and_still_ties(self):
        base2 = write_base(tempfile.mkdtemp(),
                           cust_policy='"reply_policy": "staged", '
                                       '"thread_reply_policy": "never"')
        self.base = base2
        os.environ["COMMS_SETTINGS"] = str(base2 / "settings.json")
        self.seed_ask()
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        at = self.reply_then_stage(self.run_app(), "answered in the channel")
        self.assertFalse(at.exception)
        row = self.outbox_full_rows()[0]
        self.assertEqual((row["trigger_ts"], row["thread_id"]), ("1.0", None),
                         "with the thread scope at never the reply must post "
                         "top-level — not be refused, not be silently threaded")
        at = self.buttons(at, "send-")[0].click().run()
        self.assertFalse(at.exception)
        self.assertIsNotNone(self.journal_row()[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
