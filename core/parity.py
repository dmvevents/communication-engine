"""core/parity.py — shadow-mode parity differ (gate G1, requirement R8).

The incumbent system is the ORACLE: it has been polling live channels for weeks. The
engine earns G1 by ingesting the same channels read-only and proving equivalence — zero
missed messages, zero cursor divergence — before any send path exists.

Hard rule inherited from a real defect: **an empty comparison is an ERROR, never a pass.**
The repo's own secret gate once returned PASS while scanning zero files, and CI stayed
green. A differ that "passes" because it compared nothing is the same class of lie.

CLI (read-only against the live oracle; never writes to it):
    python3 -m core.parity --oracle /path/to/live.db --candidate /path/to/engine.db \
        --channel C_EXAMPLE [--oracle-table messages]
Exit 0 only when parity holds; 1 on any divergence; 2 on an unusable comparison.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass, field


class ParityError(RuntimeError):
    """The comparison itself could not be performed. Never downgrade to 'pass'."""


@dataclass
class ParityReport:
    channel: str
    oracle_count: int
    candidate_count: int
    missed: set = field(default_factory=set)     # in oracle, absent from candidate
    extra: set = field(default_factory=set)      # in candidate, absent from oracle
    cursor_oracle: str | None = None
    cursor_candidate: str | None = None

    @property
    def cursor_divergent(self) -> bool:
        return (self.cursor_oracle is not None
                and self.cursor_candidate is not None
                and self.cursor_oracle != self.cursor_candidate)

    @property
    def ok(self) -> bool:
        return not self.missed and not self.extra and not self.cursor_divergent

    def summary(self) -> str:
        verdict = "PARITY OK" if self.ok else "PARITY FAIL"
        lines = [
            f"{verdict} channel={self.channel}",
            f"  oracle={self.oracle_count} candidate={self.candidate_count}",
            f"  missed(in oracle, not in engine)={len(self.missed)}",
            f"  extra(in engine, not in oracle)={len(self.extra)}",
        ]
        if self.cursor_oracle or self.cursor_candidate:
            lines.append(f"  cursor oracle={self.cursor_oracle} "
                         f"candidate={self.cursor_candidate} "
                         f"divergent={self.cursor_divergent}")
        for ts in sorted(self.missed)[:10]:
            lines.append(f"    MISSED ts={ts}")
        for ts in sorted(self.extra)[:10]:
            lines.append(f"    EXTRA  ts={ts}")
        return "\n".join(lines)


def compare(oracle_ts, candidate_ts, channel: str,
            cursor_oracle: str | None = None,
            cursor_candidate: str | None = None) -> ParityReport:
    """Diff two timestamp sets.

    Raises ParityError when the ORACLE side is empty: with nothing to compare against,
    'no misses' is vacuous. An empty candidate against a non-empty oracle is a legitimate
    (total) failure, not an error.
    """
    oracle_ts, candidate_ts = set(oracle_ts), set(candidate_ts)
    if not oracle_ts:
        raise ParityError(
            f"oracle returned 0 messages for channel {channel} — refusing to report "
            "parity on an empty comparison (a differ that compares nothing must not pass)")
    return ParityReport(
        channel=channel,
        oracle_count=len(oracle_ts),
        candidate_count=len(candidate_ts),
        missed=oracle_ts - candidate_ts,
        extra=candidate_ts - oracle_ts,
        cursor_oracle=cursor_oracle,
        cursor_candidate=cursor_candidate,
    )


def read_timestamps(db_path: str, channel: str, table: str = "messages",
                    channel_col: str = "channel_id", ts_col: str = "ts") -> set[str]:
    """Read a channel's timestamps READ-ONLY. Never opens the oracle writable."""
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as ex:
        raise ParityError(f"cannot open {db_path} read-only: {ex}") from ex
    try:
        rows = conn.execute(
            f"SELECT {ts_col} FROM {table} WHERE {channel_col}=?", (channel,)).fetchall()
    except sqlite3.Error as ex:
        raise ParityError(f"query failed on {db_path} ({table}.{ts_col}): {ex}") from ex
    finally:
        conn.close()
    return {str(r[0]) for r in rows}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="shadow-mode parity differ (G1/R8)")
    ap.add_argument("--oracle", required=True, help="incumbent DB (opened READ-ONLY)")
    ap.add_argument("--candidate", required=True, help="engine store DB")
    ap.add_argument("--channel", required=True)
    ap.add_argument("--oracle-table", default="messages")
    ap.add_argument("--candidate-table", default="messages")
    a = ap.parse_args(argv)

    try:
        o = read_timestamps(a.oracle, a.channel, a.oracle_table)
        c = read_timestamps(a.candidate, a.channel, a.candidate_table)
        report = compare(o, c, a.channel)
    except ParityError as ex:
        print(f"PARITY ERROR: {ex}", file=sys.stderr)
        return 2
    print(report.summary())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
