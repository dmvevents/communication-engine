#!/usr/bin/env python3
"""scripts/scheduler.py — the runnable reference loop (ENH-6; quickstart step 8).

Wires probe -> classify -> journal -> route -> owed from YOUR settings.json, with the
loop lessons already correct (core/schedule.py): single-instance guard per state
directory, cursor committed only after the journal, idle backoff that can never
suppress owed work, and edge-triggered escalation of unattended work.

Like first-poll.py, this script is read-only by construction: the send layer
(core/outbox) is never imported, so no bug in this loop can post as anyone.
tests/test_schedule.py enforces that with an AST check. Escalations go to stdout as
`OPERATOR:` lines — wire them to a real pager by replacing `notify`, never by giving
this loop a send path.

Exit codes: 0 = ran to --once/--max-cycles/Ctrl-C; 2 = configuration refused;
3 = another scheduler already holds this state directory's lock.
"""
import argparse
import importlib.util
import os
import sys
from pathlib import Path

# Runs from a fresh clone with no install step, so the repo root (this file's
# grandparent) goes on sys.path explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import ConfigError, ensure_dirs, load, load_adapter_class  # noqa: E402
from core.classify import Taxonomy  # noqa: E402
from core.escalate import Escalator  # noqa: E402
from core.journal import Journal  # noqa: E402
from core.owed import OwedRegistry  # noqa: E402
from core.schedule import AlreadyRunning, Scheduler, Source  # noqa: E402
from core.store import Store  # noqa: E402


def pid_alive(driver: str) -> bool:
    """Reference liveness probe: a driver is a `pid:<n>` string, checked against the
    process table. A driver STRING is not evidence (core/owed.py) — this is what turns
    the string into evidence. Anything this probe cannot verify counts as dead, so an
    unverifiable driver surfaces as unattended instead of silently passing."""
    if not driver or not driver.startswith("pid:"):
        return False
    try:
        os.kill(int(driver[4:]), 0)
    except (ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True                    # exists, just owned by someone else
    return True


def seed_demo(adapters, cfg):
    """Plant the SAME demo messages first-poll.py plants — imported from it, because a
    literal copy here would be a third place for that text to drift (the ENH-23 class)."""
    spec = importlib.util.spec_from_file_location(
        "first_poll", Path(__file__).resolve().parent / "first-poll.py")
    first_poll = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(first_poll)
    for inst in cfg.instances:
        adapter = adapters[inst.name]
        if hasattr(adapter, "seed"):
            adapter.seed(first_poll.demo_messages(inst))
        else:
            print(f"note: adapter {inst.adapter!r} has no dry-run seed() hook; "
                  "polling it as-is")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Run the reference probe/classify/journal/route/owed loop.")
    ap.add_argument("--config", default="settings.json",
                    help="path to your settings.json (default: ./settings.json)")
    ap.add_argument("--once", action="store_true",
                    help="fire exactly one cycle and exit — cron-friendly: the "
                         "single-instance guard makes overlapping fires refuse "
                         "instead of racing")
    ap.add_argument("--max-cycles", type=int, default=None,
                    help="stop after N cycles (default: run until Ctrl-C)")
    ap.add_argument("--seed-demo", action="store_true",
                    help="plant the quickstart demo message in adapters that expose a "
                         "dry-run seed() hook (the fake adapter does)")
    args = ap.parse_args(argv)

    try:
        cfg = load(args.config)
    except ConfigError as ex:
        print(f"REFUSED: {ex}", file=sys.stderr)
        return 2
    ensure_dirs(cfg)

    adapters = {inst.name: load_adapter_class(cfg.channels_dir, inst.adapter)(
        auth=inst.auth) for inst in cfg.instances}
    if args.seed_demo:
        seed_demo(adapters, cfg)

    sources = [Source(name=inst.name, adapter=adapters[inst.name],
                      channels=tuple(ch.id for ch in inst.channels),
                      taxonomy=Taxonomy.from_config(inst.taxonomy))
               for inst in cfg.instances]
    # Base cadence: the fastest cadence any channel asked for; backoff may widen it
    # (never past 16x), owed work restores it (core/schedule.py, R3).
    base = min((ch.poll_interval_s for inst in cfg.instances
                for ch in inst.channels), default=60)

    store = Store(cfg.store_path)
    journal = Journal(cfg.journal_path)
    owed = OwedRegistry(cfg.state_dir / "owed.db", driver_alive=pid_alive)
    escalator = Escalator(cfg.state_dir / "escalate.db",
                          notify=lambda m: print(f"OPERATOR: {m}", flush=True))

    def report(summary):
        print(f"cycle: polled={summary['polled']} fresh={summary['fresh']} "
              f"unattended={summary['unattended']} "
              f"next-fire<={summary['next_interval']:.0f}s", flush=True)

    sched = Scheduler(store=store, journal=journal, owed=owed, escalator=escalator,
                      sources=sources, base_interval=base,
                      lock_path=cfg.state_dir / "scheduler.lock", on_cycle=report)
    try:
        fired = sched.run(max_cycles=1 if args.once else args.max_cycles)
    except AlreadyRunning as ex:
        print(f"REFUSED: {ex}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("stopped", flush=True)
        return 0
    finally:
        store.close()
        journal.close()
        owed.close_db()
        escalator.close_db()

    print(f"SCHEDULER DONE — {fired} cycle(s); state under {cfg.state_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
