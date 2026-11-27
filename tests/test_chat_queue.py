"""Responder queue view tests (G143, 2026-11-14): fixture chat.state, chat.log, inbox.jsonl and chat-intake.last rendered
through core.chat_queue - the message being answered with its elapsed time, the waiting messages with their age, the last
answers with arrival-to-answer minutes, the last intake time, the responder process, and a restart in the log.
Stdlib only, nothing created outside the temp dir; the module must import no UI and no send layer."""
import ast
import calendar
import errno
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from core import chat_queue

HERE = Path(__file__).resolve().parent
NOW = 946713999.0  # 2000-01-01T08:06:39Z

LOG = """2000-01-01T04:32:38Z CHAT START poll=6s timeout=1260s channels=C_EXAMPLE_CHANNEL workdir=/wd (started); this process owns the Slack inbox while it runs
2000-01-01T05:38:16Z CHAT MSG ts=946705082.812805 channel=C_EXAMPLE_CHANNEL user=U_EXAMPLE_USER thread_ts=946694541.422527 reply_thread=946694541.422527 kind=model text="job 7 done"
2000-01-01T05:40:20Z CHAT START poll=6s timeout=1260s channels=C_EXAMPLE_CHANNEL workdir=/wd (restart); this process owns the Slack inbox while it runs
2000-01-01T05:40:26Z CHAT RESUME ts=946705082.812805 cut at 2000-01-01T05:38:16Z (model=cut) - the previous daemon's answer was cut by a restart; answering it again
2000-01-01T05:40:26Z CHAT MSG ts=946705082.812805 channel=C_EXAMPLE_CHANNEL user=U_EXAMPLE_USER thread_ts=946694541.422527 reply_thread=946694541.422527 kind=model text="job 7 done"
2000-01-01T05:49:59Z CHAT MODEL ts=946705082.812805 rc=0 duration=573s posted=yes transcript=/wd/slack/chat/x.out
2000-01-01T05:49:59Z CHAT REPLY ts=946705082.812805 kind=model ok posted=946705766.853350 reply_thread=946694541.422527
2000-01-01T07:51:23Z CHAT MSG ts=946713077.722969 channel=C_EXAMPLE_CHANNEL user=U_EXAMPLE_USER thread_ts=946694541.422527 reply_thread=946694541.422527 kind=model text="how do I point the sample job at the test queue?"
2000-01-01T07:53:06Z CHAT MODEL ts=946713077.722969 rc=0 duration=103s posted=yes transcript=/wd/slack/chat/y.out
2000-01-01T07:53:06Z CHAT REPLY ts=946713077.722969 kind=model ok posted=946713150.794502 reply_thread=946694541.422527
2000-01-01T07:53:17Z CHAT SKIP ts=946713150.794502 channel=C_EXAMPLE_CHANNEL own post (user U_EXAMPLE_USER_2)
2000-01-01T08:01:22Z CHAT JOB START ts=946713675.359260 key=C_EXAMPLE_CHANNEL/946707267.693914 thread_ts=946707267.693914
2000-01-01T08:01:22Z CHAT MSG ts=946713675.359260 channel=C_EXAMPLE_CHANNEL user=U_EXAMPLE_USER thread_ts=946707267.693914 reply_thread=946707267.693914 kind=model text="is the report ready?"
"""

INBOX = """{"ts": "946713675.359260", "thread_ts": "946707267.693914", "channel": "C_EXAMPLE_CHANNEL", "user": "U_EXAMPLE_USER", "text": "is the report ready?"}
{"ts": "946713900.994525", "thread_ts": "946694541.422527", "channel": "C_EXAMPLE_CHANNEL", "user": "U_EXAMPLE_USER", "text": "the sample list shows\\nno rows yet"}
not json at all
"""


class ChatQueueTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="chatq-"))
        (self.d / "chat.log").write_text(LOG, encoding="utf-8")
        (self.d / "inbox.jsonl").write_text(INBOX, encoding="utf-8")
        (self.d / "chat.state").write_text("POLLING interval=6s handled=28 pid=%d utc=2000-01-01T08:06:33Z\nANSWERING ts=946713675.359260 key=C_EXAMPLE_CHANNEL/946707267.693914 pid=37399\n" % os.getpid(), encoding="utf-8")
        (self.d / "chat-intake.last").write_text("2000-01-01T08:05:57Z\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_the_message_being_answered_is_the_started_unfinished_one_with_its_elapsed_time(self):
        s = chat_queue.snapshot(self.d, now=NOW)
        self.assertEqual([r["ts"] for r in s["answering"]], ["946713675.359260"])
        a = s["answering"][0]
        self.assertEqual(a["thread"], "946707267.693914")
        self.assertEqual(a["since"], "2000-01-01T08:01:22Z")
        self.assertEqual(a["elapsed_s"], 317)
        self.assertEqual(a["text"], "is the report ready?")
        self.assertEqual(s["timeout_s"], "1260")

    def test_waiting_is_the_inbox_minus_the_one_being_answered_with_its_age(self):
        s = chat_queue.snapshot(self.d, now=NOW)
        self.assertEqual([r["ts"] for r in s["waiting"]], ["946713900.994525"])
        w = s["waiting"][0]
        self.assertEqual(w["age_s"], 98)
        self.assertEqual(w["thread"], "946694541.422527")
        self.assertNotIn("\n", w["text"])

    def test_the_last_answers_carry_arrival_to_answer_minutes_newest_first(self):
        s = chat_queue.snapshot(self.d, now=NOW)
        self.assertEqual([r["ts"] for r in s["answers"]], ["946713077.722969", "946705082.812805"])
        self.assertEqual(s["answers"][0]["arrival_to_answer_min"], 1.2)
        self.assertEqual(s["answers"][0]["duration_s"], "103")
        self.assertEqual(s["answers"][1]["arrival_to_answer_min"], 11.4)
        self.assertEqual(s["answers"][1]["duration_s"], "573")

    def test_a_restart_resets_the_in_flight_set_and_the_resume_is_listed(self):
        s = chat_queue.snapshot(self.d, now=NOW)
        self.assertEqual(s["last_start"], "2000-01-01T05:40:20Z")
        self.assertEqual(s["resumes"], ["946705082.812805"])
        self.assertNotIn("946705082.812805", [r["ts"] for r in s["answering"]])

    def test_state_intake_and_process_come_from_their_files(self):
        s = chat_queue.snapshot(self.d, now=NOW)
        self.assertEqual(s["state"]["status"], "POLLING")
        self.assertEqual(s["state"]["fields"]["handled"], "28")
        self.assertTrue(s["state"]["pid_alive"])
        self.assertEqual(len(s["state"]["running"]), 1)
        self.assertEqual(s["intake_last"], "2000-01-01T08:05:57Z")
        self.assertEqual(s["now_utc"], "2000-01-01T08:06:39Z")

    def test_the_liveness_probe_answers_alike_on_linux_and_macos(self):
        # kill(pid, 0), not /proc/<pid>: macOS has no /proc, so a live responder read as NOT RUNNING there
        self.assertTrue(chat_queue.pid_alive(os.getpid()))
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()  # reaped: its pid names no process now
        self.assertFalse(chat_queue.pid_alive(child.pid))
        (self.d / "chat.state").write_text("POLLING interval=6s handled=0 pid=%d\n" % child.pid, encoding="utf-8")
        self.assertIs(chat_queue.snapshot(self.d, now=NOW)["state"]["pid_alive"], False)
        for junk in ("abc", "-1", "²", "1" * 11):
            (self.d / "chat.state").write_text("POLLING interval=6s pid=%s\n" % junk, encoding="utf-8")
            self.assertIsNone(chat_queue.snapshot(self.d, now=NOW)["state"]["pid_alive"], junk)

    def test_eperm_is_alive_esrch_is_dead_and_pid_0_is_never_alive(self):
        foreign = 4194305  # above Linux's PID_MAX_LIMIT (2**22) and macOS's 99999: no process, no /proc entry
        with mock.patch.object(chat_queue.os, "kill", side_effect=PermissionError(errno.EPERM, "not permitted")):
            self.assertTrue(chat_queue.pid_alive(foreign))  # a responder run by another user is alive
        with mock.patch.object(chat_queue.os, "kill", side_effect=ProcessLookupError(errno.ESRCH, "no such process")):
            self.assertFalse(chat_queue.pid_alive(foreign))
        self.assertFalse(chat_queue.pid_alive(foreign))  # the real kill(2): ESRCH
        self.assertFalse(chat_queue.pid_alive(0))  # kill(0, 0) succeeds: it addresses our own process group

    @staticmethod
    def _elapsed_and_age(day):
        """(elapsed_s, age_s) of a minimal queue on `day`: answering since 14:48:36Z, one message waiting since 115 s
        before NOW, NOW 14:53:25Z. Both are UTC arithmetic, so the local timezone must not change them."""
        now = calendar.timegm(time.strptime(day + "T14:53:25Z", "%Y-%m-%dT%H:%M:%SZ"))
        d = Path(tempfile.mkdtemp(prefix="chatq-tz-"))
        try:
            (d / "chat.log").write_text('%sT14:48:36Z CHAT MSG ts=%d.954177 channel=C_EXAMPLE_CHANNEL user=U_EXAMPLE_USER '
                                        'kind=model text="is the report ready?"\n' % (day, now - 302), encoding="utf-8")
            (d / "inbox.jsonl").write_text('{"ts": "%d.281671", "channel": "C_EXAMPLE_CHANNEL", "user": "U_EXAMPLE_USER", '
                                           '"text": "the sample list shows no rows yet"}\n' % (now - 115), encoding="utf-8")
            s = chat_queue.snapshot(d, now=float(now))
            return s["answering"][0]["elapsed_s"], s["waiting"][0]["age_s"]
        finally:
            shutil.rmtree(d, ignore_errors=True)

    @staticmethod
    def _dst_on_exactly_one_of(days):
        return {time.localtime(calendar.timegm(time.strptime(day, "%Y-%m-%d"))).tm_isdst for day in days} == {0, 1}

    def test_elapsed_and_age_are_the_same_in_every_timezone(self):
        # the log's stamps are UTC: read as local time (mktime) they were an hour off wherever DST was in effect on the
        # logged date, e.g. New York in July and Sydney in January
        days = ("2000-07-01", "2000-01-01")
        saved = os.environ.get("TZ")

        def use(tz):
            os.environ["TZ"] = tz
            time.tzset()
        try:
            use("UTC")
            utc = [self._elapsed_and_age(day) for day in days]
            self.assertEqual(utc, [(289, 114), (289, 114)])
            for zone, rules in (("America/New_York", "EST5EDT,M3.2.0,M11.1.0"),
                                ("Australia/Sydney", "AEST-10AEDT,M10.1.0,M4.1.0/3")):
                use(zone)
                if not self._dst_on_exactly_one_of(days):
                    use(rules)  # this host has no zoneinfo for the name: the same zone, spelled as POSIX rules
                self.assertTrue(self._dst_on_exactly_one_of(days), zone + ": no DST in effect, so this proves nothing")
                self.assertEqual([self._elapsed_and_age(day) for day in days], utc, zone)
        finally:
            if saved is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = saved
            time.tzset()

    def test_without_intake_last_the_watch_log_is_read_and_missing_files_are_reported_never_created(self):
        (self.d / "chat-intake.last").unlink()
        (self.d / "chat-watch.log").write_text("2000-01-01T07:53:41Z Watch complete: 0 new\n", encoding="utf-8")
        self.assertEqual(chat_queue.snapshot(self.d, now=NOW)["intake_last"], "2000-01-01T07:53:41Z")
        empty = Path(tempfile.mkdtemp(prefix="chatq-empty-"))
        try:
            s = chat_queue.snapshot(empty, now=NOW)
            self.assertEqual(s["state"]["status"], "missing")
            self.assertIsNone(s["intake_last"])
            self.assertEqual((s["answering"], s["waiting"], s["answers"]), ([], [], []))
            self.assertEqual(os.listdir(empty), [])
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    def test_unreadable_lines_are_skipped_and_counted_never_a_crash(self):
        # each of these raised (or, for the START, was taken as a restart), and one bad line took down the whole page
        self.assertEqual(chat_queue.snapshot(self.d, now=NOW)["skipped"], {"chat.log": 0, "inbox.jsonl": 1})
        with (self.d / "chat.log").open("a", encoding="utf-8") as f:
            f.write('2000-01-01T08:06:39Z CHAT MSG ts=cut.1 channel=C_EXAMPLE_CHANNEL user=U_EXAMPLE_USER kind=model '
                    'text="a text cut by its own newline\nsecond line"\n')  # the logged line has no closing quote
            f.write("2000-01-01T08:06:39Z CHAT START poll=6s timeout=²s channels=C_EXAMPLE_CHANNEL workdir=/wd (restart)\n")
            f.write('2000-01-01T25:00:00Z CHAT MSG ts=hour25.1 channel=C_EXAMPLE_CHANNEL user=U_EXAMPLE_USER kind=model '
                    'text="no such hour"\n')
        with (self.d / "inbox.jsonl").open("a", encoding="utf-8") as f:
            f.write('[1, 2]\n42\n"just a string"\nnull\n{"ts": "x.1", "text": 5}\n' + "[" * 100000 + "\n")
            f.write('{"ts": "nan", "channel": "C_EXAMPLE_CHANNEL", "user": "U_EXAMPLE_USER", "text": "no age"}\n')
        s = chat_queue.snapshot(self.d, now=NOW)
        self.assertEqual(s["skipped"], {"chat.log": 3, "inbox.jsonl": 7})
        self.assertEqual([r["ts"] for r in s["answering"]], ["946713675.359260"])
        self.assertEqual((s["last_start"], s["timeout_s"]), ("2000-01-01T05:40:20Z", "1260"))
        self.assertEqual([(r["ts"], r["age_s"]) for r in s["waiting"]], [("946713900.994525", 98), ("nan", None)])

    def test_the_module_imports_no_ui_and_no_send_layer(self):
        tree = ast.parse((HERE.parent / "core" / "chat_queue.py").read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        self.assertTrue(names.isdisjoint({"streamlit", "subprocess", "requests", "urllib", "socket", "outbox", "delivery"}), names)


if __name__ == "__main__":
    unittest.main()
