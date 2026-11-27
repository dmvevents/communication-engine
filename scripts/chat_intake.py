"""scripts/chat_intake.py — the "Check for new messages" button's handler (G143, 2026-11-14).

Runs the configured watch script (SLACK_WATCH_SCRIPT) ONCE, right away, so a message that just
arrived shows in the Journal within seconds instead of at the next poll. Nothing here posts, edits
or deletes. The script runs with this process's environment and does what it does. The default,
/agent/slack-watch.sh, is not shipped in this repo: the consumer's container image provides it,
and there it only reads Slack and appends to the responder's inbox under the inbox lock;
without it the button reports "missing". core/ stays free of this (no subprocess in core/chat_queue.py — its AST test pins that);
this module lives beside the Streamlit shell, like scripts/dashboard_write.py.

Three facts of the intake script this handler respects (read from its source):
  * single instance — it takes ``${SLACK_DIR}/watch.lock`` with ``flock -n`` and, when another run
    holds it, prints "Another slack-watch is already running ... exiting." and exits 0: reported
    here as ``busy``, not as a failure;
  * the inbox lock — ``flock -w 60`` on ``${SLACK_DIR}/inbox.lock``; a timeout exits 1 with its
    own message, reported verbatim;
  * the 429 pause — after an HTTP 429 the script records Slack's Retry-After in
    ``${SLACK_DIR}/retry-after`` for its callers to honor; while that pause runs the handler does
    NOT start the script (``paused``), so the button cannot add to a rate limit.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

DEFAULT_SCRIPT = "/agent/slack-watch.sh"
DEFAULT_SLACK_DIR = "/wd/slack"
BUSY_MARK = "already running"


def retry_after_remaining(slack_dir: Path, now: float | None = None) -> float:
    """Seconds left of a recorded 429 pause, 0 when none. The file holds either Slack's Retry-After
    seconds (counted from the file's mtime) or an absolute epoch deadline; junk counts as no pause."""
    now = time.time() if now is None else now
    path = Path(slack_dir) / "retry-after"
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").split()
        value = float(raw[0]) if raw else 0.0
        mtime = path.stat().st_mtime
    except (OSError, ValueError, IndexError):
        return 0.0
    deadline = value if value > 1e9 else mtime + value
    return max(0.0, deadline - now)


def run_once(script: str | None = None, slack_dir: str | None = None, timeout_s: int = 120,
             now: float | None = None) -> dict:
    """Run the configured watch script once. Returns {'status': 'ok'|'busy'|'paused'|'failed'|'missing'|
    'timeout', 'rc', 'summary', 'output', 'seconds', 'utc', 'script'}. The outcomes it expects come back as a
    status: no script, a recorded 429 pause, a held lock, a non-zero exit, a run past `timeout_s`. What it
    does not expect still raises: a retry-after value int() cannot take (inf), no `bash` to run the script
    with, or script output that is not valid UTF-8. It posts nothing itself; the script runs with this
    process's environment and does what it does."""
    script = script or os.environ.get("SLACK_WATCH_SCRIPT", DEFAULT_SCRIPT)
    slack_dir = Path(slack_dir or os.environ.get("SLACK_DIR", DEFAULT_SLACK_DIR))
    started = time.time()
    utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started))
    base = {"rc": None, "summary": "", "output": "", "seconds": 0.0, "utc": utc, "script": script}
    if not Path(script).is_file():
        return {**base, "status": "missing", "summary": f"intake script not found: {script}"}
    pause = retry_after_remaining(slack_dir, now)
    if pause > 0:
        return {**base, "status": "paused",
                "summary": f"Slack rate limit pause in force for {int(pause) + 1} s more "
                           f"({slack_dir / 'retry-after'}); not started"}
    env = dict(os.environ, SLACK_DIR=str(slack_dir))
    try:
        proc = subprocess.run(["bash", script], capture_output=True, text=True, timeout=timeout_s,
                              env=env, cwd=str(slack_dir) if slack_dir.is_dir() else None)
    except subprocess.TimeoutExpired as ex:
        out = (ex.stdout or b"").decode() if isinstance(ex.stdout, bytes) else (ex.stdout or "")
        return {**base, "status": "timeout", "seconds": round(time.time() - started, 1), "output": out,
                "summary": f"intake still running after {timeout_s} s; its lock and cursors are its own"}
    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    seconds = round(time.time() - started, 1)
    lines = [l for l in out.splitlines() if l.strip()]
    last = lines[-1] if lines else ""
    if proc.returncode == 0 and BUSY_MARK in out:
        status, summary = "busy", "another intake run holds the lock; it will fetch the same messages — nothing lost"
    elif proc.returncode == 0:
        status = "ok"
        done = [l for l in lines if l.startswith("Watch complete")]
        summary = done[-1] if done else (last or "intake finished (rc 0)")
    else:
        status, summary = "failed", f"intake exited {proc.returncode}: {last}"
    return {**base, "status": status, "rc": proc.returncode, "summary": summary, "output": out[-4000:],
            "seconds": seconds}
