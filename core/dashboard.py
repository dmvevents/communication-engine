"""core/dashboard.py — the operator surface's read layer (ENH-8).

The dashboard UI lived in spoke scratch, reading one host's monitor catalog, so an
adopter cloning this repo had no operator surface at all. What an adopter actually has
is the state this engine maintains for them — journal.db and one outbox per instance —
and that is what the ported surface reads. This module builds the whole view; any UI
(the shipped Streamlit shell in scripts/dashboard.py, or the adopter's own) renders it.

Read-only by construction, not by convention: every connection is sqlite `mode=ro`, so
even a bug in this module cannot alter the audit trail it displays. That is also why
Journal/Outbox are NOT reused here — both open read-write and run their schema and
migration scripts, which is correct for the engine and a write for a viewer. Their
column names and state strings appear below as literals; the seam is pinned by
tests/test_dashboard.py seeding through the real modules and requiring this module to
read the result, so a schema rename goes red instead of drifting.

The attention queue is SEVERITY-ordered, the spoke UI's one validated UX lesson
(what needs action is stated before anything scrollable):

    engine_lost             a parity panel proved the platform still serves messages
                            this engine lost (ENH-24). Every other queue item is READ
                            FROM the engine's archive; this one says that archive is
                            wrong, so nothing below it can be trusted until it is 0 —
                            and it is the one divergence class no accept-list can
                            waive (core/parity.py NEVER_ACCEPTABLE).
    in_flight               an INTENT/SENT outbox row — the process died mid-send, and
                            only Outbox.recover()'s read-back can tell "sent,
                            unrecorded" from "never sent". Can double-message someone.
    edited_after_response   we answered version N and the channel now shows version
                            N+1 — the answer may no longer hold (journal R23).
    staged                  drafts stopped at the operator gate, waiting for a human.
    unanswered              the open ask backlog.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SEVERITY = ("engine_lost", "in_flight", "edited_after_response", "staged",
            "unanswered")

_WHY = {
    "engine_lost": ("the platform still serves messages this engine LOST — the one "
                    "divergence class no accept-list can waive; every answer and "
                    "verdict below is read from this archive, so treat it as "
                    "incomplete until this is 0"),
    "in_flight": ("a send may have died mid-flight — run Outbox.recover(); only its "
                  "read-back can tell 'sent, unrecorded' from 'never sent'"),
    "edited_after_response": ("edited AFTER we answered — the reply on the channel "
                              "may answer text that no longer exists"),
    "staged": ("draft waiting at the operator gate — nothing sends until a human "
               "approves this exact text"),
    "unanswered": "open ask — nobody has answered yet",
}


class DashboardError(Exception):
    """The state the caller pointed at cannot be read. Never 'fixed' by creating it."""


def open_ro(db_path: str | Path) -> sqlite3.Connection:
    """A connection that CANNOT write. The is_file guard is not redundant with
    mode=ro: sqlite reports a missing file as a generic 'unable to open', and the
    operator fixing a dashboard needs the path that is wrong."""
    p = Path(db_path)
    if not p.is_file():
        raise DashboardError(
            f"no database at {p} — the dashboard reads state, never creates it")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _journal_view(journal_path: Path):
    """(attention items keyed by severity, counts) for the journal, or (None, None)
    when the file does not exist — an absent journal must read as UNKNOWN, never as
    zero messages (the F-2 false-confidence shape)."""
    if not Path(journal_path).is_file():
        return None, None
    conn = open_ro(journal_path)
    try:
        items = {"edited_after_response": [], "unanswered": []}
        for r in conn.execute(
                "SELECT j.channel, j.ts, j.text, j.kind, j.revision FROM journal j "
                "JOIN revisions r ON r.channel=j.channel AND r.ts=j.ts "
                "WHERE j.responded_at IS NOT NULL AND r.recorded_at > j.responded_at "
                "GROUP BY j.channel, j.ts ORDER BY j.ts"):
            items["edited_after_response"].append({
                "severity": "edited_after_response", "instance": None,
                "where": r["channel"], "ts": r["ts"], "text": r["text"],
                "kind": r["kind"], "state": None,
                "why": _WHY["edited_after_response"]})
        for r in conn.execute(
                "SELECT channel, ts, text, kind FROM journal "
                "WHERE responded_at IS NULL ORDER BY ts"):
            items["unanswered"].append({
                "severity": "unanswered", "instance": None,
                "where": r["channel"], "ts": r["ts"], "text": r["text"],
                "kind": r["kind"], "state": None, "why": _WHY["unanswered"]})
        counts = {
            "distinct": conn.execute("SELECT count(*) FROM journal").fetchone()[0],
            "unanswered": conn.execute(
                "SELECT count(*) FROM journal WHERE responded_at IS NULL"
            ).fetchone()[0],
            "answered": conn.execute(
                "SELECT count(*) FROM journal WHERE responded_at IS NOT NULL"
            ).fetchone()[0],
            "by_kind": {r[0]: r[1] for r in conn.execute(
                "SELECT kind, count(*) FROM journal GROUP BY kind")},
        }
        return items, counts
    finally:
        conn.close()


def _outbox_view(name: str, outbox_path: Path):
    """(attention items keyed by severity, per-state counts) for one instance's
    outbox, or (None, None) when the file does not exist. A missing outbox is the
    normal fresh state — it is created on the first staged or sent draft — but it is
    still reported missing, not rendered as zero sends."""
    if not Path(outbox_path).is_file():
        return None, None
    conn = open_ro(outbox_path)
    try:
        items = {"in_flight": [], "staged": []}
        for r in conn.execute(
                "SELECT target, trigger_ts, text, state FROM outbox "
                "WHERE state IN ('INTENT','SENT') ORDER BY created_at"):
            items["in_flight"].append({
                "severity": "in_flight", "instance": name, "where": r["target"],
                "ts": r["trigger_ts"], "text": r["text"], "kind": None,
                "state": r["state"], "why": _WHY["in_flight"]})
        for r in conn.execute(
                "SELECT target, trigger_ts, text FROM outbox "
                "WHERE state='STAGED' ORDER BY created_at"):
            items["staged"].append({
                "severity": "staged", "instance": name, "where": r["target"],
                "ts": r["trigger_ts"], "text": r["text"], "kind": None,
                "state": "STAGED", "why": _WHY["staged"]})
        counts = {r[0]: r[1] for r in conn.execute(
            "SELECT state, count(*) FROM outbox GROUP BY state")}
        return items, counts
    finally:
        conn.close()


def _tombstone_count(tombstones: Path | None, channel: str):
    """This channel's tombstone count from core/retention.py's store, or None when
    the db does not exist — no retention db means the deletion history is UNKNOWN,
    and rendering 0 would claim 'nothing was ever deleted' on no evidence. The
    table/column names are pinned literals like the journal/outbox reads above:
    core/retention.Tombstones opens read-write and runs its schema, which is a write
    for a viewer."""
    if tombstones is None or not Path(tombstones).is_file():
        return None
    conn = open_ro(tombstones)
    try:
        return conn.execute("SELECT count(*) FROM tombstones WHERE channel_id=?",
                            (channel,)).fetchone()[0]
    finally:
        conn.close()


def _parity_view(parity_dir: Path, tombstones: Path | None):
    """(attention items, channel -> panel) from the persisted parity panels
    (`python3 -m core.parity --panel-json`, ENH-24), or (None, None) when no differ
    run has left a panel yet — absent parity must read as UNKNOWN, never as clean.

    The dashboard renders verdicts, it never computes them: a panel is the archived
    evidence of a differ run, reviewable after the fact, which a live comparison
    from inside a viewer would not be.
    """
    paths = sorted(parity_dir.glob("*.json")) if parity_dir.is_dir() else []
    if not paths:
        return None, None
    items, panels = [], {}
    for p in paths:
        try:
            panel = json.loads(p.read_text())
        except ValueError as ex:
            raise DashboardError(
                f"parity panel {p} is not valid JSON ({ex}) — a broken panel must "
                "not read as 'no parity'") from ex
        if not isinstance(panel, dict) or "channel" not in panel \
                or "engine_lost" not in panel:
            raise DashboardError(
                f"parity panel {p} lacks channel/engine_lost — not a panel written "
                "by `core.parity --panel-json`, and guessing would render a verdict "
                "nobody computed")
        # Fill the differ's tombstone slot from the retention db, live: the panel
        # was written at differ time, the deletion history keeps growing after.
        panel["tombstones"] = _tombstone_count(tombstones, panel["channel"])
        panels[panel["channel"]] = panel
    for channel in sorted(panels):
        panel = panels[channel]
        if panel.get("engine_lost"):
            sample = ", ".join(panel.get("engine_lost_sample", [])[:3])
            items.append({
                "severity": "engine_lost", "instance": None, "where": channel,
                "ts": panel.get("generated_at"),
                "text": (f"ENGINE_LOST={panel['engine_lost']} — the platform still "
                         f"serves row(s) this engine lost (e.g. ts {sample})"),
                "kind": None, "state": panel.get("verdict"),
                "why": _WHY["engine_lost"]})
    return items, panels


def snapshot(journal_path: str | Path, outboxes: dict[str, str | Path] | None = None,
             parity_dir: str | Path | None = None,
             tombstones: str | Path | None = None) -> dict:
    """The whole operator view in one read-only pass.

    `outboxes` maps instance name -> that instance's outbox file (the caller derives
    them with config's `outbox_path_for`, the same per-tenant split ENH-7 enforces on
    the write side). `parity_dir` holds the panels differ runs persisted
    (--panel-json) and `tombstones` is core/retention.py's db; both are optional
    because parity is a gate an adopter runs, not state the engine always has —
    unwired is silent, wired-but-absent is reported missing.

    Returns::

        attention   every item needing a human, in SEVERITY order
        missing     paths that do not exist yet — reported, never created
        journal     distinct/unanswered/answered/by_kind counts, or None if missing
        outbox      instance name -> per-state counts, or None if that file is missing
        parity      channel -> verdict panel (ENH-24), or None if no panel exists yet
    """
    queues: dict[str, list] = {s: [] for s in SEVERITY}
    missing: list[str] = []

    j_items, j_counts = _journal_view(Path(journal_path))
    if j_items is None:
        missing.append(str(journal_path))
    else:
        for sev, found in j_items.items():
            queues[sev].extend(found)

    outbox_counts: dict[str, dict | None] = {}
    for name in sorted(outboxes or {}):
        o_items, o_counts = _outbox_view(name, Path((outboxes or {})[name]))
        outbox_counts[name] = o_counts
        if o_items is None:
            missing.append(str((outboxes or {})[name]))
        else:
            for sev, found in o_items.items():
                queues[sev].extend(found)

    panels = None
    if parity_dir is not None:
        p_items, panels = _parity_view(
            Path(parity_dir), None if tombstones is None else Path(tombstones))
        if p_items is None:
            missing.append(str(parity_dir))
        else:
            queues["engine_lost"].extend(p_items)
            if tombstones is not None and not Path(tombstones).is_file():
                missing.append(str(tombstones))

    return {
        "attention": [item for sev in SEVERITY for item in queues[sev]],
        "missing": missing,
        "journal": j_counts,
        "outbox": outbox_counts,
        "parity": panels,
    }
