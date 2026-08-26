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

    in_flight               an INTENT/SENT outbox row — the process died mid-send, and
                            only Outbox.recover()'s read-back can tell "sent,
                            unrecorded" from "never sent". Can double-message someone,
                            so nothing outranks it.
    edited_after_response   we answered version N and the channel now shows version
                            N+1 — the answer may no longer hold (journal R23).
    staged                  drafts stopped at the operator gate, waiting for a human.
    unanswered              the open ask backlog.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SEVERITY = ("in_flight", "edited_after_response", "staged", "unanswered")

_WHY = {
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


def snapshot(journal_path: str | Path, outboxes: dict[str, str | Path] | None = None
             ) -> dict:
    """The whole operator view in one read-only pass.

    `outboxes` maps instance name -> that instance's outbox file (the caller derives
    them with config's `outbox_path_for`, the same per-tenant split ENH-7 enforces on
    the write side).

    Returns::

        attention   every item needing a human, in SEVERITY order
        missing     paths that do not exist yet — reported, never created
        journal     distinct/unanswered/answered/by_kind counts, or None if missing
        outbox      instance name -> per-state counts, or None if that file is missing
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

    return {
        "attention": [item for sev in SEVERITY for item in queues[sev]],
        "missing": missing,
        "journal": j_counts,
        "outbox": outbox_counts,
    }
