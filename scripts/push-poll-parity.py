#!/usr/bin/env python3
"""scripts/push-poll-parity.py — continuous push-vs-poll parity (ENH-14).

Socket Mode push (channels/slack_socket) is sub-second but can MISS events —
the platform documents a 10-connection cap, connection cycling, and mid-flow
enablement drops. The polling adapter (channels/slack) is gap-free but slow.
So: push for latency, poll for truth, and THIS watch proves they agree —
continuously, because a loss that happens after startup is still a loss.

Watermark rule: push runs AHEAD of poll between poll cycles, always. Each cycle
therefore compares only the SETTLED window — rows at or below the poll store's
newest ts for that channel. A push row above the watermark is 'not yet
confirmable', never a divergence; the next cycle, after poll catches up, it
either matches or fails.

Built on core/parity.py and inheriting its discipline (R8): an empty poll
oracle is an ERROR, never a pass — a differ that compares nothing must not
bless whatever push does next.

Usage (both stores are engine stores; both opened READ-ONLY):
    python3 scripts/push-poll-parity.py --poll-db state/poll.db \
        --push-db state/push.db --channel C_EXAMPLE [--interval 60] [--cycles N]

Exit codes: 0 = every requested cycle held parity; 1 = divergence (fail-stop:
the push path can no longer be trusted, and the operator must look BEFORE it
silently loses more); 2 = the comparison itself was unusable.
"""
import argparse
import sys
import time
from pathlib import Path

# Runs from a fresh clone with no install step, so the repo root (this file's
# grandparent) goes on sys.path explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.parity import ParityError, compare, read_timestamps  # noqa: E402


def diff_channel(poll_db, push_db, channel):
    """One settled-window comparison. Poll is the oracle; push is the candidate."""
    poll_ts = read_timestamps(poll_db, channel)
    push_ts = read_timestamps(push_db, channel)
    if poll_ts:
        watermark = max(float(t) for t in poll_ts)
        push_ts = {t for t in push_ts if float(t) <= watermark}
    # compare() raises ParityError on an empty oracle — preserved, never downgraded.
    return compare(poll_ts, push_ts, channel)


def run(poll_db, push_db, channels, interval_s=60.0, cycles=None,
        sleep=time.sleep, out=print):
    """Diff every channel each cycle, re-reading both stores live. Returns 1 the
    moment any channel diverges; 0 after `cycles` clean cycles (None = run until
    divergence). ParityError propagates — an unusable comparison is the caller's
    problem, not a pass."""
    done = 0
    while True:
        for channel in channels:
            report = diff_channel(poll_db, push_db, channel)
            out(report.summary())
            if not report.ok:
                out(f"push-poll-parity: DIVERGENCE on {channel} after {done} clean "
                    "cycle(s) — stopping so the loss is investigated, not averaged "
                    "away by later clean reads")
                return 1
        done += 1
        if cycles is not None and done >= cycles:
            return 0
        sleep(interval_s)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="continuously diff socket-mode push ingestion against the "
                    "polling store (poll is the truth); fail-stop on divergence")
    ap.add_argument("--poll-db", required=True,
                    help="store fed by the polling adapter (the truth side)")
    ap.add_argument("--push-db", required=True,
                    help="store fed by the slack_socket adapter")
    ap.add_argument("--channel", action="append", required=True, dest="channels",
                    help="channel id to diff (repeatable)")
    ap.add_argument("--interval", type=float, default=60.0,
                    help="seconds between cycles (default 60)")
    ap.add_argument("--cycles", type=int, default=None,
                    help="stop clean after N cycles (default: run until divergence)")
    a = ap.parse_args(argv)
    try:
        return run(a.poll_db, a.push_db, a.channels,
                   interval_s=a.interval, cycles=a.cycles)
    except ParityError as ex:
        print(f"PARITY ERROR: {ex}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
