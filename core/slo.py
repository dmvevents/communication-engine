"""core/slo.py — detection-latency SLO: push-vs-poll arrival delta, poll as ground truth
(ENH-2).

The incumbent's headline win — "median detection 12.1min -> ~1min" — measured how fast
its own cron ran, not whether anything was detected sooner or at all. The honest metric
needs THREE timestamps per message, all recorded by core/store.py at ingest
(first-write-wins, so re-polls cannot move them):

    message ts      the platform's own timestamp (when it was said)
    push arrival    when the push store (channels/slack_socket) first saw it
    poll arrival    when the completeness-poll store first CONFIRMED it

The poll side is the ground truth: only poll-confirmed messages are judged, and a
poll-confirmed message that push never delivered is a FAILURE of the push path, not a
statistic — the same silent-loss class the parity watch (scripts/push-poll-parity.py)
fail-stops on, here made a continuously checkable SLO. Two deltas are exposed as
p50/p90:

    detect  push_arrival - message_ts   (how long until the engine knew; the SLO budget
                                         applies here)
    lead    poll_arrival - push_arrival (how far ahead of the truth poll push ran — the
                                         number the incumbent's headline should have been)

Arrivals are wall-clock stamps: comparing two stores assumes they were fed on the same
host or on synced clocks. Skew shows up as a negative delta — reported, never hidden.

Inherited hard rule (R8): an empty or unmeasurable comparison is an ERROR, never a
pass. A judge that measured nothing must not bless the push path.

CLI (both stores opened READ-ONLY):
    python3 -m core.slo --poll-db state/poll.db --push-db state/push.db \
        --channel C_EXAMPLE --slo-p50 60 --slo-p90 300
Exit 0 = within budget, nothing missed; 1 = breach or miss; 2 = unusable comparison.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass, field


class SLOError(RuntimeError):
    """The measurement itself could not be performed. Never downgrade to 'pass'."""


def percentile(values, p: int) -> float:
    """Nearest-rank percentile: the value at index ceil(n*p/100)-1 of the sorted
    series. Always a real observed value, never an interpolation — an SLO judged on an
    invented midpoint is judged on data that never happened. Integer ceil-division
    because float rounding on n*0.9 drifts the rank at exact multiples."""
    if not values:
        raise SLOError("percentile of an empty series — nothing was measured")
    s = sorted(values)
    k = -(-(len(s) * p) // 100)
    return s[max(1, k) - 1]


@dataclass
class SLOReport:
    channel: str
    ground_truth: int              # poll-confirmed rows (message + arrival stamp)
    measured: int                  # rows with both arrivals and a numeric message ts
    unmeasured: int                # push has the message but no arrival stamp
    missed: set = field(default_factory=set)   # poll-confirmed, never delivered by push
    slo_p50_s: float = 0.0
    slo_p90_s: float = 0.0
    detect_p50_s: float | None = None
    detect_p90_s: float | None = None
    lead_p50_s: float | None = None
    lead_p90_s: float | None = None

    @property
    def breached(self) -> bool:
        if self.detect_p50_s is None or self.detect_p90_s is None:
            # Only reachable alongside a non-empty `missed` (measure() refuses to
            # return a report that measured nothing AND missed nothing), and a miss
            # already fails `ok` — so absent percentiles never bless anything.
            return False
        return (self.detect_p50_s > self.slo_p50_s
                or self.detect_p90_s > self.slo_p90_s)

    @property
    def ok(self) -> bool:
        return not self.missed and not self.breached

    def summary(self) -> str:
        verdict = "SLO OK" if self.ok else "SLO FAIL"
        lines = [
            f"{verdict} channel={self.channel}",
            f"  ground-truth(poll-confirmed)={self.ground_truth} "
            f"measured={self.measured} unmeasured={self.unmeasured} "
            f"missed={len(self.missed)}",
        ]
        if self.detect_p50_s is not None:
            lines.append(
                f"  detect(push_arrival - message_ts) p50={self.detect_p50_s:.3f}s "
                f"p90={self.detect_p90_s:.3f}s "
                f"(budget p50<={self.slo_p50_s}s p90<={self.slo_p90_s}s)"
                + ("  BREACHED" if self.breached else ""))
            lines.append(
                f"  lead(poll_arrival - push_arrival) p50={self.lead_p50_s:.3f}s "
                f"p90={self.lead_p90_s:.3f}s")
        for ts in sorted(self.missed)[:10]:
            lines.append(f"    MISSED by push, confirmed by poll: ts={ts}")
        return "\n".join(lines)


def _read_store(db_path: str, channel: str):
    """(message ts set, ts -> first arrival) for one channel, strictly READ-ONLY.

    A failed query raises — parity.py's rule: a schema mismatch must never look like
    an empty channel. A store predating arrival tracking fails here loudly."""
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as ex:
        raise SLOError(f"cannot open {db_path} read-only: {ex}") from ex
    try:
        msgs = {str(r[0]) for r in conn.execute(
            "SELECT ts FROM messages WHERE channel_id=?", (channel,))}
        arrivals = {str(r[0]): float(r[1]) for r in conn.execute(
            "SELECT ts, arrived_at FROM arrivals WHERE channel_id=?", (channel,))}
    except sqlite3.Error as ex:
        raise SLOError(f"query failed on {db_path}: {ex}") from ex
    finally:
        conn.close()
    return msgs, arrivals


def measure(poll_db: str, push_db: str, channel: str,
            slo_p50_s: float, slo_p90_s: float) -> SLOReport:
    """Judge one channel. Raises SLOError when there is nothing to judge — an empty
    ground truth or a pair with no measurable arrivals AND no misses is 'cannot
    conclude', never 'healthy'."""
    poll_msgs, poll_arr = _read_store(poll_db, channel)
    push_msgs, push_arr = _read_store(push_db, channel)

    truth = set(poll_arr) & poll_msgs
    if not truth:
        raise SLOError(
            f"no poll-confirmed ground truth for channel {channel} — the truth side is "
            "empty or was never arrival-tracked; refusing to judge the push path "
            "against nothing")

    missed = {ts for ts in truth if ts not in push_msgs}
    detect_s, lead_s = [], []
    unmeasured = 0
    for ts in truth - missed:
        push_at = push_arr.get(ts)
        if push_at is None:
            unmeasured += 1
            continue
        try:
            message_ts = float(ts)
        except ValueError:
            raise SLOError(
                f"message ts {ts!r} on channel {channel} is not numeric — cannot "
                "compute a latency from it, and skipping it silently would shrink "
                "the sample without anyone deciding that") from None
        detect_s.append(push_at - message_ts)
        lead_s.append(poll_arr[ts] - push_at)

    if not missed and not detect_s:
        raise SLOError(
            f"no measurable (push, poll) arrival pair on channel {channel} "
            f"({unmeasured} unmeasured) — nothing was judged, so nothing can pass")

    report = SLOReport(channel=channel, ground_truth=len(truth),
                       measured=len(detect_s), unmeasured=unmeasured, missed=missed,
                       slo_p50_s=slo_p50_s, slo_p90_s=slo_p90_s)
    if detect_s:
        report.detect_p50_s = percentile(detect_s, 50)
        report.detect_p90_s = percentile(detect_s, 90)
        report.lead_p50_s = percentile(lead_s, 50)
        report.lead_p90_s = percentile(lead_s, 90)
    return report


