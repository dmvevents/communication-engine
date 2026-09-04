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


@unittest.skipUnless(HAVE_STREAMLIT, "streamlit not installed — the static checks "
                     "above and tests/test_outbox_write.py still guard the gate")
class WriteSurfaceAppTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
