"""The gated CONFIG surface (ENH-29): editing stages a diff, only a click applies.

The UI wiring of core/reconfig — the data-layer properties (stage/apply/discard
semantics, stale refusal, loader validation, secret handling) live in
tests/test_reconfig.py and need no streamlit. What is held HERE, each with a mutation
in tests/mutation_check.sh:

* **Default read-only.** With the gate off, scripts/dashboard_config.py is never
  imported — pinned statically (the import must sit inside the `if WRITE_ENABLED:`
  branch) and proven at runtime via sys.modules, like the message write surface.
* **An edit stages; only the click applies.** Submitting a form leaves settings.json
  byte-for-byte unchanged; the change waits at the gate as an exact diff; the click
  on that diff writes the file, the surface reads the NEW config back on the same
  rerun, and the flash states the reload truth (restart the running loop — no
  hot-reload exists).
* **A widening is loud.** A staged change that promotes a reply policy renders a
  POLICY WIDENING banner on the exact card the human clicks.
* **The never-default reaches the form.** The add-channel form's policy control
  defaults to 'never', and applying it yields a channel the loader denies.
* **Env values never render.** The surface shows whether a named variable is set —
  the value of a set variable must not appear anywhere on the page.
"""
import ast
import json
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

try:
    from streamlit.testing.v1 import AppTest
    HAVE_STREAMLIT = True
except ImportError:                                    # pragma: no cover
    HAVE_STREAMLIT = False


class ConfigGatePlacementTest(unittest.TestCase):
    """Static — checkable with no streamlit installed, so the mutation check keeps
    its teeth on a stdlib-only runner too (the test_dashboard_write pattern)."""

    def test_the_config_import_sits_inside_the_gate_branch(self):
        tree = ast.parse(SCRIPT.read_text())
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                child._parent = node
        imports = [n for n in ast.walk(tree)
                   if (isinstance(n, ast.Import)
                       and any(a.name == "dashboard_config" for a in n.names))
                   or (isinstance(n, ast.ImportFrom)
                       and n.module == "dashboard_config")]
        self.assertTrue(imports, "the config surface is no longer wired in at all")
        for imp in imports:
            guard, cursor = None, imp
            while cursor is not None:
                cursor = getattr(cursor, "_parent", None)
                if isinstance(cursor, ast.If):
                    guard = cursor
                    break
            self.assertIsNotNone(
                guard, "import dashboard_config is not inside any `if` — the config "
                       "write surface loads unconditionally")
            self.assertTrue(
                isinstance(guard.test, ast.Name)
                and guard.test.id == "WRITE_ENABLED",
                "the import's guard is not the WRITE_ENABLED gate")

    def test_the_config_module_reads_no_resolved_credentials(self):
        """Everything displayed comes from the raw settings text (env:NAME refs).
        InstanceConfig.auth holds RESOLVED secret values; one attribute access is
        the whole distance between 'shows whether the var is set' and 'echoes the
        token to the page'."""
        src = (ROOT / "scripts" / "dashboard_config.py").read_text()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Attribute):
                self.assertNotEqual(
                    node.attr, "auth",
                    "scripts/dashboard_config.py touches .auth — the resolved "
                    "credential dict must never reach the render path")


