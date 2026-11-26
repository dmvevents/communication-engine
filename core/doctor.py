"""core/doctor.py — the preflight command (ENH-5).

Most adoption failures are configuration failures, and before this command they surfaced
as a stack trace from the middle of the first poll. One command answers, BEFORE the
engine runs: does the config load, do the credentials resolve, is every configured
channel actually readable, and what reply policy is in force where?

    python3 -m core.doctor --config settings.json

Exit 0 = every check passed. 1 = at least one check failed. 2 = the configuration was
refused outright (the loud-refusal behaviour docs/RUNBOOK.md documents).

Design rules, each inherited from a measured defect:

* Verdicts come from core/checks.py, so a PASS that inspected nothing is refused at
  construction — the incumbent watchdog read "OK — 7 checks passed" for weeks while one
  check was inert (docs/PROVENANCE.md). A doctor with a vacuous-pass path would be that
  defect handed to every adopter.
* "Each channel is readable" is proven by ONE live adapter.poll() per instance, not by
  auth.test alone: an instance can authenticate perfectly and still watch nothing. The
  probe reuses the engine's own persisted cursor when state exists (bounded work on a
  live install) and persists NOTHING — a preflight that moves the cursor would eat the
  next real poll.
* An adapter that declares its watched channel set (a `channels` attribute, as both
  shipped real adapters do) is cross-checked against the config: an id present in one
  list and missing from the other polls nothing and looks successful — the two-place
  footgun measured during the first real bring-up (fire=11).
* The printed reply policy is resolved by core/outbox's OWN policy_for, never
  re-derived here: a doctor that reimplements the resolution can drift from the
  enforcement and print a policy the outbox would refuse.
* Secrets never appear in output: the doctor names WHICH env references resolved,
  never what they resolved to — a preflight that echoes a token value turns a
  diagnostic paste into a leak.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

from core.checks import Registry, Verdict
from core.config import ConfigError, load, load_adapter_class
from core.outbox import Outbox
from core.parity import snapshot_declaration


def _env_ref_names(config_path):
    """Which env references the config's auth blocks carry — from the PARSED structure,
    never a text scan. Measured on this command's first live run: the shipped example's
    `_note` prose contains the literal string 'env:NAME', and a text scan reported NAME
    as a resolved credential that was never configured. (The raw file is re-read because
    load() resolves the references eagerly; the loaded config holds values, and values
    must never reach this report.)"""
    raw = json.loads(Path(config_path).read_text())
    names = set()
    for spec in raw.get("instances", ()):
        for value in (spec.get("auth") or {}).values():
            if isinstance(value, str) and value.startswith("env:"):
                names.add(value[4:])
    return sorted(names)


def _persisted_cursor(cfg, inst):
    """The engine's cursor for this instance, read WITHOUT touching the store.

    Opened read-only via sqlite URI mode rather than core.store.Store, because Store's
    constructor runs schema DDL and migrations — a preflight must not create or migrate
    state. The raw query is pinned to the real schema by a test that writes the cursor
    through Store and reads it back through the doctor. Any read failure means "no
    cursor": the probe then polls from the adapter's default window, which is the same
    first-contact behaviour the engine itself would have.
    """
    path = Path(cfg.store_path)
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            for ch in inst.channels:
                row = conn.execute(
                    "SELECT cursor FROM cursors WHERE instance=? AND channel_id=?",
                    (inst.name, ch.id)).fetchone()
                if row and row[0]:
                    return row[0]
            return None
        finally:
            conn.close()
    except sqlite3.Error:
        return None


class _Probe:
    """Build the adapter and run ONE live poll per instance, memoized: every dependent
    check must report the same underlying failure (instead of re-tripping it N times),
    and N channel checks must not consume N polls of rate-limit budget."""

    def __init__(self, channels_dir, inst, cursor):
        self.channels_dir = channels_dir
        self.inst = inst
        self.cursor = cursor
        self._adapter = None
        self._adapter_err = None
        self._built = False
        self._polled = False
        self._poll_count = None
        self._poll_err = None

    def adapter(self):
        if not self._built:
            self._built = True
            try:
                cls = load_adapter_class(self.channels_dir, self.inst.adapter)
                self._adapter = cls(auth=self.inst.auth)
            except Exception as ex:  # noqa: BLE001 — surfaced per-check by the registry
                self._adapter_err = ex
        if self._adapter_err is not None:
            raise self._adapter_err
        return self._adapter

    def poll_count(self):
        adapter = self.adapter()
        if not self._polled:
            self._polled = True
            try:
                # The probe's new cursor is deliberately dropped on the floor.
                messages, _ = adapter.poll(self.cursor)
                self._poll_count = len(messages)
            except Exception as ex:  # noqa: BLE001
                self._poll_err = ex
        if self._poll_err is not None:
            raise self._poll_err
        return self._poll_count

    def watch_set(self):
        declared = getattr(self.adapter(), "channels", None)
        if declared is None:
            return None
        return {str(c) for c in declared}


def _adapter_verdict(name, probe, inst):
    adapter = probe.adapter()
    caps = adapter.capabilities()
    if not caps.get("read"):
        return Verdict.failed(
            name, f"adapter {inst.adapter!r} declares read=False — this engine watches "
                  "channels, and an adapter that cannot read has nothing to preflight")
    return Verdict.passed(name, inspected=1,
                          detail=f"{inst.adapter!r} adapter constructed, read capability "
                                 "declared")


def _health_verdict(name, probe):
    h = probe.adapter().health()
    if not isinstance(h, dict) or not {"reachable", "auth_ok"} <= set(h):
        return Verdict.failed(
            name, f"health() answered {h!r} instead of the contract's "
                  "reachable/auth_ok/detail — a health surface that cannot report "
                  "failure is a defect (channels/CONTRACT.md rule 5)")
    detail = str(h.get("detail", ""))
    if not h["reachable"]:
        return Verdict.failed(name, f"unreachable: {detail}")
    if not h["auth_ok"]:
        return Verdict.failed(name, f"platform refused the credentials: {detail}")
    if h.get("complete") is False:
        # ENH-16: a loss admission is a health failure, never a detail string.
        return Verdict.failed(name, "platform admitted dropping deliveries; a "
                                    f"completeness poll is due: {detail}")
    return Verdict.passed(name, inspected=1, detail=detail or "reachable, auth ok")


def _snapshot_verdict(name, probe, inst):
    # Both outcomes PASS: retrievable_ts is an OPTIONAL capability and core degrades
    # rather than demands (channels/CONTRACT.md), so a push adapter without it is not
    # unhealthy. The property this check exists for is the DECLARATION (ENH-27): a
    # parity run against such an adapter is permanently fail-closed, and an operator
    # who was never told reads the resulting ENGINE_LOST rows as a read-path defect —
    # the R8 misreading, re-armed. The doctor is the preflight, so it says so here.
    declaration = snapshot_declaration(probe.adapter(), inst.adapter)
    if declaration is not None:
        return Verdict.passed(name, inspected=1, detail=declaration)
    return Verdict.passed(
        name, inspected=1,
        detail=f"adapter {inst.adapter!r} can supply a platform snapshot "
               "(retrievable_ts) — parity can tell a real loss from an upstream "
               "deletion")


def _readable_verdict(name, probe, ch):
    count = probe.poll_count()
    watched = probe.watch_set()
    if watched is not None and ch.id not in watched:
        return Verdict.failed(
            name, "configured here but absent from the adapter's watched channel set — "
                  "every poll silently skips it and looks successful (the id must "
                  "appear in BOTH the config channels[] and the adapter's own list; "
                  "docs/QUICKSTART.md step 7)")
    if watched is not None:
        detail = f"read by the live preflight poll ({count} message(s) pending)"
    else:
        detail = (f"instance poll succeeded ({count} message(s) pending); the adapter "
                  "declares no fixed watch set")
    return Verdict.passed(name, inspected=1, detail=detail)


def _nothing_watched_verdict(name, inst):
    return Verdict.failed(
        name, f"instance {inst.name!r} watches no channels — there is nothing to "
              "confirm readable, and \"nothing to check\" must never read as healthy")


def _effective_policy(inst, channel_id, scope):
    # Resolved by the enforcement code itself, on a stand-in carrying only the policy
    # map — Outbox.policy_for reads nothing else of self, and using it means the doctor
    # prints exactly what a send would be judged against.
    return Outbox.policy_for(SimpleNamespace(policies=inst.policies()),
                             channel_id, scope)


def run(config_path, env=None, echo=print) -> int:
    """Preflight one settings file. Returns the exit code (0 ok / 1 unhealthy / 2 refused)."""
    config_path = Path(config_path)
    try:
        cfg = load(config_path, env=env)
    except ConfigError as ex:
        echo(f"DOCTOR REFUSED: {ex}")
        return 2

    echo(f"doctor: {config_path} — {len(cfg.instances)} instance(s)")
    names = _env_ref_names(config_path)
    if names:
        echo("credentials: resolved from the environment: " + ", ".join(names))
    else:
        echo("credentials: no env references in the config (nothing to resolve)")

    reg = Registry()
    for inst in cfg.instances:
        probe = _Probe(cfg.channels_dir, inst, _persisted_cursor(cfg, inst))
        reg.add(f"{inst.name}:adapter",
                lambda n=f"{inst.name}:adapter", p=probe, i=inst:
                _adapter_verdict(n, p, i))
        reg.add(f"{inst.name}:health",
                lambda n=f"{inst.name}:health", p=probe: _health_verdict(n, p))
        reg.add(f"{inst.name}:parity-snapshot",
                lambda n=f"{inst.name}:parity-snapshot", p=probe, i=inst:
                _snapshot_verdict(n, p, i))
        if not inst.channels:
            reg.add(f"{inst.name}:channels",
                    lambda n=f"{inst.name}:channels", i=inst:
                    _nothing_watched_verdict(n, i))
        for ch in inst.channels:
            reg.add(f"{inst.name}/{ch.id}:readable",
                    lambda n=f"{inst.name}/{ch.id}:readable", p=probe, c=ch:
                    _readable_verdict(n, p, c))

    results = reg.run_all()
    for v in results:
        echo(str(v))

    echo("reply policy in force (as core/outbox will enforce it):")
    for inst in cfg.instances:
        for ch in inst.channels:
            label = f" [{ch.label}]" if ch.label else ""
            echo(f"  {inst.name}/{ch.id}{label}: "
                 f"channel={_effective_policy(inst, ch.id, 'channel')} "
                 f"thread={_effective_policy(inst, ch.id, 'thread')}")
    echo("  every other target: denied by default ('never')")

    bad = [v for v in results if not v.ok]
    if bad:
        echo(f"DOCTOR FAIL — {len(bad)}/{len(results)} check(s) failed; this "
             "configuration is not healthy")
        return 1
    readable = sum(1 for v in results if v.name.endswith(":readable"))
    echo(f"DOCTOR OK — {len(results)} check(s) passed, "
         f"{readable} channel(s) confirmed readable")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="preflight: validate config, resolve credentials, confirm every "
                    "configured channel is readable, and print the effective reply "
                    "policy per channel (ENH-5)")
    ap.add_argument("--config", default="settings.json",
                    help="path to your settings.json (default: ./settings.json)")
    a = ap.parse_args(argv)
    return run(a.config)


if __name__ == "__main__":
    sys.exit(main())
