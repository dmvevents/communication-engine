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

## Ordering is a capability of the id space, not an assumption (ENH-26)

Email identities are Message-ID strings no float() will ever parse, and they are the
case that turns `_fl()`'s refusal from defensive into load-bearing. The explicit rule:
a comparison is **orderable** only when EVERY id on every side parses as a float.
Uniformly non-orderable ids still get the full set-membership classification — telling
a loss from a deletion needs only the served set — but the four window classes
(NOT_YET_POLLED, BEFORE_ENGINE_START, AHEAD_OF_ORACLE, PRE_ORACLE_FLOOR), which only
mean anything on a timeline, become UNAVAILABLE rather than guessed: extras that would
have needed the oracle floor read ENGINE_ONLY, and asking for window placement
(`covered_from`/`covered_through`) is refused outright — sorting an unparseable id as
0.0 would let a real loss earn a benign class for free. A MIXED id space stays an
ERROR: half a timeline is corruption (two id shapes in one channel), never a mode.

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
import time
from dataclasses import dataclass, field
from pathlib import Path

from core.config import ConfigError, load_adapter_class

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


def snapshot_declaration(adapter, adapter_name):
    """None when `adapter` (class or instance) can answer "what do you still serve?" —
    otherwise the operator-facing declaration of the missing capability (ENH-27).

    retrievable_ts is optional and core degrades to fail-closed without it — correct,
    but silent. A push surface can NEVER supply the snapshot (channels/slack_socket
    receives events and has no history call; a Telegram bot cannot re-read updates it
    has acknowledged), so every parity run against its store is permanently fail-closed
    and the resulting ENGINE_LOST rows read as a read-path defect. That misreading is
    the measured R8 incident wearing a different cause; this declaration exists so the
    explanation is made once, in writing, instead of once per confused operator.

    Accepts the discovered CLASS as well as an instance, because the parity CLI has no
    auth to construct with — the capability answer must be the same either way.
    """
    if callable(getattr(adapter, "retrievable_ts", None)):
        return None
    return (f"adapter {adapter_name!r} cannot supply a platform snapshot: it has no "
            f"retrievable_ts (a push surface receives events and cannot ask the "
            f"platform what it still serves), so the verdicts {UNRETRIEVABLE} and "
            f"{ORACLE_MISSED} are unavailable and every miss reads {ENGINE_LOST} "
            "(fail-closed) — a capability gap of this adapter, not yet proof of a "
            "read-path defect")


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
    # snapshot_declaration()'s answer for the adapter feeding the candidate store;
    # printed only while the snapshot is genuinely absent (see summary()).
    snapshot_unavailable: str | None = None
    # False when the id space is uniformly non-orderable (email Message-IDs): the
    # window classes never fired and could not have — see the module docstring.
    orderable: bool = True

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

    def panel(self) -> dict:
        """The verdict-led, machine-readable view an operator surface renders (ENH-24).

        Key order IS render order — json and every dict consumer preserve it — and it
        encodes the lesson of R8's first live window: the raw missed/extra counts said
        '342 missed, 24 extra' for a channel whose truthful verdict was PARITY OK /
        ENGINE_LOST=0. A surface that leads with the scary raw number trains the
        operator to ignore it, and they will ignore it on the day ENGINE_LOST goes to
        1. So the ONE number that means a defect leads, the accepted classes are the
        stated reason the run is clean, and the raw counts sit at the tail — demoted,
        not deleted.

        `tombstones` is a slot, always None here: the differ has no retention db, and
        the operator surface fills it from core/retention.py's store. None renders as
        UNKNOWN, never as 0.
        """
        lost = sorted(self.by_class(ENGINE_LOST), key=_fl if self.orderable else str)
        return {
            "verdict": "PARITY OK" if self.ok else "PARITY FAIL",
            "engine_lost": len(lost),
            "engine_lost_sample": lost[:10],
            "unexplained": self.unexplained,
            "accepted": {cls: n for cls, n in self.counts().items()
                         if cls in self.accepted},
            "accept_list": sorted(self.accepted),
            "tombstones": None,
            "channel": self.channel,
            "oracle_count": self.oracle_count,
            "candidate_count": self.candidate_count,
            "served_known": self.served_known,
            "orderable": self.orderable,
            "snapshot_unavailable": self.snapshot_unavailable,
            "cursor_divergent": self.cursor_divergent,
            "raw": {"missed": len(self.missed), "extra": len(self.extra)},
        }

    def summary(self) -> str:
        verdict = "PARITY OK" if self.ok else "PARITY FAIL"
        lines = [
            f"{verdict} channel={self.channel}",
            f"  oracle={self.oracle_count} candidate={self.candidate_count}",
            f"  missed(in oracle, not in engine)={len(self.missed)}",
            f"  extra(in engine, not in oracle)={len(self.extra)}",
        ]
        if not self.served_known:
            if self.snapshot_unavailable:
                lines.append(f"  platform snapshot: UNAVAILABLE — "
                             f"{self.snapshot_unavailable}")
            else:
                lines.append("  platform snapshot: ABSENT — every miss counted as "
                             "ENGINE_LOST (fail-closed: without it, a loss and a "
                             "deletion are indistinguishable)")
        if not self.orderable:
            # Named, not implied: an operator who does not know the window classes
            # CANNOT fire on this id space would read their absence as a clean bill.
            lines.append(f"  identities not orderable (e.g. email Message-IDs) — "
                         f"window classes {NOT_YET_POLLED}, {BEFORE_ENGINE_START}, "
                         f"{AHEAD_OF_ORACLE}, {PRE_ORACLE_FLOOR} unavailable; "
                         "classification is by served-set membership only")
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