def config_base(tmp):
    """An adopter tree: the fake adapter, one staged channel, one default-deny one."""
    base = Path(tmp)
    shutil.copytree(ROOT / "channels" / "fake", base / "channels" / "fake",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (base / "settings.json").write_text(json.dumps({
        "engine": {"state_dir": "state"},
        "instances": [{"name": "team", "adapter": "fake",
                       "channels": [{"id": "C_CUST", "reply_policy": "staged"},
                                    {"id": "C_RO"}]}]}, indent=2) + "\n")
    return base


@unittest.skipUnless(HAVE_STREAMLIT, "streamlit not installed — the static checks "
                     "above and tests/test_reconfig.py still guard the gate")
class ConfigSurfaceAppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = config_base(self.tmp.name)
        self.settings = self.base / "settings.json"
        self._saved = {k: os.environ.get(k)
                       for k in ("COMMS_SETTINGS", "COMMS_UI_WRITE_ENABLED")}
        self.addCleanup(self._restore_env)
        os.environ["COMMS_SETTINGS"] = str(self.settings)
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"

    def _restore_env(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def run_app(self):
        return AppTest.from_file(str(SCRIPT), default_timeout=60).run()

    # -- widget helpers (by key: this module keys every widget) ---------------
    def by_key(self, widgets, key):
        found = [w for w in widgets if w.key == key]
        self.assertEqual(len(found), 1, f"widget {key!r}: {len(found)} found")
        return found[0]

    def submit(self, at, form_key):
        found = [b for b in at.button
                 if b.key and b.key.startswith(f"FormSubmitter:{form_key}")]
        self.assertEqual(len(found), 1, f"form {form_key!r} lost its submit")
        return found[0].click().run()

    def gate_buttons(self, at, prefix):
        return [b for b in at.button if b.key and b.key.startswith(prefix)]

    def stage_rows(self):
        db = self.base / "state" / "confstage.db"
        if not db.is_file():
            return []
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return conn.execute("SELECT key, state FROM confstage "
                                "ORDER BY created_at").fetchall()
        finally:
            conn.close()

    def page_text(self, at):
        return "\n".join(str(getattr(el, "value", el)) for kind in
                         ("markdown", "caption", "error", "warning", "success",
                          "info", "subheader", "header", "code")
                         for el in getattr(at, kind))

    # -- default read-only -----------------------------------------------------
    def test_gate_off_renders_no_config_surface_and_never_loads_the_module(self):
        os.environ["COMMS_UI_WRITE_ENABLED"] = "false"
        sys.modules.pop("dashboard_config", None)
        at = self.run_app()
        self.assertFalse(at.exception)
        self.assertNotIn("dashboard_config", sys.modules,
                         "the config write layer was IMPORTED with the gate off — "
                         "read-only must mean unreachable, not unused")
        self.assertEqual(self.gate_buttons(at, "confapply-"), [])
        self.assertNotIn("connections & monitored channels",
                         "\n".join(str(h.value) for h in at.header),
                         "the config surface rendered with the gate off")

    # -- the staged-apply cycle, through the real widgets -----------------------
    def test_an_edit_stages_and_the_file_is_untouched_until_the_click(self):
        before = self.settings.read_text()
        at = self.run_app()
        self.assertFalse(at.exception)
        self.by_key(at.text_input, "confaddinst-name").input("alerts")
        at = self.submit(at, "conf-add-inst")
        self.assertFalse(at.exception)
        self.assertEqual(self.settings.read_text(), before,
                         "SUBMITTING A FORM WROTE settings.json — the click gate "
                         "does not exist")
        self.assertEqual([s[1] for s in self.stage_rows()], ["STAGED"])
        self.assertEqual(len(self.gate_buttons(at, "confapply-")), 1,
                         "the staged diff is not at the gate with an Apply button")
        self.assertIn("alerts", self.page_text(at))

    def test_the_click_applies_reads_back_live_and_states_the_reload_truth(self):
        at = self.run_app()
        self.by_key(at.text_input, "confaddinst-name").input("alerts")
        at = self.submit(at, "conf-add-inst")
        at = self.gate_buttons(at, "confapply-")[0].click().run()
        self.assertFalse(at.exception)
        raw = json.loads(self.settings.read_text())
        self.assertIn("alerts", [s["name"] for s in raw["instances"]],
                      "the click did not write the staged change")
        self.assertEqual([s[1] for s in self.stage_rows()], ["APPLIED"])
        flashes = "\n".join(str(s.value) for s in at.success)
        for word in ("restart", "startup"):
            self.assertIn(word, flashes,
                          "the apply flash lost the reload truth — 'applied' alone "
                          "reads as 'live everywhere', false for a running loop")
        self.assertIn("alerts", self.page_text(at),
                      "the applied connection is not on the re-read surface")

    def test_a_widening_is_flagged_in_red_on_the_card_it_belongs_to(self):
        before = self.settings.read_text()
        at = self.run_app()
        self.by_key(at.text_input, "confaddchan-id").input("C_HOT")
        self.by_key(at.selectbox, "confaddchan-policy").select("staged")
        at = self.submit(at, "conf-add-chan")
        self.assertFalse(at.exception)
        errors = "\n".join(str(e.value) for e in at.error)
        self.assertIn("POLICY WIDENING", errors,
                      "a promoted policy staged without the loud flag")
        self.assertIn("C_HOT", errors, "the flag does not name the widened channel")
        self.assertEqual(self.settings.read_text(), before,
                         "the widening applied without a click")

    def test_discard_is_terminal_and_kept_on_the_surface(self):
        at = self.run_app()
        self.by_key(at.text_input, "confaddinst-name").input("alerts")
        at = self.submit(at, "conf-add-inst")
        at = self.gate_buttons(at, "confdiscard-")[0].click().run()
        self.assertFalse(at.exception)
        self.assertEqual([s[1] for s in self.stage_rows()], ["DISCARDED"],
                         "discard did not keep the record")
        self.assertEqual(self.gate_buttons(at, "confapply-"), [],
                         "a discarded diff still offers an Apply button")
        self.assertNotIn("alerts",
                         [s["name"] for s in
                          json.loads(self.settings.read_text())["instances"]],
                         "the discarded change reached the file anyway")
        self.assertIn("1 discarded", self.page_text(at),
                      "the kept refusal is not counted on the surface")

    def test_env_var_values_never_render(self):
        os.environ["CONF_UI_PROBE_TOKEN"] = "sekrit-value-a8f3"
        self.addCleanup(os.environ.pop, "CONF_UI_PROBE_TOKEN", None)
        raw = json.loads(self.settings.read_text())
        raw["instances"][0]["auth"] = {"token": "env:CONF_UI_PROBE_TOKEN"}
        self.settings.write_text(json.dumps(raw, indent=2) + "\n")
        at = self.run_app()
        self.assertFalse(at.exception)
        text = self.page_text(at)
        self.assertIn("CONF_UI_PROBE_TOKEN", text,
                      "the env reference NAME should be shown")
        self.assertIn("set", text)
        self.assertNotIn("sekrit-value-a8f3", text,
                         "the VALUE of a set environment variable reached the page")

    def test_offline_e2e_connection_then_channel_then_composable(self):
        """The whole increment, offline, no secrets: add a connection → apply →
        add a monitored channel at 'staged' (flagged) → apply → the message write
        surface offers it for compose. Config read-back is live because the
        dashboard reloads settings.json every rerun."""
        at = self.run_app()
        self.by_key(at.text_input, "confaddinst-name").input("alerts")
        at = self.submit(at, "conf-add-inst")
        at = self.gate_buttons(at, "confapply-")[0].click().run()
        self.assertFalse(at.exception)
        # The new connection is selectable for channel work on the SAME rerun.
        self.by_key(at.selectbox, "confaddchan-inst").select("alerts")
        self.by_key(at.text_input, "confaddchan-id").input("C_HOT")
        self.by_key(at.selectbox, "confaddchan-policy").select("staged")
        at = self.submit(at, "conf-add-chan")
        self.assertIn("POLICY WIDENING",
                      "\n".join(str(e.value) for e in at.error))
        at = self.gate_buttons(at, "confapply-")[0].click().run()
        self.assertFalse(at.exception)
        raw = json.loads(self.settings.read_text())
        alerts = next(s for s in raw["instances"] if s["name"] == "alerts")
        self.assertEqual(alerts["channels"], [{"id": "C_HOT",
                                               "reply_policy": "staged"}])
        # The message write surface (ENH-28) now offers the new channel.
        compose = [s for s in at.selectbox if not s.key]  # the unkeyed compose box
        self.assertTrue(any("alerts · C_HOT (policy: staged)" in s.options
                            for s in compose),
                        "the applied channel never reached the compose surface")

    def test_add_channel_defaults_deny_by_omission_end_to_end(self):
        at = self.run_app()
        self.by_key(at.text_input, "confaddchan-id").input("C_NEW")
        at = self.submit(at, "conf-add-chan")     # policy control left at default
        self.assertFalse(at.exception)
        errors = "\n".join(str(e.value) for e in at.error)
        self.assertNotIn("POLICY WIDENING", errors,
                         "a default add-channel must not widen anything")
        at = self.gate_buttons(at, "confapply-")[0].click().run()
        team = next(s for s in json.loads(self.settings.read_text())["instances"]
                    if s["name"] == "team")
        ch = next(c for c in team["channels"] if c["id"] == "C_NEW")
        self.assertNotIn("reply_policy", ch,
                         "the form's default wrote a policy key — deny must come "
                         "from the loader's own default, by omission")


if __name__ == "__main__":
    unittest.main(verbosity=2)
