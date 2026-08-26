"""core/parity.py — shadow-mode parity differ (gate G1, requirement R8).

The incumbent system is the ORACLE: it has been polling live channels for weeks. The
engine earns G1 by ingesting the same channels read-only and proving equivalence before
any send path exists.

Hard rule inherited from a real defect: **an empty comparison is an ERROR, never a pass.**
The repo's own secret gate once returned PASS while scanning zero files, and CI stayed
green. A differ that "passes" because it compared nothing is the same class of lie.

## Why a two-way diff is not enough (measured 2026-08-26, R8's first live window)

The first real run reported 342 missed and 24 extra on one channel and looked like a
catastrophic read-path defect. It was not. Asking the PLATFORM what it still serves for
that exact window settled it:

    window 2026-05-13..05-22   oracle=360   platform serves now=18   engine=18
    control 2026-05-11..05-13  oracle=43    platform serves now=43   engine=43
    control 2026-08-17..08-25  oracle=26    platform serves now=26   engine=26

Zero rows existed that the platform would serve and the engine lacked. All 342 were
messages the platform **no longer returns** — one app's 42-per-day burst, deleted since
the incumbent recorded it live — and all 24 "extra" were older than the oracle's own
retention floor, i.e. the engine was the *more* complete store. The engine had captured
100% of retrievable history and the differ still said FAIL.

The lesson generalizes past this incident: **the oracle is not ground truth, it is a
third-party archive that drifts from the platform in both directions.** Messages get
deleted; retention floors move; the incumbent has its own gaps. So parity has three
sides, not two:

    served     what the platform will return RIGHT NOW   (the only authority on loss)
    oracle     what the incumbent archived               (may hold what is now deleted)
    candidate  what the engine stored

Exactly one class of divergence is an engine defect: **the platform serves it, it is
inside the window the engine claims to have consumed, and the engine does not have it.**
That is `ENGINE_LOST`, and it can never be waived — `accept()` refuses it, so no amount
of configuration can turn a real loss into a pass. Every other class is legitimate on
some deployment, so the verdict is not "no divergence" but **"every divergence falls in
a class the caller has explicitly accepted"**, with acceptance a reviewable argument
rather than a silent tolerance. Accept nothing (the default) and this behaves exactly as
the strict two-way differ did.

Fail-closed everywhere the platform is silent: with no `served` set, every miss is
`ENGINE_LOST`; with an empty `served` set against a non-empty oracle, the comparison is
an ERROR (an empty platform snapshot would otherwise re-label every real loss
"unretrievable" — the vacuous-pass bug wearing a new hat).

CLI (read-only against the live oracle; never writes to it):
    python3 -m core.parity --oracle /path/to/live.db --candidate /path/to/engine.db \
        --channel C_EXAMPLE [--served-json served.json] \
        [--covered-through TS] [--accept UNRETRIEVABLE,PRE_ORACLE_FLOOR]
Exit 0 only when every divergence is accounted for; 1 on an unexplained divergence;
2 on an unusable comparison.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---- the divergence classes -------------------------------------------------
# A miss: present on the platform and/or in the oracle, absent from the engine.
ENGINE_LOST = "ENGINE_LOST"                  # platform serves it, in window, engine lacks it
UNRETRIEVABLE = "UNRETRIEVABLE"              # oracle has it, platform will not serve it
NOT_YET_POLLED = "NOT_YET_POLLED"            # newer than the engine's own cursor
BEFORE_ENGINE_START = "BEFORE_ENGINE_START"  # older than the engine's first poll
# An extra: the engine has it, the oracle does not.
AHEAD_OF_ORACLE = "AHEAD_OF_ORACLE"          # newer than anything the oracle holds
PRE_ORACLE_FLOOR = "PRE_ORACLE_FLOOR"        # older than the oracle's retention floor
ORACLE_MISSED = "ORACLE_MISSED"              # platform serves it; the INCUMBENT lost it
ENGINE_ONLY = "ENGINE_ONLY"                  # engine is the sole witness — bug, or deleted
UNCLASSIFIED = "UNCLASSIFIED"                # reached no rule: a hole in this taxonomy

# Waiving these two is never a decision a caller gets to make. ENGINE_LOST is the defect
# the gate exists to catch, and UNCLASSIFIED means the taxonomy above did not cover a real
# row — silently tolerating either would make a green run meaningless.
NEVER_ACCEPTABLE = frozenset({ENGINE_LOST, UNCLASSIFIED})


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
    # ts -> class name, for every divergent row on either side
    classified: dict = field(default_factory=dict)
    accepted: frozenset = frozenset()
    served_known: bool = False

    @property
    def cursor_divergent(self) -> bool:
        return (self.cursor_oracle is not None
                and self.cursor_candidate is not None
                and self.cursor_oracle != self.cursor_candidate)

    def counts(self) -> dict:
        """How many rows landed in each class, largest first."""
        out: dict = {}
        for cls in self.classified.values():
            out[cls] = out.get(cls, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def by_class(self, cls: str) -> set:
        return {ts for ts, c in self.classified.items() if c == cls}

    @property
    def unexplained(self) -> dict:
        """Classes that occurred and were NOT accepted. This is the verdict."""
        return {cls: n for cls, n in self.counts().items() if cls not in self.accepted}

    @property
    def ok(self) -> bool:
        return not self.unexplained and not self.cursor_divergent

    def summary(self) -> str:
        verdict = "PARITY OK" if self.ok else "PARITY FAIL"
        lines = [
            f"{verdict} channel={self.channel}",
            f"  oracle={self.oracle_count} candidate={self.candidate_count}",
            f"  missed(in oracle, not in engine)={len(self.missed)}",
            f"  extra(in engine, not in oracle)={len(self.extra)}",
        ]
        if not self.served_known:
            lines.append("  platform snapshot: ABSENT — every miss counted as ENGINE_LOST "
                         "(fail-closed: without it, a loss and a deletion are "
                         "indistinguishable)")
        for cls, n in self.counts().items():
            mark = "accepted" if cls in self.accepted else "UNEXPLAINED"
            lines.append(f"  {cls}={n} [{mark}]")
        if self.cursor_oracle or self.cursor_candidate:
            lines.append(f"  cursor oracle={self.cursor_oracle} "
                         f"candidate={self.cursor_candidate} "
                         f"divergent={self.cursor_divergent}")
        # Sample the ACTIONABLE class first: an operator reading a truncated log should
        # see the rows that mean a defect, not the first ten sorted timestamps.
        for cls in (ENGINE_LOST, UNCLASSIFIED):
            for ts in sorted(self.by_class(cls))[:10]:
                lines.append(f"    {cls} ts={ts}")
        for ts in sorted(self.missed)[:10]:
            lines.append(f"    MISSED ts={ts} [{self.classified.get(ts, '?')}]")
        for ts in sorted(self.extra)[:10]:
            lines.append(f"    EXTRA  ts={ts} [{self.classified.get(ts, '?')}]")
        return "\n".join(lines)


def _fl(ts) -> float:
    """Sortable value of a platform timestamp, tolerating non-numeric ids.

    A ts that will not parse must not silently compare as 0.0 — that would place it
    below every retention floor and quietly earn a benign class. Refuse instead.
    """
    try:
        return float(ts)
    except (TypeError, ValueError) as ex:
        raise ParityError(f"timestamp {ts!r} is not orderable — cannot place it in a "
                          "retention window, and guessing would invent a class") from ex


def compare(oracle_ts, candidate_ts, channel: str,
            cursor_oracle: str | None = None,
            cursor_candidate: str | None = None,
            served_ts=None,
            covered_from: str | None = None,
            covered_through: str | None = None,
            accept=()) -> ParityReport:
    """Diff the engine's store against the oracle, classified by cause.

    `served_ts` is the platform's own answer to "what would you return right now?" —
    data, not a plugin, so `core/` stays platform-free (R11). Omit it and every miss is
    ENGINE_LOST: without the platform's word, a message we lost and a message someone
    deleted look identical, and the safe reading of an ambiguous loss is that it is ours.

    `covered_through` is the engine's CURSOR, not the newest row it happens to hold. That
    distinction is load-bearing: inferring the window from the candidate's own maximum
    would let a lost newest message define itself out of the window and be waved through
    as "not yet polled".

    `accept` names the classes this deployment has decided are legitimate. ENGINE_LOST and
    UNCLASSIFIED are refused — see NEVER_ACCEPTABLE.

    Raises ParityError when the ORACLE side is empty (nothing to compare against), or when
    a `served_ts` set is supplied but empty while the oracle is not (an empty platform
    snapshot would re-label every real loss as a deletion).
    """
    oracle_ts, candidate_ts = set(oracle_ts), set(candidate_ts)
    if not oracle_ts:
        raise ParityError(
            f"oracle returned 0 messages for channel {channel} — refusing to report "
            "parity on an empty comparison (a differ that compares nothing must not pass)")

    accepted = frozenset(accept)
    forbidden = accepted & NEVER_ACCEPTABLE
    if forbidden:
        raise ParityError(
            f"refusing to accept {sorted(forbidden)} as explained divergence: "
            "ENGINE_LOST is the defect this gate exists to catch and UNCLASSIFIED means "
            "the taxonomy missed a real row — neither is waivable by configuration")

    served_known = served_ts is not None
    served = set(served_ts) if served_known else set()
    if served_known and not served:
        raise ParityError(
            f"the platform snapshot for {channel} is empty while the oracle holds "
            f"{len(oracle_ts)} messages — refusing to compare: an empty snapshot would "
            "classify every genuine loss as 'deleted upstream'")

    o_lo, o_hi = min(map(_fl, oracle_ts)), max(map(_fl, oracle_ts))
    through = _fl(covered_through) if covered_through is not None else None
    since = _fl(covered_from) if covered_from is not None else None

    classified: dict = {}

    # ---- misses: anything the platform or the oracle has and the engine does not ----
    # The universe is served|oracle, not just the oracle: a message the platform serves
    # and the engine lacks is a loss whether or not the incumbent happened to catch it.
    for ts in (served | oracle_ts) - candidate_ts:
        v = _fl(ts)
        if through is not None and v > through:
            classified[ts] = NOT_YET_POLLED          # the engine never claimed this far
        elif since is not None and v < since:
            classified[ts] = BEFORE_ENGINE_START     # predates this engine's first poll
        elif not served_known or ts in served:
            classified[ts] = ENGINE_LOST             # fail-closed when the platform is silent
        else:
            classified[ts] = UNRETRIEVABLE           # deleted/expired since the oracle saw it

    # ---- extras: the engine has it, the oracle does not ----------------------------
    for ts in candidate_ts - oracle_ts:
        v = _fl(ts)
        if v > o_hi:
            classified[ts] = AHEAD_OF_ORACLE         # engine polled ahead of the incumbent
        elif v < o_lo:
            classified[ts] = PRE_ORACLE_FLOOR        # deeper than the oracle's retention
        elif served_known and ts in served:
            classified[ts] = ORACLE_MISSED           # platform has it; the INCUMBENT lost it
        else:
            classified[ts] = ENGINE_ONLY             # sole witness: bug, or deleted since

    return ParityReport(
        channel=channel,
        oracle_count=len(oracle_ts),
        candidate_count=len(candidate_ts),
        missed=oracle_ts - candidate_ts,
        extra=candidate_ts - oracle_ts,
        cursor_oracle=cursor_oracle,
        cursor_candidate=cursor_candidate,
        classified=classified,
        accepted=accepted,
        served_known=served_known,
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


def read_served(path: str) -> set[str]:
    """Load a platform snapshot: a JSON list of timestamps the platform serves now.

    Data rather than an imported callable on purpose — `core/` never imports an adapter
    (R11), and a snapshot on disk is reviewable evidence after the fact, which a live
    call is not.
    """
    try:
        raw = json.loads(Path(path).read_text())
    except OSError as ex:
        raise ParityError(f"cannot read platform snapshot {path}: {ex}") from ex
    except ValueError as ex:
        raise ParityError(f"platform snapshot {path} is not valid JSON: {ex}") from ex
    if isinstance(raw, dict):                 # {"channel": [ts, ...]} is also accepted
        raw = [ts for tss in raw.values() for ts in tss]
    if not isinstance(raw, list):
        raise ParityError(f"platform snapshot {path} must be a JSON list of timestamps "
                          f"(or a channel->list object), got {type(raw).__name__}")
    return {str(t) for t in raw}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="shadow-mode parity differ (G1/R8)")
    ap.add_argument("--oracle", required=True, help="incumbent DB (opened READ-ONLY)")
    ap.add_argument("--candidate", required=True, help="engine store DB")
    ap.add_argument("--channel", required=True)
    ap.add_argument("--oracle-table", default="messages")
    ap.add_argument("--candidate-table", default="messages")
    ap.add_argument("--served-json", default=None,
                    help="JSON list of timestamps the PLATFORM still serves. Without it "
                         "every miss counts as ENGINE_LOST (fail-closed).")
    ap.add_argument("--covered-from", default=None,
                    help="engine's first-polled ts; older misses are BEFORE_ENGINE_START")
    ap.add_argument("--covered-through", default=None,
                    help="engine's CURSOR (not its newest row); newer misses are "
                         "NOT_YET_POLLED")
    ap.add_argument("--accept", default="",
                    help="comma-separated divergence classes this deployment has "
                         "explained. ENGINE_LOST and UNCLASSIFIED are refused.")
    a = ap.parse_args(argv)

    try:
        o = read_timestamps(a.oracle, a.channel, a.oracle_table)
        c = read_timestamps(a.candidate, a.channel, a.candidate_table)
        served = read_served(a.served_json) if a.served_json else None
        report = compare(o, c, a.channel, served_ts=served,
                         covered_from=a.covered_from,
                         covered_through=a.covered_through,
                         accept=[s.strip() for s in a.accept.split(",") if s.strip()])
    except ParityError as ex:
        print(f"PARITY ERROR: {ex}", file=sys.stderr)
        return 2
    print(report.summary())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
