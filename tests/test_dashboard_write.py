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

import json
import time

from core.config import load, load_adapter_class  # noqa: E402
from core.dashboard import snapshot  # noqa: E402
from core.journal import Journal  # noqa: E402
from core.outbox import Outbox  # noqa: E402
from core.reconfig import ConfigStage, remove_auth, update_channel  # noqa: E402
from core.store import Store  # noqa: E402

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


def slack_stub_base(tmp):
    """An adopter tree on the REAL Slack adapter, pointed at the loopback stub: a DM at
    `staged` (replies post top-level) and a channel answerable in-thread, with a marker."""
    base = Path(tmp)
    shutil.copytree(ROOT / "channels" / "slack", base / "channels" / "slack",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (base / "settings.json").write_text(
        '{"engine": {"state_dir": "state"},\n'
        ' "instances": [{"name": "team", "adapter": "slack", "send_marker": "[AGENT]",\n'
        '   "auth": {"token": "env:UI_TEST_SLACK_TOKEN", '
        '"channels": "env:UI_TEST_SLACK_CHANNELS"},\n'
        '   "channels": [{"id": "C_CUST", "thread_reply_policy": "staged"},\n'
        '                {"id": "D_SOMEONE", "kind": "dm", "reply_policy": "staged"}]}]}\n')
    return base


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
                for s in at.selectbox if s.key == "compose_where"),
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
        at = self.buttons(at, "reply|C_CUST|1.0")[0].click().run()
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
        self.assertEqual(len(self.buttons(at, "reply|C_CUST|1.0")), 1,
                         "an open ask on a composable channel has no Reply")
        self.assertEqual(self.buttons(at, "reply|C_RO|"), [],
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

    def test_ask_text_cannot_inject_markdown_beside_the_reply_button(self):
        """Ask text is attacker-controlled (anyone who can post in a watched channel).
        html.escape neutralises tags but not markdown: a row that STARTS with markdown
        lets `![x](https://evil/p.png)` render as an image the operator's browser fetches,
        and `[approve here](https://phish)` as a link beside the Reply button. The row
        must be a CommonMark HTML block (`<div`-led) with no blank line inside, where
        markdown is inert."""
        (self.base / "state").mkdir(exist_ok=True)
        j = Journal(self.base / "state" / "journal.db")
        j.record("C_CUST", "1.0", sender_id="U_EVIL", kind="QUESTION",
                 text="see ![x](https://evil.example/p.png)\n\n[approve here]"
                      "(https://phish.example) <script>alert(1)</script>?")
        # A bare CR pair is a blank line to CommonMark too (delta review, M-ESC-R).
        j.record("C_CUST", "2.0", sender_id="U_EVIL", kind="QUESTION",
                 text="bare\r\r![y](https://evil.example/q.png) <script>x</script>?")
        j.close()
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        at = self.run_app()
        self.assertFalse(at.exception)
        rows = [str(m.value) for m in at.markdown if "evil.example" in str(m.value)]
        self.assertTrue(any("padding:2px 0" in r for r in rows),
                        "the open-asks row did not render")
        for row in rows:   # the Reply row, the viewer card, the needs-an-answer table
            self.assertTrue(row.lstrip().startswith("<"),
                            "untrusted text must sit inside an HTML block so inline "
                            f"markdown is inert: {row[:60]!r}")
            self.assertNotIn("\n\n", row, "a blank line ends the HTML block and "
                                          "re-arms markdown for everything after it")
            self.assertNotIn("\r", row, "a carriage return survived — CR CR is a blank "
                                       "line to the renderer")
            self.assertNotIn("<script>", row, "tags must stay escaped")
            self.assertIn("&lt;script&gt;", row)

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
        successes = "\n".join(str(x.value) for x in at.success)
        self.assertIn("top-level", successes,
                      "the flash must say the reply posts top-level here")
        self.assertNotIn("in its thread", successes,
                         "the flash described a top-level reply as threaded")
        at = self.buttons(at, "send-")[0].click().run()
        self.assertFalse(at.exception)
        self.assertIsNotNone(self.journal_row()[0])



NOTHREAD_ADAPTER = '''\
"""Send-capable, thread-INCAPABLE (channels/CONTRACT.md permits a send() without a
thread_id parameter): the shipped fake with its placement argument removed."""
import importlib.util
from pathlib import Path

_entry = Path(__file__).resolve().parent.parent / "fake" / "adapter.py"
_spec = importlib.util.spec_from_file_location("nothread_fake_base", _entry)
_fake = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fake)


class Adapter(_fake.Adapter):
    def capabilities(self):
        caps = dict(super().capabilities())
        caps["threads"] = False
        return caps

    def send(self, channel_id, text, key=None):
        return super().send(channel_id, text, key=key)
'''


def nothread_base(tmp):
    """One connection whose adapter can send but cannot thread: C_T answers in-thread
    only (a reply there has nowhere legal to go), C_CUST answers top-level only, C_BOTH
    allows both placements (so a reply there goes top-level, visibly)."""
    base = Path(tmp)
    shutil.copytree(ROOT / "channels" / "fake", base / "channels" / "fake",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (base / "channels" / "nothread").mkdir()
    (base / "channels" / "nothread" / "adapter.py").write_text(NOTHREAD_ADAPTER)
    (base / "settings.json").write_text(
        '{"engine": {"state_dir": "state"},\n'
        ' "instances": [{"name": "team", "adapter": "nothread",\n'
        '   "channels": [{"id": "C_T", "thread_reply_policy": "staged"},\n'
        '                {"id": "C_CUST", "reply_policy": "staged", '
        '"thread_reply_policy": "never"},\n'
        '                {"id": "C_BOTH", "reply_policy": "staged", '
        '"thread_reply_policy": "staged"}]}]}\n')
    return base


def marked_base(tmp):
    """A connection with a send_marker and a DM channel next to a public one (ENH-31)."""
    base = Path(tmp)
    shutil.copytree(ROOT / "channels" / "fake", base / "channels" / "fake",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (base / "settings.json").write_text(
        '{"engine": {"state_dir": "state"},\n'
        ' "instances": [{"name": "team", "adapter": "fake", "send_marker": "[AGENT]",\n'
        '   "channels": [{"id": "C_CUST", "reply_policy": "staged"},\n'
        '                {"id": "D_SOMEONE", "kind": "dm", "reply_policy": "staged", '
        '"thread_reply_policy": "staged"}]}]}\n')
    return base


def two_channel_base(tmp):
    """Two composable channels, so a mis-targeted second draft is observable."""
    base = Path(tmp)
    shutil.copytree(ROOT / "channels" / "fake", base / "channels" / "fake",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (base / "settings.json").write_text(
        '{"engine": {"state_dir": "state"},\n'
        ' "instances": [{"name": "team", "adapter": "fake",\n'
        '   "channels": [{"id": "C_A", "reply_policy": "staged"},\n'
        '                {"id": "C_B", "reply_policy": "staged"},\n'
        '                {"id": "C_T", "thread_reply_policy": "staged"}]}]}\n')
    return base


@unittest.skipUnless(HAVE_STREAMLIT, "streamlit not installed — the static checks "
                     "above and tests/test_outbox_write.py still guard the gate")
@unittest.skipUnless(HAVE_STREAMLIT, "streamlit not installed")
class SlackStubWriteSurfaceTest(_WriteSurfaceBase):
    """The one production send path — a human's click on the surface — driven through the
    REAL Slack adapter against the loopback stub, so guard 2 (re-read), guard 3's third
    answer (a mangled post) and the exact Slack calls a Send makes are measured on the path
    that ships. Review F4: every UI flow ran on the fake, whose newest() has no history and
    which is rebuilt empty on every click, so no UI test could reach the "re-read first"
    flash or the re-stage that follows it. Nothing here can reach slack.com:
    COMMS_SLACK_API_BASE admits the loopback stub only."""

    DM, ASK_TS = "D_SOMEONE", "1700000002.000100"
    REPLY = "it is at http://127.0.0.1:8502"

    def setUp(self):
        sys.path.insert(0, str(ROOT / "tests"))
        from slack_stub import SlackStub
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stub = SlackStub(slack_escapes=True).start()
        self.addCleanup(self.stub.stop)
        self.base = slack_stub_base(self.tmp.name)
        keys = ("COMMS_SETTINGS", "COMMS_UI_WRITE_ENABLED", "COMMS_SLACK_API_BASE",
                "UI_TEST_SLACK_TOKEN", "UI_TEST_SLACK_CHANNELS")
        self._saved = {k: os.environ.get(k) for k in keys}
        self.addCleanup(self._restore_env)
        os.environ.update(COMMS_SETTINGS=str(self.base / "settings.json"),
                          COMMS_UI_WRITE_ENABLED="true",
                          COMMS_SLACK_API_BASE=self.stub.base,
                          UI_TEST_SLACK_TOKEN="xoxb-ui",       # bot-shaped, not a token
                          UI_TEST_SLACK_CHANNELS="C_CUST,D_SOMEONE")
        self.stub.seed(self.DM, "where is the dashboard?", user="U_ASKER", ts=self.ASK_TS)
        (self.base / "state").mkdir(exist_ok=True)
        j = Journal(self.base / "state" / "journal.db")
        j.record(self.DM, self.ASK_TS, sender_id="U_ASKER", text="where is the dashboard?",
                 kind="QUESTION")
        j.close()

    def methods(self):
        return [m for m, _, _ in self.stub.requests]

    def rows(self):
        db = self.base / "state" / "outbox-team.db"
        if not db.is_file():
            return []
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return [dict(zip(("key", "state", "text", "seen_ts"), r)) for r in conn.execute(
                "SELECT key, state, text, seen_ts FROM outbox ORDER BY created_at")]
        finally:
            conn.close()

    def responded(self):
        j = Journal(self.base / "state" / "journal.db")
        try:
            r = j.get(self.DM, self.ASK_TS)
            return (r["responded_at"], r["response_key"])
        finally:
            j.close()

    def reply_and_stage(self, at, text):
        at = self.buttons(at, "reply|")[0].click().run()
        at.text_area[0].input(text)
        return self.buttons(at, "FormSubmitter:compose")[0].click().run()

    def test_a_message_after_staging_refuses_the_click_and_re_staging_is_the_acknowledgement(self):
        """Guard 2 on the shipped path. The baseline is what the human saw on the
        PLATFORM's clock (the ask, recorded at stage); a message after it refuses the
        click, naming who and when and never the text; staging the SAME text again is the
        acknowledgement ("I re-read; keep this text"), and the next click posts exactly
        once — re-read, post, read-back, and nothing else."""
        at = self.reply_and_stage(self.run_app(), self.REPLY)
        self.assertFalse(at.exception)
        (row,) = self.rows()
        self.assertEqual((row["state"], row["text"], row["seen_ts"]),
                         ("STAGED", f"[AGENT] {self.REPLY}", self.ASK_TS))
        self.stub.seed(self.DM, "never mind, found it", user="U_ASKER")   # before the click
        n0 = len(self.stub.requests)
        at = self.buttons(at, "send-")[0].click().run()
        self.assertFalse(at.exception)
        flashes = "\n".join(str(x.value) for x in list(at.error) + list(at.warning))
        self.assertIn("re-read first", flashes)
        self.assertIn("U_ASKER", flashes)
        self.assertNotIn("never mind", flashes, "a flash renders markdown — never the text")
        self.assertEqual(self.rows()[0]["state"], "STAGED")
        self.assertEqual(self.methods()[n0:], ["conversations.history"],
                         "a refused click made a Slack call beyond the re-read")
        self.assertEqual(self.stub.posted(self.DM), [])
        at = self.reply_and_stage(at, self.REPLY)                 # the human re-read
        self.assertFalse(at.exception)
        successes = "\n".join(str(x.value) for x in at.success)
        self.assertIn("re-read acknowledged", successes)
        self.assertEqual(len(self.rows()), 1, "re-staging the same text minted a second draft")
        self.assertGreater(float(self.rows()[0]["seen_ts"]), float(self.ASK_TS))
        n1 = len(self.stub.requests)
        at = self.buttons(at, "send-")[0].click().run()
        self.assertFalse(at.exception)
        self.assertEqual(self.rows()[0]["state"], "COMMITTED")
        self.assertEqual(self.methods()[n1:],
                         ["conversations.history", "chat.postMessage", "conversations.history"],
                         "a Send is exactly: re-read, one post, read-back")
        (post,) = self.stub.posted(self.DM)
        self.assertEqual(post["text"], "[AGENT] it is at <http://127.0.0.1:8502>",
                         "the stub stores the URL the way Slack does; read-back compared canonically")
        self.assertEqual(post["metadata"]["event_payload"]["key"], self.rows()[0]["key"])
        self.assertNotIn("thread_ts", post, "a DM reply is top-level")
        self.assertIsNotNone(self.responded()[0], "the proven reply did not tie the ask")

    def test_a_refused_post_returns_the_draft_to_the_gate_and_says_why(self):
        """Review R2-8: Slack answered ok:false (the bot is not in the DM); nothing was
        posted. The draft is back at the gate with Send and Discard, and the flash says
        what the platform said — not a generic exception with 'row state is now INTENT'."""
        at = self.reply_and_stage(self.run_app(), self.REPLY)
        self.stub.fail_next["chat.postMessage"] = "not_in_channel"
        at = self.buttons(at, "send-")[0].click().run()
        self.assertFalse(at.exception)
        errors = "\n".join(str(x.value) for x in at.error)
        self.assertIn("refused the post", errors)
        self.assertIn("not_in_channel", errors)
        self.assertNotIn("row state is now", errors)
        self.assertEqual(self.rows()[0]["state"], "STAGED")
        self.assertEqual(self.stub.posted(self.DM), [])
        self.assertEqual((len(self.buttons(at, "send-")), len(self.buttons(at, "discard-"))), (1, 1))

    def test_the_baseline_is_what_the_page_showed_not_what_the_platform_holds(self):
        """Review R2-5 case A: the page lists the journal, which lags the platform by a
        poll. A message the asker posted after the last poll was never on the page, so it
        must refuse the click — the old baseline (the platform's newest at staging) would
        have recorded it as seen and posted over it."""
        self.stub.seed(self.DM, "never mind, found it", user="U_ASKER")   # unpolled, unseen
        at = self.reply_and_stage(self.run_app(), self.REPLY)
        self.assertFalse(at.exception)
        (row,) = self.rows()
        self.assertEqual(row["seen_ts"], self.ASK_TS, "the baseline is the platform's newest, "
                                                      "not the newest ts the page showed")
        at = self.buttons(at, "send-")[0].click().run()
        flashes = "\n".join(str(x.value) for x in list(at.error) + list(at.warning))
        self.assertIn("re-read first", flashes)
        self.assertIn("U_ASKER", flashes)
        self.assertEqual((self.rows()[0]["state"], self.stub.posted(self.DM)), ("STAGED", []))

    def test_settling_a_parked_reply_as_delivered_answers_the_ask(self):
        """Review round 4 (R4-5): settling the row alone left the ask unanswered, its owed
        work escalating and its Reply still offered. The human's reason acts on the ask:
        'the reply is there' ties it to this row; 'not needed' closes it as such."""
        self.stub.truncate_posts_to = 20                            # a MISMATCH after the click
        at = self.reply_and_stage(self.run_app(), self.REPLY)
        at = self.buttons(at, "send-")[0].click().run()
        (row,) = self.rows()
        self.assertEqual((row["state"], self.responded()), ("MISMATCH", (None, None)))
        self.assertEqual(len(self.buttons(at, "reply|")), 1, "the ask should still be open here")
        at = self.buttons(at, f"settle-delivered-{row['key']}")[0].click().run()
        self.assertFalse(at.exception)
        self.assertIn("recorded as answered", "\n".join(str(x.value) for x in at.success))
        self.assertEqual(self.rows()[0]["state"], "SETTLED")
        self.assertEqual(self.responded()[1], row["key"], "the ask was not tied to the settled row")
        self.assertEqual(self.buttons(at, "reply|"), [], "a settled ask still offers Reply")
        self.assertEqual(len(self.stub.posted(self.DM)), 1)

    def test_a_newer_open_ask_does_not_mask_a_statement_between_it_and_the_ask_answered(self):
        """Review round 4 (R4-3): ask 1, then "never mind the first one", then ask 2 — all
        journaled, the statement never listed. The reply to ask 1 is baselined on ask 1, so
        the statement refuses the click, whatever the page lists after it."""
        never_mind = self.stub.seed(self.DM, "never mind the first one, found it", user="U_ASKER")
        ask2 = self.stub.seed(self.DM, "and where are the logs?", user="U_ASKER")
        j = Journal(self.base / "state" / "journal.db")
        j.record(self.DM, never_mind, sender_id="U_ASKER", text="never mind the first one, found it",
                 kind="STATEMENT")
        j.record(self.DM, ask2, sender_id="U_ASKER", text="and where are the logs?", kind="QUESTION")
        j.close()
        at = self.run_app()
        reply = [b for b in self.buttons(at, "reply|") if self.ASK_TS in b.key]
        self.assertEqual(len(reply), 1)
        at = reply[0].click().run()
        at.text_area[0].input(self.REPLY)
        at = self.buttons(at, "FormSubmitter:compose")[0].click().run()
        self.assertFalse(at.exception)
        (row,) = self.rows()
        self.assertEqual(row["seen_ts"], self.ASK_TS, "a newer listed ask became the baseline, "
                                                      "masking the statement before it")
        at = self.buttons(at, "send-")[0].click().run()
        flashes = "\n".join(str(x.value) for x in list(at.error) + list(at.warning))
        self.assertIn("re-read first", flashes)
        self.assertEqual((self.rows()[0]["state"], self.stub.posted(self.DM)), ("STAGED", []))

    def test_send_again_passes_when_the_conversation_moved_on_because_the_click_is_the_re_read(self):
        """Review round 4 (R4-4): the canonical UNPROVEN row — a post the platform holds
        without its metadata — reads as a newer message from the bot, so the re-read refused
        "Send again" forever and its advice could not be followed. The click is the re-read."""
        self.stub.drop_post_metadata = True
        at = self.reply_and_stage(self.run_app(), self.REPLY)
        at = self.buttons(at, "send-")[0].click().run()             # posts; read-back cannot prove
        (row,) = self.rows()
        self.assertEqual((row["state"], len(self.stub.posted(self.DM))), ("SENT", 1))
        conn = sqlite3.connect(self.base / "state" / "outbox-team.db")
        conn.execute("UPDATE outbox SET state='UNPROVEN', resent_at=1 WHERE key=?", (row["key"],))
        conn.commit()
        conn.close()
        self.stub.drop_post_metadata = False                        # the human looked: not there
        at = self.run_app()
        at = self.buttons(at, f"reapprove-{row['key']}")[0].click().run()
        self.assertFalse(at.exception)
        flashes = "\n".join(str(x.value) for x in list(at.error) + list(at.warning))
        self.assertNotIn("re-read first", flashes, "the human's own 'send again' was refused as unread")
        self.assertIn("sent again and proven", "\n".join(str(x.value) for x in at.success))
        self.assertEqual((self.rows()[0]["state"], len(self.stub.posted(self.DM))), ("COMMITTED", 2))
        self.assertIsNotNone(self.responded()[0])

    def test_a_polled_statement_the_page_never_lists_still_refuses_the_click(self):
        """Review round 3 (R3-4): the page lists open asks, never statements. A polled
        "never mind, found it" was journaled, taken as the baseline, and the reply posted
        after it. The baseline is the newest LISTED ask — here the ask itself — so the
        statement refuses the click."""
        later = self.stub.seed(self.DM, "never mind, found it", user="U_ASKER")
        j = Journal(self.base / "state" / "journal.db")
        j.record(self.DM, later, sender_id="U_ASKER", text="never mind, found it", kind="STATEMENT")
        j.close()
        at = self.reply_and_stage(self.run_app(), self.REPLY)
        self.assertFalse(at.exception)
        self.assertEqual(self.rows()[0]["seen_ts"], self.ASK_TS,
                         "a journaled statement the page never showed became the baseline")
        at = self.buttons(at, "send-")[0].click().run()
        flashes = "\n".join(str(x.value) for x in list(at.error) + list(at.warning))
        self.assertIn("re-read first", flashes)
        self.assertEqual((self.rows()[0]["state"], self.stub.posted(self.DM)), ("STAGED", []))

    def test_a_reply_to_an_in_thread_ask_is_baselined_on_that_ask_and_posts_first_time(self):
        """Review round 3 (R3-8): baselining a thread reply on its ROOT refused the first
        click naming the very in-thread question being answered. The ask the human clicked
        Reply on is what they saw; nothing newer in the thread means the click posts."""
        root, ask = "1700000005.000100", "1700000006.000100"
        self.stub.seed("C_CUST", "deploy thread", user="U_ASKER", ts=root)
        self.stub.seed("C_CUST", "which dashboard do you mean?", user="U_ASKER", ts=ask,
                       thread_ts=root)
        j = Journal(self.base / "state" / "journal.db")
        j.record("C_CUST", ask, sender_id="U_ASKER", text="which dashboard do you mean?",
                 kind="QUESTION")
        j.close()
        store = Store(self.base / "state" / "messages.db")
        store.upsert_messages([
            {"channel_type": "slack", "channel_id": "C_CUST", "sender_id": "U_ASKER", "ts": root,
             "text": "deploy thread", "thread_id": None},
            {"channel_type": "slack", "channel_id": "C_CUST", "sender_id": "U_ASKER", "ts": ask,
             "text": "which dashboard do you mean?", "thread_id": root}])
        store.close()
        at = self.run_app()
        reply = [b for b in self.buttons(at, "reply|") if ask in b.key]
        self.assertEqual(len(reply), 1, [b.key for b in self.buttons(at, "reply|")])
        at = reply[0].click().run()
        at.text_area[0].input("the comms one")
        at = self.buttons(at, "FormSubmitter:compose")[0].click().run()
        self.assertFalse(at.exception)
        row = [r for r in self.rows() if "the comms one" in r["text"]][0]
        self.assertEqual(row["seen_ts"], ask, "the baseline was the root, not the ask answered")
        at = self.buttons(at, f"send-{row['key']}")[0].click().run()
        self.assertFalse(at.exception)
        self.assertIn("sent and proven", "\n".join(str(x.value) for x in at.success))
        (post,) = self.stub.posted("C_CUST")
        self.assertEqual(post["thread_ts"], root)

    def test_a_mangled_post_is_parked_as_mismatch_on_the_surface_and_nothing_more_is_sent(self):
        """Guard 3's third answer on the shipped path (review F3): the platform holds a
        message with this draft's key and other text. The flash says so (not "unproven,
        run recover()", which re-sent), the row is MISMATCH with no Send and no Discard,
        nothing is recorded as answered, and a later render posts nothing."""
        self.stub.truncate_posts_to = 20
        at = self.reply_and_stage(self.run_app(), self.REPLY)
        at = self.buttons(at, "send-")[0].click().run()
        self.assertFalse(at.exception)
        errors = "\n".join(str(x.value) for x in at.error)
        self.assertIn("DIFFERENT text", errors)
        self.assertIn("MISMATCH", errors)
        self.assertNotIn("recover()", errors, "a mismatch was flashed as 'unproven, run recover()'")
        # the surface's OWN words, not the outbox exception's: the message IS on the platform,
        # so this is not a refusal, and the operator is pointed at the card
        self.assertNotIn("refused", errors, "a delivered-but-mangled post was flashed as a refusal")
        self.assertIn("the card below needs a human", errors)
        self.assertEqual(self.rows()[0]["state"], "MISMATCH")
        self.assertEqual(len(self.stub.posted(self.DM)), 1)
        cards = "\n".join(str(m.value) for m in at.markdown)
        self.assertIn("MISMATCH", cards)
        self.assertIn("Needs a human", cards)
        self.assertEqual((self.buttons(at, "send-"), self.buttons(at, "discard-")), ([], []))
        self.assertEqual(self.responded(), (None, None), "a mangled post was recorded as the answer")
        at = self.run_app()
        self.assertFalse(at.exception)
        self.assertEqual((self.rows()[0]["state"], len(self.stub.posted(self.DM))), ("MISMATCH", 1))


class WriteSurfaceReviewTest(_WriteSurfaceBase):
    """Properties the independent merge review (2026-10-13) found missing or broken:
    the click must act on the CURRENT config (B2), Send must never be offered where the
    adapter cannot deliver (B3), Discard must need no adapter (S5), the screen after a
    stage must be the post-action truth (stale reply banner), a reply to an in-thread
    ask lands under the thread ROOT, a thread-only channel is answerable, and a journal
    refusal after a proven send is a warning with a real retry, never a crash."""

    def seed_ask(self, channel, ts, sender="U_ASKER", text=ASK, thread=None,
                 with_store=False):
        (self.base / "state").mkdir(exist_ok=True)
        j = Journal(self.base / "state" / "journal.db")
        j.record(channel, ts, sender_id=sender, text=text, kind="QUESTION")
        j.close()
        if with_store:
            st_ = Store(self.base / "state" / "messages.db")
            st_.upsert_messages([{"channel_type": "fake", "channel_id": channel,
                                  "sender_id": sender, "ts": ts, "text": text,
                                  "thread_id": thread}])
            st_.close()

    def rows(self, name="team"):
        db = self.base / "state" / f"outbox-{name}.db"
        if not db.is_file():
            return []
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return [dict(zip(("key", "state", "target", "trigger_ts", "thread_id", "text"), r))
                    for r in conn.execute(
                        "SELECT key, state, target, trigger_ts, thread_id, text FROM outbox "
                        "ORDER BY created_at")]
        finally:
            conn.close()

    def journal_row(self, channel, ts):
        j = Journal(self.base / "state" / "journal.db")
        try:
            r = j.get(channel, ts)
            return (r["responded_at"], r["response_key"])
        finally:
            j.close()

    def stage_text(self, at, text):
        at.text_area[0].input(text)
        return self.buttons(at, "FormSubmitter:compose")[0].click().run()

    # ---- B2: the click acts on the config as it is NOW ----------------------------
    def test_a_click_after_a_demotion_to_never_refuses(self):
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        at = self.compose(self.run_app(), "draft for a channel about to be demoted")
        settings = self.base / "settings.json"
        cs = ConfigStage(self.base / "state" / "confstage.db", settings)
        k = cs.stage(update_channel(json.loads(settings.read_text()), "team", "C_CUST",
                                    reply_policy=None))["key"]
        cs.apply(k)
        cs.close()
        self.assertEqual(load(settings).instance("team").policies()["C_CUST"], "never")
        at = self.buttons(at, "send-")[0].click().run()
        self.assertFalse(at.exception)
        self.assertEqual(self.rows()[0]["state"], "STAGED",
                         "a draft was released to a channel whose policy is now never — "
                         "the click used the config captured at render time")
        flashes = "\n".join(str(e.value) for e in at.error)
        self.assertIn("never", flashes)

    # ---- B3: no Send where the adapter cannot deliver; S5: Discard needs no adapter ----
    SLACK_SETTINGS = ('{"engine": {"state_dir": "state"},\n'
                      ' "instances": [{"name": "sl", "adapter": "slack",\n'
                      '   "auth": {"token": "env:WS_TEST_TOK", "channels": "env:WS_TEST_CH"},\n'
                      '   "channels": [{"id": "C_X", "reply_policy": "staged"}]}]}\n')
    # The socket adapter has no send() at all — since ENH-31 the polling adapter has one
    # (behind the outbox), so "an adapter that cannot send" is slack_socket here.
    SOCKET_SETTINGS = ('{"engine": {"state_dir": "state"},\n'
                       ' "instances": [{"name": "sl", "adapter": "slack_socket",\n'
                       '   "auth": {"app_token": "env:WS_TEST_APP", "channels": "env:WS_TEST_CH"},\n'
                       '   "channels": [{"id": "C_X", "reply_policy": "staged"}]}]}\n')

    def engine_staged_slack_draft(self, adapter="slack"):
        base = Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "channels" / adapter, base / "channels" / adapter,
                        ignore=shutil.ignore_patterns("__pycache__"))
        (base / "settings.json").write_text(
            self.SLACK_SETTINGS if adapter == "slack" else self.SOCKET_SETTINGS)
        # The REAL adapter is pointed at a loopback stub: since the staged send() records a
        # baseline (newest()), an adapter left at its default base would reach slack.com
        # from the suite (review round 3, R3-1). The guard in tests/netguard makes that a
        # red test; this keeps the helper honest even without the guard.
        sys.path.insert(0, str(ROOT / "tests"))
        from slack_stub import SlackStub
        stub = SlackStub().start()
        self.addCleanup(stub.stop)
        self._helper_stub = stub
        os.environ.update(WS_TEST_TOK="placeholder-not-a-token", WS_TEST_CH="C_X",
                          WS_TEST_APP="placeholder-not-a-token",
                          COMMS_SETTINGS=str(base / "settings.json"),
                          COMMS_UI_WRITE_ENABLED="true",
                          COMMS_SLACK_API_BASE=stub.base)
        self.addCleanup(lambda: [os.environ.pop(k, None)
                                 for k in ("WS_TEST_TOK", "WS_TEST_CH", "COMMS_SLACK_API_BASE")])
        cfg = load(base / "settings.json")
        inst = cfg.instance("sl")
        (base / "state").mkdir(exist_ok=True)
        ob = Outbox(cfg.outbox_path_for("sl"),
                    load_adapter_class(cfg.channels_dir, inst.adapter)(auth=inst.auth),
                    inst.policies())
        res = ob.send("C_X", "1700000000.000100", "engine-staged draft")
        ob.close()
        self.assertEqual(res["state"], "STAGED")
        self.base = base
        return base

    def test_an_engine_staged_draft_on_a_read_only_adapter_is_discard_only(self):
        self.engine_staged_slack_draft(adapter="slack_socket")
        at = self.run_app()
        self.assertFalse(at.exception)
        self.assertEqual(self.buttons(at, "send-"), [],
                         "Send offered on a draft whose adapter has no send() — one click "
                         "wedges the row in INTENT")
        self.assertEqual(len(self.buttons(at, "discard-")), 1)
        body = "\n".join(str(m.value) for m in at.markdown)
        self.assertIn("cannot send", body, "the card must say WHY there is no Send")
        at = self.buttons(at, "discard-")[0].click().run()
        self.assertFalse(at.exception)
        self.assertEqual(self.rows("sl")[0]["state"], "DISCARDED")

    def test_discard_on_a_half_configured_connection_does_not_crash(self):
        base = self.engine_staged_slack_draft()
        settings = base / "settings.json"
        cs = ConfigStage(base / "state" / "confstage.db", settings)
        k = cs.stage(remove_auth(json.loads(settings.read_text()), "sl", "channels"))["key"]
        cs.apply(k)
        cs.close()
        at = self.run_app()
        self.assertFalse(at.exception)
        at = self.buttons(at, "discard-")[0].click().run()
        self.assertFalse(at.exception, "Discard built an adapter it does not need and "
                                       "crashed on the half-configured connection")
        self.assertEqual(self.rows("sl")[0]["state"], "DISCARDED")

    # ---- R5: each Send button releases its own draft only ------------------------
    def test_a_send_releases_the_draft_whose_button_was_clicked_not_the_newest(self):
        """Two drafts at the gate; the click lands on the OLDER one. Clicking the newest
        cannot tell a correct binding from "every Send releases the newest draft" (the
        delta review's surviving G1 mutant): the newest is what both would release."""
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        at = self.compose(self.run_app(), "first draft")
        at = self.compose(at, "second draft")
        key1 = [r["key"] for r in self.rows() if r["text"] == "first draft"][0]
        at = [b for b in self.buttons(at, "send-") if b.key == f"send-{key1}"][0].click().run()
        self.assertFalse(at.exception)
        self.assertEqual({r["text"]: r["state"] for r in self.rows()},
                         {"first draft": "COMMITTED", "second draft": "STAGED"})

    # ---- D1/F6: placement is decided with the ADAPTER in hand ------------------------
    def use_nothread_base(self):
        self.tmp2 = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp2.cleanup)
        self.base = nothread_base(self.tmp2.name)
        os.environ["COMMS_SETTINGS"] = str(self.base / "settings.json")
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"

    def test_a_thread_only_channel_on_a_thread_incapable_adapter_offers_no_reply_and_says_why(self):
        """Review D1: the Reply path staged in-thread whenever the thread policy allowed,
        never asking whether the adapter could place it — the draft then wedged at Send
        (INTENT, undiscardable). Where the thread scope is the ONLY open placement and
        the adapter cannot thread, there is nowhere legal for a reply to go: no Reply is
        offered, and the reason is on the page (review F6)."""
        self.use_nothread_base()
        self.seed_ask("C_T", "1.0")
        at = self.run_app()
        self.assertFalse(at.exception)
        self.assertEqual(self.buttons(at, "reply|"), [],
                         "Reply offered where no placement is open")
        warnings = "\n".join(str(w.value) for w in at.warning)
        self.assertIn("C_T", warnings)
        self.assertIn("cannot place a reply in a thread", warnings)
        self.assertEqual(self.rows(), [])

    def test_a_reply_on_a_thread_incapable_adapter_posts_top_level_where_the_channel_allows_and_ties(self):
        """Where the channel scope allows top-level, a reply through an adapter that
        cannot thread goes top-level — VISIBLY (banner and flash say why), with the ask
        as its trigger so the Send still ties it — exactly as a thread_reply_policy of
        'never' already behaves. Refusing here left the ask unanswerable from the
        surface: a free compose never ties (review F6)."""
        self.use_nothread_base()
        self.seed_ask("C_BOTH", "3.0")
        at = self.run_app()
        at = self.buttons(at, "reply|")[0].click().run()
        banner = "\n".join(str(i.value) for i in at.info)
        self.assertIn("top-level", banner)
        self.assertIn("cannot place a reply in a thread", banner)
        at = self.stage_text(at, "answered top-level, visibly")
        self.assertFalse(at.exception)
        self.assertEqual([(r["trigger_ts"], r["thread_id"], r["state"]) for r in self.rows()],
                         [("3.0", None, "STAGED")])
        successes = "\n".join(str(x.value) for x in at.success)
        self.assertIn("top-level", successes)
        self.assertIn("cannot place a reply in a thread", successes)
        at = self.buttons(at, "send-")[0].click().run()
        self.assertFalse(at.exception)
        self.assertIsNotNone(self.journal_row("C_BOTH", "3.0")[0],
                             "the top-level reply did not tie the ask")

    def test_a_thread_draft_whose_adapter_was_swapped_is_refused_at_the_click_by_name(self):
        """Send was rendered while the connection's adapter could thread; the adapter was
        edited to one that cannot before the click. The click re-decides on fresh config
        (B2) and refuses BEFORE the flip, naming the adapter by its channels/ directory —
        "adapter nothread", not "adapter Adapter" (review F10/R2-9)."""
        self.use_nothread_base()
        settings = self.base / "settings.json"
        nothread = settings.read_text()
        settings.write_text(nothread.replace('"adapter": "nothread"', '"adapter": "fake"'))
        (self.base / "state").mkdir(exist_ok=True)
        capable = load_adapter_class(self.base / "channels", "fake")()
        ob = Outbox(self.base / "state" / "outbox-team.db", capable, {"C_BOTH": "staged"})
        ob.stage("C_BOTH", "1.0", "staged while the connection could thread", thread_id="1.0")
        ob.close()
        at = self.run_app()
        send = self.buttons(at, "send-")
        self.assertEqual(len(send), 1, "Send must be offered while the adapter can thread")
        settings.write_text(nothread)                   # the connection cannot thread now
        at = send[0].click().run()
        self.assertFalse(at.exception)
        errors = "\n".join(str(e.value) for e in at.error)
        self.assertIn("adapter nothread cannot place a reply in thread 1.0", errors)
        self.assertIn("the draft stays STAGED", errors)
        self.assertEqual(self.rows()[0]["state"], "STAGED")

    # ---- ENH-31: the marker is part of the approved text; a DM reply posts top-level ----
    def use_marked_base(self):
        self.tmp2 = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp2.cleanup)
        self.base = marked_base(self.tmp2.name)
        os.environ["COMMS_SETTINGS"] = str(self.base / "settings.json")
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"

    def test_the_connections_marker_is_prepended_at_stage_shown_on_the_card_and_sent(self):
        """Guard 1 on the surface: the human types the answer, the staged card shows the
        exact text a Send would post — marker included — and that is what the adapter
        receives. A reader, and the other senders' dedupe, can tell the engine's
        messages apart by it."""
        self.use_marked_base()
        at = self.run_app()
        at.selectbox[0].select("team · C_CUST (policy: staged)")
        at = self.stage_text(at, "the numbers are attached")
        self.assertFalse(at.exception)
        rows = self.rows()
        self.assertEqual([r["text"] for r in rows], ["[AGENT] the numbers are attached"])
        cards = "\n".join(str(m.value) for m in at.markdown)
        self.assertIn("[AGENT] the numbers are attached", cards)
        at = self.buttons(at, "send-")[0].click().run()
        self.assertFalse(at.exception)
        self.assertEqual(self.rows()[0]["state"], "COMMITTED")
        caps = "\n".join(str(c.value) for c in at.caption)
        self.assertIn("[AGENT]", caps, "the compose form must say the marker is added")

    def test_a_reply_in_a_direct_message_posts_top_level_and_says_so(self):
        """Guard 6: placement follows the human. A 1:1 DM is answered in the conversation,
        top-level, even where a thread policy would allow a thread — nobody reads a
        thread inside a DM (measured: 535 DM messages, 0 in-thread)."""
        self.use_marked_base()
        self.seed_ask("D_SOMEONE", "7.0")
        at = self.run_app()
        at = self.buttons(at, "reply|")[0].click().run()
        banner = "\n".join(str(i.value) for i in at.info)
        self.assertIn("top-level", banner)
        self.assertIn("direct message", banner)
        at = self.stage_text(at, "yes — it is at http://127.0.0.1:8502")
        self.assertFalse(at.exception)
        self.assertEqual([(r["trigger_ts"], r["thread_id"]) for r in self.rows()],
                         [("7.0", None)])
        at = self.buttons(at, "send-")[0].click().run()
        self.assertFalse(at.exception)
        self.assertEqual(self.rows()[0]["state"], "COMMITTED")
        self.assertIsNotNone(self.journal_row("D_SOMEONE", "7.0")[0])

    def test_an_unproven_row_is_shown_as_needing_a_human_with_no_buttons(self):
        """Review R2-6: recovery re-sent once and still could not prove the post; the row is
        UNPROVEN, terminal for the engine. The card says what to do, including how to send
        it after all (changed text — the same text IS this row), and offers no button."""
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        at = self.compose(self.run_app(), "approved text, never provably posted")
        key = self.rows()[0]["key"]
        conn = sqlite3.connect(self.base / "state" / "outbox-team.db")
        conn.execute("UPDATE outbox SET state='UNPROVEN' WHERE key=?", (key,))
        conn.commit()
        conn.close()
        at = self.run_app()
        self.assertFalse(at.exception)
        self.assertEqual((self.buttons(at, "send-"), self.buttons(at, "discard-")), ([], []))
        cards = "\n".join(str(m.value) for m in at.markdown)
        self.assertIn("UNPROVEN", cards)
        self.assertIn("send it again", cards)
        self.assertIn("settle it as delivered", cards)
        self.assertIn("settle it as not needed", cards)
        self.assertIn("Needs a human", cards)
        self.assertEqual((len(self.buttons(at, "settle-")), len(self.buttons(at, "reapprove-"))), (2, 1),
                         "the parked row's own actions are missing")

    def test_a_stale_send_on_a_parked_row_says_nothing_was_sent(self):
        """Review round 3 (R3-6): a Send drawn while the row was STAGED, clicked after a
        recovery run parked it, flashed a green 'sent and proven'. Nothing is sent, and the
        flash must say so."""
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        at = self.compose(self.run_app(), "approved, then parked under the operator's feet")
        key = self.rows()[0]["key"]
        send = self.buttons(at, "send-")[0]                      # drawn while STAGED
        conn = sqlite3.connect(self.base / "state" / "outbox-team.db")
        conn.execute("UPDATE outbox SET state='UNPROVEN' WHERE key=?", (key,))
        conn.commit()
        conn.close()
        at = send.click().run()
        self.assertFalse(at.exception)
        self.assertEqual("\n".join(str(x.value) for x in at.success), "",
                         "a stale click on a parked row flashed success")
        warnings = "\n".join(str(x.value) for x in at.warning)
        self.assertIn("nothing sent", warnings)
        self.assertIn("UNPROVEN", warnings)
        self.assertEqual(self.rows()[0]["state"], "UNPROVEN")

    def test_a_parked_row_can_be_settled_or_sent_again_by_a_human(self):
        """Review round 3 (R3-7): an unresolved send could never leave triage. The card's
        'Settled' keeps the row as SETTLED and removes it; its 'Send again' (UNPROVEN only)
        is one more post through the same guards, an explicit second approval."""
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        at = self.compose(self.run_app(), "first parked reply")
        at = self.compose(at, "second parked reply")
        first, second = [r["key"] for r in self.rows()]
        conn = sqlite3.connect(self.base / "state" / "outbox-team.db")
        conn.execute("UPDATE outbox SET state='UNPROVEN' WHERE key=?", (first,))
        conn.execute("UPDATE outbox SET state='MISMATCH' WHERE key=?", (second,))
        conn.commit()
        conn.close()
        at = self.run_app()
        self.assertEqual(len(self.buttons(at, "settle-")), 4, "two settle reasons per parked row")
        self.assertEqual([b.key for b in self.buttons(at, "reapprove-")], [f"reapprove-{first}"],
                         "Send again must be offered for UNPROVEN only — a MISMATCH is on the platform")
        at = self.buttons(at, f"settle-notneeded-{second}")[0].click().run()
        self.assertFalse(at.exception)
        self.assertIn("not needed", "\n".join(str(x.value) for x in at.success))
        states = {r["key"]: r["state"] for r in self.rows()}
        self.assertEqual(states[second], "SETTLED")
        self.assertEqual(len(self.buttons(at, "settle-")), 2, "a settled row still has a card")
        at = self.buttons(at, f"reapprove-{first}")[0].click().run()
        self.assertFalse(at.exception)
        self.assertIn("sent again and proven", "\n".join(str(x.value) for x in at.success))
        self.assertEqual({r["key"]: r["state"] for r in self.rows()}[first], "COMMITTED")
        self.assertEqual((self.buttons(at, "settle-"), self.buttons(at, "reapprove-")), ([], []))

    def test_a_draft_staged_before_the_marker_was_configured_is_refused_at_the_click(self):
        """Review X7: the marker is verified again at the click BY THE OUTBOX THE SURFACE
        BUILDS — the one production send path has to carry the guard, not only the compose
        form. A draft from before the connection had a marker must not go out bare."""
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        at = self.compose(self.run_app(), "a bare draft, staged before the marker existed")
        self.assertEqual([r["state"] for r in self.rows()], ["STAGED"])
        settings = self.base / "settings.json"
        settings.write_text(settings.read_text().replace(
            '"adapter": "fake",', '"adapter": "fake", "send_marker": "[AGENT]",'))
        self.assertEqual(load(settings).instance("team").send_marker, "[AGENT]")
        at = self.buttons(at, "send-")[0].click().run()
        self.assertFalse(at.exception)
        flashes = "\n".join(str(x.value) for x in list(at.error) + list(at.warning))
        self.assertIn("marker", flashes)
        self.assertNotIn("a bare draft", flashes, "a flash renders markdown — never the text")
        self.assertEqual(self.rows()[0]["state"], "STAGED",
                         "a bare draft went out — the surface's outbox ran without the marker")

    def test_a_mismatched_row_is_shown_as_needing_a_human_with_no_buttons(self):
        """ENH-31 guard 3 (review F3): the platform holds a message with this draft's key
        and other text. The card says so and offers neither Send (it is there) nor
        Discard (it was delivered); nothing else on the page changes."""
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        at = self.compose(self.run_app(), "approved text, as staged")
        key = self.rows()[0]["key"]
        conn = sqlite3.connect(self.base / "state" / "outbox-team.db")
        conn.execute("UPDATE outbox SET state='MISMATCH' WHERE key=?", (key,))
        conn.commit()
        conn.close()
        at = self.run_app()
        self.assertFalse(at.exception)
        self.assertEqual((self.buttons(at, "send-"), self.buttons(at, "discard-")), ([], []))
        cards = "\n".join(str(m.value) for m in at.markdown)
        self.assertIn("MISMATCH", cards)
        self.assertIn("DIFFERENT text", cards)
        self.assertIn("Needs a human", cards)
        self.assertIn("approved text, as staged", cards)

    def test_send_is_not_offered_on_a_thread_draft_the_adapter_cannot_place(self):
        """A thread draft staged while the connection could thread, before its adapter
        was edited (ENH-29 allows that): B3's rule on the thread dimension — Send is
        offered only where the adapter can deliver, the card says why, Discard works
        (review F7)."""
        self.use_nothread_base()
        (self.base / "state").mkdir(exist_ok=True)
        capable = load_adapter_class(self.base / "channels", "fake")()
        ob = Outbox(self.base / "state" / "outbox-team.db", capable, {"C_BOTH": "staged"})
        ob.stage("C_BOTH", "1.0", "staged while the connection could thread", thread_id="1.0")
        ob.close()
        at = self.run_app()
        self.assertFalse(at.exception)
        self.assertEqual(self.buttons(at, "send-"), [],
                         "Send offered on a thread draft this adapter cannot place")
        self.assertEqual(len(self.buttons(at, "discard-")), 1)
        cards = "\n".join(str(m.value) for m in at.markdown)
        self.assertIn("cannot place a reply in thread 1.0", cards)
        at = self.buttons(at, "discard-")[0].click().run()
        self.assertFalse(at.exception)
        self.assertEqual(self.rows()[0]["state"], "DISCARDED")

    def test_a_top_level_reply_through_the_same_adapter_still_stages_and_sends(self):
        """The refusal is about PLACEMENT, not the adapter: where the thread scope is
        'never' the reply posts top-level, which this adapter can do."""
        self.use_nothread_base()
        self.seed_ask("C_CUST", "2.0")
        at = self.run_app()
        at = self.buttons(at, "reply|")[0].click().run()
        at = self.stage_text(at, "answered top-level")
        self.assertFalse(at.exception)
        self.assertEqual([(r["trigger_ts"], r["thread_id"], r["state"]) for r in self.rows()],
                         [("2.0", None, "STAGED")])
        at = self.buttons(at, "send-")[0].click().run()
        self.assertFalse(at.exception)
        self.assertEqual(self.rows()[0]["state"], "COMMITTED")
        self.assertIsNotNone(self.journal_row("C_CUST", "2.0")[0])

    # ---- the screen after a stage is the post-action truth ------------------------
    def test_after_a_reply_is_staged_the_banner_is_gone_and_the_next_draft_is_not_retargeted(self):
        self.base = two_channel_base(tempfile.mkdtemp())
        os.environ["COMMS_SETTINGS"] = str(self.base / "settings.json")
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        self.seed_ask("C_B", "7.0")
        at = self.run_app()
        at = self.buttons(at, "reply|C_B|7.0")[0].click().run()
        self.assertTrue(any("replying to" in str(i.value) for i in at.info))
        at = self.stage_text(at, "confirmed, part 1")
        self.assertFalse(at.exception)
        self.assertFalse(any("replying to" in str(i.value) for i in at.info),
                         "the reply banner survived the stage — the screen shows "
                         "pre-action state")
        self.assertEqual(self.buttons(at, "reply_cancel"), [])
        self.assertEqual(at.selectbox[0].value, "team · C_B (policy: staged)",
                         "after replying to C_B the selector must show where the NEXT "
                         "draft goes, and it must be C_B")
        at = self.stage_text(at, "part 2: and the numbers")
        self.assertFalse(at.exception)
        second = [r for r in self.rows() if r["text"].startswith("part 2")][0]
        self.assertEqual(second["target"], "C_B",
                         "the second draft was silently re-targeted to another channel")
        self.assertTrue(second["trigger_ts"].startswith("ui-compose-"))
        self.assertIsNone(second["thread_id"])

    # ---- a reply to an in-thread ask lands under the thread ROOT ------------------
    def test_a_reply_to_an_in_thread_ask_is_placed_under_the_thread_root(self):
        self.base = two_channel_base(tempfile.mkdtemp())
        os.environ["COMMS_SETTINGS"] = str(self.base / "settings.json")
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        self.seed_ask("C_A", "6.0", text="is the dashboard up?", thread="5.0",
                      with_store=True)
        at = self.run_app()
        at = self.buttons(at, "reply|C_A|6.0")[0].click().run()
        at = self.stage_text(at, "yes, on 8502")
        row = self.rows()[0]
        self.assertEqual((row["trigger_ts"], row["thread_id"]), ("6.0", "5.0"),
                         "the reply must thread under the ROOT the sweep reads, and the "
                         "tie must stay keyed by the ask itself")

    # ---- a thread-only channel is answerable, always in its thread ------------------
    def test_a_thread_only_channel_offers_reply_and_never_a_free_compose(self):
        self.base = two_channel_base(tempfile.mkdtemp())
        os.environ["COMMS_SETTINGS"] = str(self.base / "settings.json")
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        self.seed_ask("C_T", "9.0", text="any update on the rollout?")
        at = self.run_app()
        self.assertFalse(at.exception)
        self.assertEqual(len(self.buttons(at, "reply|C_T|9.0")), 1,
                         "a channel whose THREAD policy allows a reply offered no Reply")
        self.assertNotIn("C_T", "\n".join(at.selectbox[0].options),
                         "a thread-only channel must not accept a top-level compose")
        at = self.buttons(at, "reply|C_T|9.0")[0].click().run()
        at = self.stage_text(at, "rollout done at 09:00")
        row = self.rows()[0]
        self.assertEqual((row["target"], row["trigger_ts"], row["thread_id"]),
                         ("C_T", "9.0", "9.0"))

    # ---- F9: an edit while the draft waited is never laundered by the Send ----------
    def test_an_edit_while_the_draft_waited_is_flagged_not_laundered(self):
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        self.seed_ask("C_CUST", "1.0", text="Can you deploy to staging?")
        at = self.run_app()
        at = self.buttons(at, "reply|C_CUST|1.0")[0].click().run()
        at = self.stage_text(at, "yes - deploying to staging now")
        time.sleep(0.05)
        j = Journal(self.base / "state" / "journal.db")               # the asker edits
        j.record("C_CUST", "1.0", sender_id="U_ASKER", text="Can you deploy to PROD?",
                 kind="QUESTION")
        j.close()
        at = self.buttons(at, "send-")[0].click().run()
        self.assertFalse(at.exception)
        self.assertEqual(self.rows()[0]["state"], "COMMITTED")
        snap = snapshot(self.base / "state" / "journal.db", {})
        flagged = [(i["where"], i["ts"]) for i in snap["attention"]
                   if i["severity"] == "edited_after_response"]
        self.assertEqual(flagged, [("C_CUST", "1.0")],
                         "the ask was edited after the answer was written and the "
                         "dashboard reads it as cleanly answered — the edit was laundered")
        responded_at = self.journal_row("C_CUST", "1.0")[0]
        self.assertIsNotNone(responded_at)
        self.assertLess(responded_at, Journal(self.base / "state" / "journal.db")
                        .revisions("C_CUST", "1.0")[-1]["recorded_at"])

    # ---- F8 A: tie ONLY on proof ---------------------------------------------------
    def test_an_in_flight_row_is_never_tied(self):
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        self.seed_ask("C_CUST", "1.0")
        at = self.run_app()
        at = self.buttons(at, "reply|C_CUST|1.0")[0].click().run()
        at = self.stage_text(at, "on it")
        key = self.rows()[0]["key"]
        conn = sqlite3.connect(str(self.base / "state" / "outbox-team.db"))
        conn.execute("UPDATE outbox SET state='INTENT' WHERE key=?", (key,))
        conn.commit(); conn.close()
        at = self.buttons(at, "send-")[0].click().run()
        self.assertFalse(at.exception)
        self.assertIsNone(self.journal_row("C_CUST", "1.0")[0],
                          "an unproven (in-flight) send was recorded as the answer")
        warnings = "\n".join(str(w.value) for w in at.warning)
        self.assertIn("in flight", warnings)
        self.assertNotIn("sent and proven", "\n".join(str(x.value) for x in at.success))

    # ---- F8 B: the first proven answer keeps the tie ---------------------------------
    def test_the_first_proven_answer_keeps_the_tie(self):
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        self.seed_ask("C_CUST", "1.0")
        at = self.run_app()
        at = self.buttons(at, "reply|C_CUST|1.0")[0].click().run()
        at = self.stage_text(at, "first answer")
        at = self.buttons(at, "reply|C_CUST|1.0")[0].click().run()
        at = self.stage_text(at, "second answer")
        first_key = [r["key"] for r in self.rows() if r["text"] == "first answer"][0]
        at = [b for b in self.buttons(at, "send-") if b.key == f"send-{first_key}"][0].click().run()
        first_at, first_tie = self.journal_row("C_CUST", "1.0")
        at = self.buttons(at, "send-")[0].click().run()          # the second draft
        self.assertFalse(at.exception)
        self.assertEqual(self.journal_row("C_CUST", "1.0"), (first_at, first_tie),
                         "a later send overwrote the first answer's tie")

    # ---- F8 C: a demotion after Reply drops the banner, never crashes ------------------
    def test_a_demotion_after_reply_drops_the_banner_without_a_crash(self):
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        self.seed_ask("C_CUST", "1.0")
        at = self.run_app()
        at = self.buttons(at, "reply|C_CUST|1.0")[0].click().run()
        self.assertTrue(any("replying to" in str(i.value) for i in at.info))
        settings = self.base / "settings.json"
        settings.write_text(settings.read_text().replace('"reply_policy": "staged"', '"label": "ro"'))
        at = at.run()
        self.assertFalse(at.exception)
        self.assertFalse(any("replying to" in str(i.value) for i in at.info),
                         "the reply banner outlived the channel's demotion")

    # ---- F8 D/E/G/F: the flashes say what happened; no journal is minted -------------
    def test_a_free_compose_send_flashes_no_tie_and_mints_no_journal(self):
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        at = self.compose(self.run_app(), DRAFT)
        successes = "\n".join(str(x.value) for x in at.success)
        self.assertIn("staged", successes)
        self.assertNotIn("as a reply", successes, "a free compose was described as a reply")
        at = self.buttons(at, "send-")[0].click().run()
        self.assertFalse(at.exception)
        successes = "\n".join(str(x.value) for x in at.success)
        self.assertIn("sent and proven", successes)
        self.assertNotIn("recorded as answered", successes,
                         "a free compose flashed a tie it cannot have made")
        self.assertFalse((self.base / "state" / "journal.db").is_file(),
                         "a Send with no journal minted journal.db — the viewer would "
                         "then show a healthy zero instead of 'No journal file yet'")

    def test_the_reply_flash_and_card_state_the_placement(self):
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        self.seed_ask("C_CUST", "1.0")
        at = self.run_app()
        at = self.buttons(at, "reply|C_CUST|1.0")[0].click().run()
        at = self.stage_text(at, "threaded answer")
        successes = "\n".join(str(x.value) for x in at.success)
        self.assertIn("as a reply to ts 1.0, in its thread", successes)
        cards = "\n".join(str(m.value) for m in at.markdown if "<b>STAGED</b>" in str(m.value))
        self.assertIn("in reply to ts 1.0", cards)
        self.assertIn("(in thread 1.0)", cards)

    # ---- F8 H: cancel actually cancels ------------------------------------------------
    def test_cancel_reply_returns_to_a_free_compose(self):
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        self.seed_ask("C_CUST", "1.0")
        at = self.run_app()
        at = self.buttons(at, "reply|C_CUST|1.0")[0].click().run()
        at = self.buttons(at, "reply_cancel")[0].click().run()
        self.assertFalse(any("replying to" in str(i.value) for i in at.info),
                         "cancel did not cancel")
        at = self.stage_text(at, "a plain message")
        row = self.rows()[0]
        self.assertTrue(row["trigger_ts"].startswith("ui-compose-"),
                        "after cancel the draft still staged as a reply")
        self.assertIsNone(self.journal_row("C_CUST", "1.0")[0])

    # ---- N2: an ask answered elsewhere drops the banner --------------------------------
    def test_an_ask_answered_elsewhere_drops_the_reply_banner(self):
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        self.seed_ask("C_CUST", "1.0")
        at = self.run_app()
        at = self.buttons(at, "reply|C_CUST|1.0")[0].click().run()
        j = Journal(self.base / "state" / "journal.db")
        j.mark_responded("C_CUST", "1.0", "thread-reply:1.5")          # the sweep got there
        j.close()
        at = at.run()
        self.assertFalse(at.exception)
        self.assertFalse(any("Sending will record" in str(i.value) for i in at.info),
                         "the banner still promises to record an ask that is closed")
        infos = "\n".join(str(i.value) for i in at.info)
        self.assertIn("no longer open", infos)

    # ---- a journal refusal after a PROVEN send: warning, kept open, real retry --------
    def test_a_journal_lock_after_a_proven_send_warns_and_the_retry_ties(self):
        os.environ["COMMS_UI_WRITE_ENABLED"] = "true"
        self.seed_ask("C_CUST", "1.0")
        at = self.run_app()
        at = self.buttons(at, "reply|C_CUST|1.0")[0].click().run()
        at = self.stage_text(at, "reviewed — go ahead")
        # BEGIN IMMEDIATE takes the RESERVED lock: readers (the viewer's mode=ro
        # snapshot) still read, the tie's UPDATE waits out sqlite's busy timeout
        # and fails — the shape of a long scheduler write transaction.
        lock = sqlite3.connect(str(self.base / "state" / "journal.db"))
        lock.execute("BEGIN IMMEDIATE")
        lock.execute("UPDATE journal SET seen_count=seen_count WHERE 0")
        try:
            at = self.buttons(at, "send-")[0].click().run()
        finally:
            lock.rollback()
            lock.close()
        self.assertFalse(at.exception, "a locked journal crashed the page after the send")
        self.assertEqual(self.rows()[0]["state"], "COMMITTED", "the send itself happened")
        self.assertIsNone(self.journal_row("C_CUST", "1.0")[0])
        warnings = "\n".join(str(w.value) for w in at.warning)
        self.assertIn("FAILED", warnings, "the failed tie must be said out loud")
        self.assertEqual(len(self.buttons(at, "reply|C_CUST|1.0")), 1,
                         "the ask must still offer Reply — that is the retry")
        at = self.buttons(at, "reply|C_CUST|1.0")[0].click().run()
        at = self.stage_text(at, "reviewed — go ahead")       # dedupes to the proven row
        self.assertFalse(at.exception)
        at_, key = self.journal_row("C_CUST", "1.0")
        self.assertIsNotNone(at_, "the retry (same text) did not tie the proven answer")
        self.assertEqual(key, self.rows()[0]["key"])
        self.assertEqual(len(self.rows()), 1, "the retry minted a second message")


if __name__ == "__main__":
    unittest.main(verbosity=2)
