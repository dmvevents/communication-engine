"""Responder queue view for the Comms dashboard (G143, 2026-11-14).

What the Slack chat responder (/agent/slack-chat.sh) is answering right now, what waits in its inbox, what it answered
last and when it last fetched - read from the files the responder maintains under its state dir, so a slow answer is
VISIBLE and nobody restarts the harness to find out. Stdlib only, read only: no streamlit, no subprocess, no network,
nothing created. The Streamlit shell (scripts/dashboard.py) renders what `snapshot()` returns; any UI can.

Files read (the responder's own shapes, slack-chat.sh header "How a message is answered"):
  chat.state        line 1 ``POLLING interval=5s handled=N pid=P utc=...`` or ``STOPPED reason=...``; one more line per
                    running answer (the job table, with the parallel dispatcher)
  chat.log          ``<UTC> CHAT START ...``, ``CHAT MSG ts=.. [thread_ts=..] ...``, ``CHAT JOB START ts=..``,
                    ``CHAT MODEL ts=.. duration=..s`` (written after the model call returns), ``CHAT REPLY ts=.. posted=<ts>``,
                    ``CHAT UNANSWERED ts=..``, ``CHAT COVERED ts=.. by=..``, ``CHAT RESUME ts=..``
  inbox.jsonl       the messages not yet answered (ts, thread_ts, channel, user, text)
  chat-intake.last  the UTC time of the last Slack fetch (the continuous intake); older copies: the last ``Watch complete``
                    line of chat-watch.log

A line that cannot be read is skipped and counted in ``skipped``, never a crash of the page that renders the rest: an
inbox line that is not a JSON object or whose text is not a string; a MSG or JOB START line whose text="..." is cut off
(a newline in the text, say); a START line whose poll or timeout is not a whole number of seconds; and the start line
of a message still being answered whose stamp names no real time (hour 25), since its elapsed time cannot be computed.
"""
from __future__ import annotations

import calendar
import json
import math
import os
import re
import time
from pathlib import Path

_LOG = re.compile(r"^(?P<utc>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) CHAT (?P<kind>START|MSG|JOB START|JOB END|MODEL|REPLY|UNANSWERED|COVERED|RESUME|RESUME-SKIP|SKIP)\b(?P<rest>.*)$")
_TEXT = re.compile(r'text="(.*)"\s*$')
_SECONDS = re.compile(r"[0-9]*")  # ASCII digits or absent: int() refuses some str.isdigit() strings ("²")
_FINISHED = {"REPLY", "UNANSWERED", "COVERED"}
_STARTED = {"MSG", "JOB START"}


def _field(rest: str, name: str) -> str | None:
    m = re.search(r"(?:^|\s)%s=(\S+)" % re.escape(name), rest)
    return m.group(1) if m else None


def _utc_epoch(utc: str) -> float:
    # the stamp is UTC: timegm, never mktime, which reads it as local time (an hour off wherever DST is in effect)
    return float(calendar.timegm(time.strptime(utc, "%Y-%m-%dT%H:%M:%SZ")))


def _fmt_utc(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []


def pid_alive(pid: int) -> bool:
    """Whether process `pid` exists. kill(pid, 0) checks without delivering a signal and answers the same on Linux and
    macOS (macOS has no /proc): ESRCH is the only "dead"; EPERM means the process exists under another user, so it is
    alive. 0 and below name process groups, never one process."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):  # ESRCH, or a number no pid_t can hold
        return False
    except PermissionError:  # EPERM: it exists, owned by someone else
        return True
    return True


def read_state(slack_dir: Path) -> dict:
    """chat.state -> {'status': 'POLLING'|'STOPPED'|'ANSWERING'|'missing', 'fields': {...}, 'running': [line...], 'pid_alive': bool|None}."""
    lines = [l for l in _read_lines(slack_dir / "chat.state") if l.strip()]
    if not lines:
        return {"status": "missing", "fields": {}, "running": [], "pid_alive": None}
    first = lines[0].split()
    fields = dict(p.split("=", 1) for p in first[1:] if "=" in p)
    pid = fields.get("pid")
    # ASCII digits, at most 10: int() refuses some str.isdigit() strings ("²"), so junk reads as unknown, never a crash
    alive = pid_alive(int(pid)) if re.fullmatch(r"[0-9]{1,10}", pid or "") else None
    return {"status": first[0], "fields": fields, "running": lines[1:], "pid_alive": alive}


def read_intake_last(slack_dir: Path) -> str | None:
    """The UTC time of the last Slack fetch: chat-intake.last, else the last 'Watch complete' line's time in chat-watch.log."""
    lines = [l for l in _read_lines(slack_dir / "chat-intake.last") if l.strip()]
    if lines:
        return lines[-1].strip()
    for line in reversed(_read_lines(slack_dir / "chat-watch.log")):
        if "Watch complete" in line:
            m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)", line)
            return m.group(1) if m else line[:20]
    return None


