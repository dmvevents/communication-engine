"""The Check-for-new-messages handler (scripts/chat_intake.py, G143) against a STUB intake script.

Stdlib only, nothing created outside the temp dir, no network. The stub stands for /agent/slack-watch.sh: it records
every call (so "ran once" is a count, not a belief), prints the real script's line shapes, and takes its behaviour
from a mode file — ok, busy (the single-instance lock message + exit 0), fail (exit 1), slow (sleeps past the timeout)."""
import ast
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
import chat_intake  # noqa: E402

STUB = """#!/usr/bin/env bash
set -u
echo "call" >> "${SLACK_DIR}/stub-calls"
mode=$(cat "${SLACK_DIR}/stub-mode")
case "$mode" in
  busy) echo "Another slack-watch is already running (lock ${SLACK_DIR}/watch.lock held); exiting."; exit 0 ;;
  fail) echo "the inbox lock ${SLACK_DIR}/inbox.lock was not free within 60 s: nothing committed" >&2; exit 1 ;;
  slow) sleep 5; echo "Watch complete: late"; exit 0 ;;
  *) echo '{"ts":"946765065.639553","text":"ping"}' >> "${SLACK_DIR}/inbox.jsonl"
     echo "Watch complete: 2000-01-01T22:17:51Z new=1 replies=0 deferred=0 inbox=1"; exit 0 ;;
esac
"""


class HandlerTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="chat-intake-"))
        self.slack = self.base / "slack"
        self.slack.mkdir()
        self.script = self.base / "slack-watch.sh"
        self.script.write_text(STUB, encoding="utf-8")
        os.chmod(self.script, 0o755)
        self.mode("ok")

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def mode(self, word):
        (self.slack / "stub-mode").write_text(word + "\n", encoding="utf-8")

    def calls(self):
        p = self.slack / "stub-calls"
        return len(p.read_text().splitlines()) if p.exists() else 0

    def run_once(self, **kw):
        return chat_intake.run_once(script=str(self.script), slack_dir=str(self.slack), **kw)

    def test_ok_runs_the_script_exactly_once_and_reports_the_watch_complete_line(self):
        r = self.run_once()
        self.assertEqual(r["status"], "ok", r)
        self.assertEqual(r["rc"], 0)
        self.assertEqual(self.calls(), 1)
        self.assertTrue(r["summary"].startswith("Watch complete: "), r["summary"])
        self.assertIn("new=1", r["summary"])
        self.assertTrue((self.slack / "inbox.jsonl").exists(), "the stub's append landed in SLACK_DIR")

    def test_busy_lock_is_reported_cleanly_not_as_a_failure(self):
        self.mode("busy")
        r = self.run_once()
        self.assertEqual(r["status"], "busy", r)
        self.assertEqual(r["rc"], 0)
        self.assertIn("lock", r["summary"])
        self.assertEqual(self.calls(), 1)

    def test_failure_carries_the_scripts_own_last_line_and_rc(self):
        self.mode("fail")
        r = self.run_once()
        self.assertEqual(r["status"], "failed", r)
        self.assertEqual(r["rc"], 1)
        self.assertIn("inbox lock", r["summary"])

    def test_a_recorded_429_pause_stops_the_handler_before_the_script_runs(self):
        (self.slack / "retry-after").write_text("30\n", encoding="utf-8")   # the real script's shape: seconds
        r = self.run_once()
        self.assertEqual(r["status"], "paused", r)
        self.assertEqual(self.calls(), 0, "the script must not run during the pause")
        self.assertIn("rate limit", r["summary"])
        # a pause written long ago has elapsed: the script runs
        old = time.time() - 120
        os.utime(self.slack / "retry-after", (old, old))
        r = self.run_once()
        self.assertEqual(r["status"], "ok", r)
        self.assertEqual(self.calls(), 1)

    def test_timeout_is_a_status_not_an_exception(self):
        self.mode("slow")
        r = self.run_once(timeout_s=1)
        self.assertEqual(r["status"], "timeout", r)
        self.assertEqual(self.calls(), 1)

    def test_missing_script_is_said_not_raised(self):
        r = chat_intake.run_once(script=str(self.base / "nope.sh"), slack_dir=str(self.slack))
        self.assertEqual(r["status"], "missing", r)
        self.assertEqual(self.calls(), 0)

    def test_the_handler_imports_no_send_layer_and_no_ui(self):
        tree = ast.parse((HERE.parent / "scripts" / "chat_intake.py").read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        self.assertTrue(names.isdisjoint({"streamlit", "requests", "urllib", "socket", "core"}), names)

    def test_the_dashboard_wires_the_button_to_the_handler_and_the_queue_panel(self):
        src = (HERE.parent / "scripts" / "dashboard.py").read_text(encoding="utf-8")
        self.assertIn('button("⇄ Check for new messages"', src)
        self.assertIn("chat_intake.run_once()", src)
        self.assertIn("chat_queue_snapshot(slack_dir)", src)
        self.assertIn("SLACK_CHAT_POLL_INTERVAL", src, "the UI hint names the continuous intake's interval")
        tree = ast.parse(src)
        names = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertNotIn("core.outbox", names, "the dashboard stays a viewer: no send layer")


if __name__ == "__main__":
    unittest.main()