def _orderable(ids, channel: str) -> bool:
    """True when every id parses as a float, False when NONE does (email Message-IDs).

    A MIXED space raises: two id shapes in one channel means corruption — an adapter
    emitting two identity schemes, or two channels' rows in one comparison — and
    degrading it to the unordered mode would hide that. Probing through `_fl` on
    purpose: if its refusal is ever softened to a silent 0.0, every id reads
    orderable and the mixed-space test goes red."""
    unparseable = []
    for ts in ids:
        try:
            _fl(ts)
        except ParityError:
            unparseable.append(ts)
    if not unparseable:
        return True
    if len(unparseable) == len(set(ids)):
        return False
    bad = set(unparseable)
    ordered_example = next(ts for ts in ids if ts not in bad)
    raise ParityError(
        f"channel {channel} mixes orderable and non-orderable identities "
        f"(e.g. {ordered_example!r} vs {unparseable[0]!r}) — refusing to classify: "
        "half a timeline is id-space corruption, not a degraded mode")


def compare(oracle_ts, candidate_ts, channel: str,
            cursor_oracle: str | None = None,
            cursor_candidate: str | None = None,
            served_ts=None,
            covered_from: str | None = None,
            covered_through: str | None = None,
            accept=(),
            snapshot_unavailable: str | None = None) -> ParityReport:
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

    `snapshot_unavailable` is `snapshot_declaration()`'s answer for the adapter feeding
    the candidate store (ENH-27). It changes no classification — declared is explained,
    never excused — and is printed only while no `served_ts` exists: a snapshot supplied
    from elsewhere (the sibling polling adapter on the same platform) un-degrades the
    verdicts, and "cannot supply" would then misdescribe the run that happened.

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

    orderable = _orderable(served | oracle_ts | candidate_ts, channel)
    if not orderable and (covered_from is not None or covered_through is not None):
        # The _fl() refusal made load-bearing (ENH-26): these ids have no timeline to
        # place a window on, and sorting them as 0.0 would let a real loss earn
        # NOT_YET_POLLED or BEFORE_ENGINE_START for free.
        raise ParityError(
            f"channel {channel}: covered_from/covered_through place ids on a "
            "timeline, but these identities are not orderable — drop the window "
            "arguments; classification falls back to served-set membership")
    o_lo = min(map(_fl, oracle_ts)) if orderable else None
    o_hi = max(map(_fl, oracle_ts)) if orderable else None
    through = _fl(covered_through) if covered_through is not None else None
    since = _fl(covered_from) if covered_from is not None else None

    classified: dict = {}

    # ---- misses: anything the platform or the oracle has and the engine does not ----
    # The universe is served|oracle, not just the oracle: a message the platform serves
    # and the engine lacks is a loss whether or not the incumbent happened to catch it.
    for ts in (served | oracle_ts) - candidate_ts:
        v = _fl(ts) if orderable else None
        if orderable and through is not None and v > through:
            classified[ts] = NOT_YET_POLLED          # the engine never claimed this far
        elif orderable and since is not None and v < since:
            classified[ts] = BEFORE_ENGINE_START     # predates this engine's first poll
        elif not served_known or ts in served:
            classified[ts] = ENGINE_LOST             # fail-closed when the platform is silent
        else:
            classified[ts] = UNRETRIEVABLE           # deleted/expired since the oracle saw it

    # ---- extras: the engine has it, the oracle does not ----------------------------
    for ts in candidate_ts - oracle_ts:
        if orderable and _fl(ts) > o_hi:
            classified[ts] = AHEAD_OF_ORACLE         # engine polled ahead of the incumbent
        elif orderable and _fl(ts) < o_lo:
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
        snapshot_unavailable=snapshot_unavailable,
        orderable=orderable,
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
    ap.add_argument("--candidate-adapter", default=None,
                    help="channel type that FEEDS the candidate store (e.g. "
                         "slack_socket). If the discovered adapter cannot supply a "
                         "platform snapshot (no retrievable_ts), the report says so "
                         "by name, so its fail-closed ENGINE_LOST rows read as a "
                         "missing capability rather than a read-path defect.")
    ap.add_argument("--channels-dir", default="channels",
                    help="adapter discovery dir for --candidate-adapter (default: "
                         "./channels, the same dir config points core at)")
    ap.add_argument("--panel-json", default=None,
                    help="also write the verdict-led panel (ENH-24) here, e.g. "
                         "state/parity/panel-<channel>.json — the dashboard renders "
                         "these read-only; it never computes parity itself")
    a = ap.parse_args(argv)

    try:
        # A typo'd adapter name must not silently degrade to the generic ABSENT
        # line, so ConfigError shares ParityError's "unusable comparison" exit.
        declaration = None
        if a.candidate_adapter:
            cls = load_adapter_class(a.channels_dir, a.candidate_adapter)
            declaration = snapshot_declaration(cls, a.candidate_adapter)
        o = read_timestamps(a.oracle, a.channel, a.oracle_table)
        c = read_timestamps(a.candidate, a.channel, a.candidate_table)
        served = read_served(a.served_json) if a.served_json else None
        report = compare(o, c, a.channel, served_ts=served,
                         covered_from=a.covered_from,
                         covered_through=a.covered_through,
                         accept=[s.strip() for s in a.accept.split(",") if s.strip()],
                         snapshot_unavailable=declaration)
    except (ConfigError, ParityError) as ex:
        print(f"PARITY ERROR: {ex}", file=sys.stderr)
        return 2
    if a.panel_json:
        # The differ run is the WRITER of dashboard state (the viewer never mints
        # any); generated_at rides along so a stale verdict can be judged stale.
        out = Path(a.panel_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({**report.panel(), "generated_at": time.time()},
                                  indent=2))
    print(report.summary())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