def read_inbox(slack_dir: Path, now: float) -> tuple[list[dict], int]:
    """inbox.jsonl -> (the waiting messages, oldest first: ts, thread, channel, user, text (first 120 chars), age_s;
    the number of unreadable lines skipped: not JSON, not a JSON object, or a text that is not a string)."""
    out, skipped = [], 0
    for raw in _read_lines(slack_dir / "inbox.jsonl"):
        if not raw.strip():
            continue
        try:
            d = json.loads(raw)
        except (ValueError, RecursionError):  # RecursionError: nesting deeper than the parser's stack
            d = None
        if not isinstance(d, dict) or not isinstance(d.get("text") or "", str):
            skipped += 1
            continue
        ts = str(d.get("ts") or "")
        try:
            age = now - float(ts)
        except ValueError:
            age = None
        if age is not None and not math.isfinite(age):
            age = None  # "nan" or "inf" parse as floats; int() of them raises
        out.append({"ts": ts, "thread": str(d.get("thread_ts") or ts), "channel": d.get("channel", ""), "user": d.get("user", ""),
                    "text": (d.get("text") or "").replace("\n", " ")[:120], "age_s": None if age is None else max(0, int(age))})
    out.sort(key=lambda r: r["ts"])
    return out, skipped


def read_log(slack_dir: Path, now: float, limit: int = 10) -> dict:
    """chat.log -> start (the last CHAT START: utc, poll_s, timeout_s), answering (started, not finished: ts, thread, since,
    elapsed_s), answers (the last `limit` REPLY lines: ts, thread, posted, arrival_to_answer_min, duration_s), resumes,
    skipped (the number of unreadable lines: see the module docstring)."""
    start = None
    started: dict[str, dict] = {}
    finished: set[str] = set()
    durations: dict[str, str] = {}
    replies: list[dict] = []
    resumes: list[str] = []
    skipped = 0
    for line in _read_lines(slack_dir / "chat.log"):
        m = _LOG.match(line)
        if not m:
            continue
        utc, kind, rest = m.group("utc"), m.group("kind"), m.group("rest")
        ts = _field(rest, "ts")
        if kind == "START":
            poll, timeout = ((_field(rest, k) or "").rstrip("s") for k in ("poll", "timeout"))
            if not (_SECONDS.fullmatch(poll) and _SECONDS.fullmatch(timeout)):
                skipped += 1
                continue
            start = {"utc": utc, "poll_s": poll, "timeout_s": timeout}
            started, finished = {}, set()  # a restart: the previous daemon's answers are gone (part 4 re-answers the cut ones)
        elif kind in _STARTED and ts:
            t = _TEXT.search(rest) if 'text="' in rest else None
            if 'text="' in rest and not t:  # the text is cut off, e.g. by a newline in it
                skipped += 1
                continue
            text = t.group(1)[:120] if t else ""
            rec = started.setdefault(ts, {"ts": ts, "thread": _field(rest, "thread_ts") or _field(rest, "reply_thread") or ts, "since": utc, "text": text})
            if text and not rec["text"]:
                rec["text"] = text  # JOB START (dispatcher) precedes the MSG line that carries the text
        elif kind == "MODEL" and ts:
            durations[ts] = (_field(rest, "duration") or "").rstrip("s")
        elif kind in _FINISHED and ts:
            finished.add(ts)
            if kind == "REPLY":
                posted = _field(rest, "posted")
                a2a = None
                try:
                    a2a = round((float(posted) - float(ts)) / 60.0, 1)
                except (TypeError, ValueError):
                    pass
                replies.append({"ts": ts, "thread": _field(rest, "reply_thread") or ts, "posted": posted, "utc": utc,
                                "arrival_to_answer_min": a2a, "duration_s": durations.get(ts, ""), "kind": _field(rest, "kind") or ""})
        elif kind == "RESUME" and ts:
            resumes.append(ts)
    answering = []
    for ts, rec in started.items():
        if ts in finished:
            continue
        try:
            since = _utc_epoch(rec["since"])
        except ValueError:  # the stamp's digits matched, but it names no real time (hour 25, month 13)
            skipped += 1
            continue
        rec = dict(rec)
        rec["elapsed_s"] = max(0, int(now - since))
        answering.append(rec)
    answering.sort(key=lambda r: r["since"])
    return {"start": start, "answering": answering, "answers": replies[-limit:][::-1], "resumes": resumes,
            "skipped": skipped}


def snapshot(slack_dir: str | Path, now: float | None = None, limit: int = 10) -> dict:
    """Everything the Responder queue panel shows, from the files alone. `now` is injectable for tests."""
    slack_dir = Path(slack_dir)
    now = time.time() if now is None else now
    state = read_state(slack_dir)
    log = read_log(slack_dir, now, limit)
    inbox, inbox_skipped = read_inbox(slack_dir, now)
    answering_ts = {r["ts"] for r in log["answering"]}
    waiting = [r for r in inbox if r["ts"] not in answering_ts]
    timeout = log["start"]["timeout_s"] if log["start"] else ""
    return {"now_utc": _fmt_utc(now), "state": state, "intake_last": read_intake_last(slack_dir), "timeout_s": timeout,
            "answering": log["answering"], "waiting": waiting, "answers": log["answers"], "resumes": log["resumes"],
            "last_start": log["start"]["utc"] if log["start"] else None,
            "skipped": {"chat.log": log["skipped"], "inbox.jsonl": inbox_skipped}}