def slo_check(name: str, poll_db: str, push_db: str, channel: str,
              slo_p50_s: float, slo_p90_s: float):
    """The SLO as a core/checks.py check: FAIL on miss, breach, or an unusable
    comparison — a registry check must emit a verdict, never raise past the runner."""
    from core.checks import Verdict
    try:
        report = measure(poll_db, push_db, channel,
                         slo_p50_s=slo_p50_s, slo_p90_s=slo_p90_s)
    except SLOError as ex:
        return Verdict.failed(name, f"cannot conclude: {ex}")
    if report.missed:
        return Verdict.failed(
            name, f"push missed {len(report.missed)} poll-confirmed message(s) on "
                  f"{channel}: {sorted(report.missed)[:5]}",
            inspected=report.ground_truth)
    if report.breached:
        return Verdict.failed(
            name, f"detection-latency SLO breached on {channel}: "
                  f"p50={report.detect_p50_s:.3f}s (budget {slo_p50_s}s) "
                  f"p90={report.detect_p90_s:.3f}s (budget {slo_p90_s}s)",
            inspected=report.measured)
    return Verdict.passed(
        name, inspected=report.measured,
        detail=f"detect p50={report.detect_p50_s:.3f}s p90={report.detect_p90_s:.3f}s "
               f"within budget; lead p50={report.lead_p50_s:.3f}s over the truth poll")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="detection-latency SLO: push-vs-poll arrival delta with the "
                    "completeness poll as ground truth (ENH-2)")
    ap.add_argument("--poll-db", required=True,
                    help="store fed by the polling adapter (the truth side)")
    ap.add_argument("--push-db", required=True,
                    help="store fed by the push adapter")
    ap.add_argument("--channel", action="append", required=True, dest="channels",
                    help="channel id to judge (repeatable)")
    ap.add_argument("--slo-p50", type=float, required=True, dest="slo_p50_s",
                    help="p50 detection-latency budget in seconds")
    ap.add_argument("--slo-p90", type=float, required=True, dest="slo_p90_s",
                    help="p90 detection-latency budget in seconds")
    a = ap.parse_args(argv)

    worst = 0
    for channel in a.channels:
        try:
            report = measure(a.poll_db, a.push_db, channel,
                             slo_p50_s=a.slo_p50_s, slo_p90_s=a.slo_p90_s)
        except SLOError as ex:
            print(f"SLO ERROR: {ex}", file=sys.stderr)
            return 2
        print(report.summary())
        if not report.ok:
            worst = 1
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
